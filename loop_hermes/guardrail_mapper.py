# -*- coding: utf-8 -*-
"""Hermes Guardrail 映射模块。

将 Hermes Agent 返回的 guardrail 事件映射为 loop-hermes 内部的
严重性等级（P0/P1/P2）和处置动作。

映射规则:
    - HARDLINE / HARDLINE_BLOCK → P0（致命，触发 Part 1 回退）
    - WARN / WARN_PATTERN → P1（警告，进入 P1 决策树）
    - APPROVAL_DENY / APPROVAL_TIMEOUT → P2（需审批，触发 repair）
    - BLOCK / HARDLINE_BLOCK → 直接终止工作流

设计意图:
    Guardrail 事件来自 Hermes Agent 的安全层，表示 Agent 在执行
    过程中触碰了安全边界。loop-hermes 需要将这些事件转化为可路由
    的 issue，以便路由引擎做出正确的决策。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("loop_hermes.guardrail_mapper")

# ============================================================================
# Guardrail 类型 → 严重性映射表
# ============================================================================

GUARDRAIL_SEVERITY_MAP: Dict[str, str] = {
    "HARDLINE": "P0",
    "HARDLINE_BLOCK": "P0",
    "WARN": "P1",
    "WARN_PATTERN": "P1",
    "APPROVAL_DENY": "P2",
    "APPROVAL_TIMEOUT": "P2",
}

# Guardrail 类型 → 默认处置动作
GUARDRAIL_ACTION_MAP: Dict[str, str] = {
    "HARDLINE": "RETREAT_TO_PART1",
    "HARDLINE_BLOCK": "TERMINATE",
    "WARN": "ROUTE_TO_DECISION_TREE",
    "WARN_PATTERN": "ROUTE_TO_DECISION_TREE",
    "APPROVAL_DENY": "REPAIR",
    "APPROVAL_TIMEOUT": "REPAIR",
    "BLOCK": "TERMINATE",
}

# 终止级 guardrail 类型集合
TERMINATING_GUARDRAILS = frozenset({"BLOCK", "HARDLINE_BLOCK"})


# ============================================================================
# Guardrail 事件 → Issue 映射
# ============================================================================


def map_guardrail_to_severity(event_type: str) -> str:
    """将 guardrail 事件类型映射为严重性等级。

    Args:
        event_type: guardrail 事件类型字符串（如 HARDLINE、WARN）

    Returns:
        严重性等级: "P0" / "P1" / "P2"
        未知类型默认返回 "P2"
    """
    severity = GUARDRAIL_SEVERITY_MAP.get(event_type)
    if severity:
        return severity
    logger.warning("未知 guardrail 类型 [%s]，默认映射为 P2", event_type)
    return "P2"


def map_guardrail_to_action(event_type: str) -> str:
    """将 guardrail 事件类型映射为处置动作。

    Args:
        event_type: guardrail 事件类型字符串

    Returns:
        处置动作:
            - RETREAT_TO_PART1: 回退到 Part 1 设计气泡
            - ROUTE_TO_DECISION_TREE: 进入 P1 决策树
            - REPAIR: 触发 repair_context 修复
            - TERMINATE: 直接终止工作流
    """
    action = GUARDRAIL_ACTION_MAP.get(event_type)
    if action:
        return action
    logger.warning("未知 guardrail 类型 [%s]，默认动作为 REPAIR", event_type)
    return "REPAIR"


def is_terminating_guardrail(event_type: str) -> bool:
    """判断是否为终止级 guardrail 事件。

    Args:
        event_type: guardrail 事件类型

    Returns:
        True 如果是 BLOCK 或 HARDLINE_BLOCK 类型
    """
    return event_type in TERMINATING_GUARDRAILS


# ============================================================================
# Guardrail 事件 → Issue 对象创建
# ============================================================================


def guardrail_event_to_issue(
    event: dict,
    phase: str,
) -> dict:
    """将单个 guardrail 事件转换为 issue 对象。

    生成的 issue 带 source="hermes_guardrail"，确保路由引擎
    能识别其来源并进行正确处理。

    Args:
        event: guardrail 事件字典，必须包含 type/tool/message/timestamp
        phase: 触发 guardrail 的 phase 名称

    Returns:
        符合 ISSUE_LIST_SCHEMA 的 issue 字典
    """
    event_type = event.get("type", "UNKNOWN")
    severity = map_guardrail_to_severity(event_type)
    tool = event.get("tool", "")
    message = event.get("message", "")
    timestamp = event.get("timestamp", datetime.now(timezone.utc).isoformat())

    # 构造 title（容错处理 tool 缺失的情况）
    tool_suffix = f": {tool}" if tool else ""
    title = f"Hermes guardrail [{event_type}]{tool_suffix}"

    desc_tool = f" triggered by tool '{tool}'." if tool else "."
    description = (
        f"Guardrail event [{event_type}]{desc_tool}\n"
        f"Message: {message}"
    )[:500]

    issue = {
        "id": f"guardrail-{uuid.uuid4().hex[:8]}",
        "severity": severity,
        "title": title,
        "description": description,
        "source": "hermes_guardrail",
        "source_ref": f"guardrail_event@{timestamp}",
        "discovered_in_phase": phase,
        "status": "open",
        "affected_files": [],
        "linked_task_ids": [],
        "fix_strategy": (
            "HARDLINE/P0: 回退到 Part 1 重新设计。"
            if severity == "P0" else
            "WARN/P1: 进入决策树判定是设计级还是实现级修复。"
            if severity == "P1" else
            "APPROVAL/P2: 进入 repair 模式修复触发审批的操作。"
        ),
    }
    return issue


def process_guardrail_events(
    events: List[dict],
    phase: str,
) -> Tuple[List[dict], Dict[str, Any]]:
    """批量处理 guardrail 事件：分类、映射、生成摘要。

    处理流程:
        1. 遍历每个事件
        2. 识别是否有终止级事件（BLOCK / HARDLINE_BLOCK）
        3. 将事件按严重性分组：P0 / P1 / P2
        4. 每个事件生成对应 issue
        5. 返回 issues 列表 + 处理摘要

    Args:
        events: guardrail 事件列表
        phase: 当前 phase 名称

    Returns:
        (issues, summary) 元组:
            - issues: 生成的 issue 字典列表
            - summary: {
                "total": int,
                "by_severity": {"P0": int, "P1": int, "P2": int},
                "terminating": bool,
                "actions": ["RETREAT_TO_PART1", ...],
              }
    """
    issues: List[dict] = []
    summary: Dict[str, Any] = {
        "total": 0,
        "by_severity": {"P0": 0, "P1": 0, "P2": 0},
        "terminating": False,
        "actions": [],
        "highest_severity": None,
    }

    for event in events:
        event_type = event.get("type", "UNKNOWN")
        severity = map_guardrail_to_severity(event_type)
        action = map_guardrail_to_action(event_type)

        # 记录终止事件
        if is_terminating_guardrail(event_type):
            summary["terminating"] = True

        # 创建 issue
        issue = guardrail_event_to_issue(event, phase)
        issues.append(issue)

        # 更新统计
        summary["total"] += 1
        summary["by_severity"][severity] += 1
        if action not in summary["actions"]:
            summary["actions"].append(action)

        # 跟踪最高严重性
        sev_order = {"P0": 0, "P1": 1, "P2": 2}
        current_high = summary["highest_severity"]
        if current_high is None or sev_order.get(severity, 99) < sev_order.get(current_high, 99):
            summary["highest_severity"] = severity

    logger.info(
        "Guardrail 处理完成: %d 个事件, P0=%d P1=%d P2=%d, terminating=%s",
        summary["total"],
        summary["by_severity"]["P0"],
        summary["by_severity"]["P1"],
        summary["by_severity"]["P2"],
        summary["terminating"],
    )
    return issues, summary


# ============================================================================
# Guardrail 事件注入到 state
# ============================================================================


def inject_guardrail_issues_into_state(
    state: dict,
    events: List[dict],
    phase: str,
) -> Dict[str, Any]:
    """将 guardrail 事件转换为 issues 并注入到 state 中。

    副作用:
        - 将生成的 issues 追加到 state["issues"]["active"]["p0/p1/p2"]
        - 更新 state["gate_state"]["hermes_guardrail_events"]
        - 如果存在终止级 guardrail，设置 state["termination"]["status"] = "failed"

    Args:
        state: state 字典（原地修改）
        events: guardrail 事件列表
        phase: 触发 guardrail 的 phase

    Returns:
        处理摘要字典（同 process_guardrail_events 的 summary）
    """
    if not events:
        return {"total": 0, "by_severity": {"P0": 0, "P1": 0, "P2": 0},
                "terminating": False, "actions": [], "highest_severity": None}

    # 记录原始事件到 gate_state
    gate = state.setdefault("gate_state", {})
    gate.setdefault("hermes_guardrail_events", []).extend(events)

    # 批量处理
    issues, summary = process_guardrail_events(events, phase)

    # 注入 issue 到 state
    for issue in issues:
        sev = issue["severity"].lower()
        state["issues"]["active"][sev].append(issue)
        state["issues"]["all_time"][f"{sev}_total"] += 1

    state["progress"]["new_issues_this_round"] = True

    # 终止处理
    if summary["terminating"]:
        state["termination"]["status"] = "failed"
        state["termination"]["exit_reason"] = (
            "Guardrail 终止: 检测到 BLOCK/HARDLINE_BLOCK 事件"
        )
        logger.warning("终止级 guardrail 触发，工作流标记为 failed")

    logger.info(
        "Guardrail issues 注入 state 完成: %d 个 issues",
        len(issues),
    )
    return summary
