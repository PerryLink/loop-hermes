# -*- coding: utf-8 -*-
r"""路由规则模块 —— P0/P1/P2 检测与路由决策树。

路由层级:
    P0 检测 → Part 1 重新设计（回退到设计气泡起点）
    P1 检测 → 决策树分类（设计级 → Part 1.3 / 实现级 → Part 2.2 repair）
    P2 检测 → Part 2.2 repair 模式

P1 决策树:
    优先级 1（否定条件，快速短路 → IMPLEMENTATION_LEVEL）:
        N1: source 为 lint_warning 或 build_error
        N2: 仅影响 1 个模块且不涉及接口变更
        N3: fix_strategy 指示代码级修复
        N4: 仅影响 1-2 个文件且不涉及接口变更
    优先级 2（设计级正向条件，先匹配先赢）:
        C3: Recurrence（同问题反复出现 → 设计级，最高优先）
        C5: Security foundation（安全基础设施 → 设计级）
        C2: Cross-module impact（跨 3+ 模块 → 设计级）
        C4: Blocking multiple tasks（阻塞 2+ 任务 → 设计级）
        C1: Root cause in design（源头在设计中 → 设计级）
    优先级 3: Default → IMPLEMENTATION_LEVEL

convergence_counter 5 优先级操作表:
    P1: 非 routing 阶段 → 保持不变
    P2: 路由目标为 Part 1 → 重置为 0
    P3: 本轮发现新 P1 → 重置为 0
    P4: 本轮发现新 P2 → 重置为 0
    P5: 无新问题 + 全部已关闭 + 全部测试通过 → +1
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("loop_hermes.routing")

# ============================================================================
# 常量定义
# ============================================================================

# 路由目标映射
ROUTE_TARGET = {
    "part_1_1": "part_1_1",
    "part_1_3": "part_1_3",
    "part_2_1": "part_2_1",
    "part_2_2": "part_2_2",
}

# 严重性 → 默认路由目标
SEVERITY_ROUTE = {
    "P0": "part_1_1",
    "P1": "p1_decision",
    "P2": "part_2_2",
}

# ============================================================================
# P1 决策树：否定条件（N1-N4）
# ============================================================================


def _negate_by_source(issue: dict) -> bool:
    """N1: source 为 lint_warning 或 build_error → 实现级。"""
    source = issue.get("source", "")
    return source in ("lint_warning", "build_error")


def _negate_by_single_module_no_interface(issue: dict) -> bool:
    """N2: 仅影响 1 个模块且不涉及接口变更 → 实现级。"""
    modules = _extract_modules(issue)
    if len(modules) > 1:
        return False
    return not _involves_interface_change(issue)


def _negate_by_fix_strategy(issue: dict) -> bool:
    """N3: fix_strategy 指示代码级修复 → 实现级。"""
    fix = (issue.get("fix_strategy", "") or "").lower()
    code_level_keywords = [
        "补写校验", "修复错误的api", "处理边界", "修正资源释放",
        "boundary check", "null check", "validation", "parameter fix",
        "补写检查", "修复调用", "修正参数", "处理空值",
    ]
    return any(kw in fix for kw in code_level_keywords)


def _negate_by_few_files(issue: dict) -> bool:
    """N4: 仅影响 1-2 个文件且不涉及接口变更 → 实现级。"""
    files = issue.get("affected_files", [])
    if len(files) > 2:
        return False
    return not _involves_interface_change(issue)

# ============================================================================
# P1 决策树：设计级条件（C1-C5）
# ============================================================================


def _check_recurrence(issue: dict, history: list) -> bool:
    """C3: Recurrence（同问题反复出现）→ 设计级，最高优先。

    检测方法：比较 issue 标题前 40 字符和历史 routing reason 的语义重叠。
    """
    title = (issue.get("title", "") or "").lower()
    desc = (issue.get("description", "") or "").lower()
    issue_id = issue.get("id", "")

    for entry in history:
        reason = (entry.get("reason", "") or "").lower()
        # 标题语义匹配
        if title and len(title) >= 10:
            # 取标题前 40 字符进行包含性匹配
            title_snippet = title[:40]
            if title_snippet in reason:
                return True
        # issue ID 匹配
        if issue_id and issue_id in reason:
            return True
        # 描述关键词匹配
        desc_words = set(desc.split()[:10])
        reason_words = set(reason.split()[:20])
        if len(desc_words & reason_words) >= 4:
            return True
    return False


def _check_security_foundation(issue: dict) -> bool:
    """C5: Security foundation（安全基础设施）→ 设计级。

    检测安全架构级关键词：认证机制、授权模型、加密方案等。
    """
    text = (issue.get("title", "") + " " + issue.get("description", "")).lower()
    security_keywords = [
        "authentication mechanism", "authorization model", "encryption scheme",
        "session management", "credential storage", "token design",
        "auth flow", "access control model", "auth architecture",
        "认证机制", "授权模型", "加密方案", "会话管理",
    ]
    return any(kw in text for kw in security_keywords)


def _check_cross_module(modules: set) -> bool:
    """C2: Cross-module impact（跨 3+ 模块）→ 设计级。"""
    return len(modules) >= 3


def _check_blocking(issue: dict, state: dict) -> bool:
    """C4: Blocking multiple tasks（阻塞 2+ 任务）→ 设计级。

    检测方法：通过 linked_issue_ids 或 dependencies 判断阻塞范围。
    """
    issue_id = issue.get("id", "")
    blocking_count = 0

    # 检查 state 中的 tasks（dict 格式）
    tasks_data = state.get("tasks", {})
    task_list = tasks_data if isinstance(tasks_data, list) else []

    for task in task_list:
        linked = task.get("linked_issue_ids", [])
        deps = task.get("dependencies", [])
        if issue_id in linked or issue_id in deps:
            blocking_count += 1

    return blocking_count >= 2


def _check_root_in_design(issue: dict) -> bool:
    """C1: Root cause in design（源头在设计中）→ 设计级。

    条件：source 为 manual_inspection 且所有 affected_files 为 .md 文件。
    """
    source = issue.get("source", "")
    if source not in ("manual_inspection", "self_check"):
        return False
    files = issue.get("affected_files", [])
    if not files:
        return False
    return all(f.endswith(".md") for f in files)


# ============================================================================
# 辅助函数
# ============================================================================


def _extract_modules(issue: dict) -> set:
    """从 affected_files 中提取模块名集合。

    Args:
        issue: issue 字典

    Returns:
        模块名集合
    """
    files = issue.get("affected_files", [])
    modules = set()
    for f in files:
        parts = f.replace("\\", "/").split("/")
        # 跳过常见顶层目录
        top_level_skip = {"src", "tests", "artifacts", "docs", "lib", "dist", "build"}
        if len(parts) >= 2:
            if parts[0] in top_level_skip and len(parts) > 1:
                modules.add(parts[1])
            else:
                modules.add(parts[0])
        elif len(parts) == 1:
            modules.add(parts[0])
    return modules


def _involves_interface_change(issue: dict) -> bool:
    """检测 issue 是否涉及接口/签名级变更。

    Args:
        issue: issue 字典

    Returns:
        True 如果涉及接口变更
    """
    text = (
        (issue.get("description", "") or "") + " " +
        (issue.get("title", "") or "")
    ).lower()
    interface_keywords = [
        "interface", "signature", "api change", "renamed function",
        "renamed class", "parameter change", "return type",
        "breaking change", "public api",
    ]
    return any(kw in text for kw in interface_keywords)


# ============================================================================
# P1 决策树主函数
# ============================================================================


def classify_p1(issue: dict, state: dict) -> str:
    """P1 决策树：判定 P1 issue 是设计级还是实现级。

    决策流程:
        1. 否定条件（N1-N4）：任一命中 → IMPLEMENTATION_LEVEL
        2. 设计级条件（C3→C5→C2→C4→C1）：优先级递减，首个命中 → DESIGN_LEVEL
        3. Default → IMPLEMENTATION_LEVEL

    Args:
        issue: P1 issue 字典，含 id/severity/title/source/affected_files/description/fix_strategy
        state: state 字典，用于 recurrence_history 和 task blocking 检查

    Returns:
        "DESIGN_LEVEL" 或 "IMPLEMENTATION_LEVEL"
    """
    history = state.get("routing_history", [])

    # ---- 高优先级设计级条件（优先级最高，覆盖否定条件） ----
    if _check_recurrence(issue, history):
        logger.info("P1 [%s] → DESIGN_LEVEL (C3: recurrence detected)", issue.get("id"))
        return "DESIGN_LEVEL"

    if _check_security_foundation(issue):
        logger.info("P1 [%s] → DESIGN_LEVEL (C5: security foundation)", issue.get("id"))
        return "DESIGN_LEVEL"

    if _check_root_in_design(issue):
        logger.info("P1 [%s] → DESIGN_LEVEL (C1: root cause in design)", issue.get("id"))
        return "DESIGN_LEVEL"

    # ---- 否定条件（快速短路 → IMPLEMENTATION_LEVEL） ----
    if _negate_by_source(issue):
        logger.debug("P1 [%s] → IMPLEMENTATION_LEVEL (N1: source=%s)", issue.get("id"), issue.get("source"))
        return "IMPLEMENTATION_LEVEL"

    if _negate_by_single_module_no_interface(issue):
        logger.debug("P1 [%s] → IMPLEMENTATION_LEVEL (N2: single module, no interface change)", issue.get("id"))
        return "IMPLEMENTATION_LEVEL"

    if _negate_by_fix_strategy(issue):
        logger.debug("P1 [%s] → IMPLEMENTATION_LEVEL (N3: fix_strategy indicates code-level)", issue.get("id"))
        return "IMPLEMENTATION_LEVEL"

    if _negate_by_few_files(issue):
        logger.debug("P1 [%s] → IMPLEMENTATION_LEVEL (N4: <=2 files, no interface change)", issue.get("id"))
        return "IMPLEMENTATION_LEVEL"

    # ---- 设计级正向条件（优先级递减） ----
    modules = _extract_modules(issue)
    if _check_cross_module(modules):
        logger.info("P1 [%s] → DESIGN_LEVEL (C2: cross-module, %d modules)", issue.get("id"), len(modules))
        return "DESIGN_LEVEL"

    if _check_blocking(issue, state):
        logger.info("P1 [%s] → DESIGN_LEVEL (C4: blocking multiple tasks)", issue.get("id"))
        return "DESIGN_LEVEL"

    # ---- Default ----
    logger.debug("P1 [%s] → IMPLEMENTATION_LEVEL (default)", issue.get("id"))
    return "IMPLEMENTATION_LEVEL"


# ============================================================================
# convergence_counter 更新
# ============================================================================


def update_convergence_counter(state: dict) -> int:
    """更新 convergence_counter。

    5 优先级操作表:
        P1: 非 routing 阶段 → 保持不变
        P2: 有活跃 P0 → 重置为 0
        P3: 本轮发现新 P1 → 重置为 0
        P4: 本轮发现新 P2 → 重置为 0
        P5: 无新问题 + 全部问题已关闭 + 全部测试通过 → +1

    Args:
        state: state 字典（原地修改 progress.convergence_counter）

    Returns:
        更新后的 convergence_counter 值
    """
    counter = state["progress"].get("convergence_counter", 0)
    active = state["issues"]["active"]

    # P2: 有活跃 P0 → 重置
    if len(active["p0"]) > 0:
        state["progress"]["convergence_counter"] = 0
        logger.debug("convergence_counter 重置（P2: 活跃 P0=%d）", len(active["p0"]))
        return 0

    # P3: 本轮发现新 P1 → 重置
    snapshot = state["progress"].get("issues_snapshot_at_round_start", {})
    if state["progress"].get("new_issues_this_round", False) and len(active["p1"]) > 0:
        if len(active["p1"]) > snapshot.get("p1", 0):
            state["progress"]["convergence_counter"] = 0
            logger.debug("convergence_counter 重置（P3: 新 P1=%d）", len(active["p1"]))
            return 0

    # P4: 本轮发现新 P2 → 重置
    if state["progress"].get("new_issues_this_round", False) and len(active["p2"]) > 0:
        if len(active["p2"]) > snapshot.get("p2", 0):
            state["progress"]["convergence_counter"] = 0
            logger.debug("convergence_counter 重置（P4: 新 P2=%d）", len(active["p2"]))
            return 0

    # P5: 无新问题 + 全部已关闭 → +1
    if (not state["progress"].get("new_issues_this_round", True)
            and len(active["p0"]) == 0
            and len(active["p1"]) == 0
            and len(active["p2"]) == 0):
        counter += 1
        state["progress"]["convergence_counter"] = counter
        logger.info("convergence_counter 递增: %d → %d", counter - 1, counter)
        return counter

    # P1: 保持不变
    return counter


# ============================================================================
# 路由执行主函数
# ============================================================================


def execute_routing(state: dict) -> Dict[str, Any]:
    """执行路由逻辑，判定下一 phase。

    路由流程:
        1. 检查 P0 → Part 1.1（重新设计）
        2. 检查 P1 → 决策树分类（设计级→Part 1.3, 实现级→Part 2.2 repair）
        3. 检查 P2 → Part 2.2（repair 模式）
        4. 更新 convergence_counter → 判定收敛或继续

    Args:
        state: state 字典（原地修改 phase/cycle/repair_context 等字段）

    Returns:
        路由决策字典: {target, reason, cycle}
    """
    active = state["issues"]["active"]
    route_repeat_max = state["config"].get("route_repeat_max", 3)

    # 1. 检查路由重复
    repeat_count = _count_route_repeats(state)
    if repeat_count >= route_repeat_max:
        logger.warning("路由重复 %d 次（>= %d），暂停", repeat_count, route_repeat_max)
        state["termination"]["status"] = "paused"
        state["termination"]["exit_reason"] = (
            f"路由在同一点重复 {repeat_count} 次，已达上限 {route_repeat_max}"
        )
        return {"target": "paused", "reason": state["termination"]["exit_reason"]}

    # 2. P0 检测 → Part 1
    if len(active["p0"]) > 0:
        return _route_to("part_1_1", state, f"P0 问题: {len(active['p0'])} 个")

    # 3. P1 检测 → 决策树
    if len(active["p1"]) > 0:
        issue = active["p1"][0]
        level = classify_p1(issue, state)
        if level == "DESIGN_LEVEL":
            target = "part_1_3"
        else:
            target = "part_2_2"
            state["progress"]["repair_context"] = _build_repair_context(issue, "P1")
        return _route_to(target, state, f"P1 问题 ({level}): {issue.get('title', '')}")

    # 4. P2 检测 → Part 2.2 repair
    if len(active["p2"]) > 0:
        issue = active["p2"][0]
        state["progress"]["repair_context"] = _build_repair_context(issue, "P2")
        return _route_to("part_2_2", state, f"P2 问题: {len(active['p2'])} 个")

    # 5. 更新 convergence_counter
    convergence = update_convergence_counter(state)
    convergence_rounds = state["config"].get("convergence_rounds", 2)
    max_cycles = state["config"].get("max_cycles", 5)
    cycle = state["progress"].get("cycle", 0)

    if convergence >= convergence_rounds:
        if cycle >= max_cycles:
            state["termination"]["status"] = "complete"
            state["termination"]["completed_at"] = datetime.now(timezone.utc).isoformat()
            state["termination"]["exit_reason"] = "所有条件满足（convergence + max_cycles）"
        else:
            state["termination"]["status"] = "complete"
            state["termination"]["completed_at"] = datetime.now(timezone.utc).isoformat()
            state["termination"]["exit_reason"] = "收敛达成"
        return {"target": "complete", "reason": state["termination"]["exit_reason"]}

    # 6. 无活跃问题，继续下一 cycle
    return _route_to("part_2_1", state, "无活跃问题，重新进入 Part 2 下一轮")


# ============================================================================
# 内部辅助
# ============================================================================


def _route_to(target: str, state: dict, reason: str) -> Dict[str, Any]:
    """记录路由决策并更新 state。

    Args:
        target: 目标 phase
        state: state 字典（原地修改）
        reason: 路由原因

    Returns:
        路由决策字典
    """
    state["progress"]["phase"] = target
    cycle = state["progress"].get("cycle", 0) + 1
    state["progress"]["cycle"] = cycle

    # 记录路由历史
    history_entry = {
        "cycle": cycle,
        "from": "routing",
        "to": target,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    state.setdefault("routing_history", []).append(history_entry)

    # 路由重复追踪
    key = f"routing→{target}"
    tracker = state.setdefault("routing_repeat_tracker", {})
    tracker[key] = tracker.get(key, 0) + 1

    # 快照当前 issue 数量
    active = state["issues"]["active"]
    state["progress"]["issues_snapshot_at_round_start"] = {
        "p0": len(active["p0"]),
        "p1": len(active["p1"]),
        "p2": len(active["p2"]),
    }
    state["progress"]["new_issues_this_round"] = False

    logger.info("路由决策: → %s (cycle=%d, reason=%s)", target, cycle, reason)
    return {"target": target, "reason": reason, "cycle": cycle}


def _build_repair_context(issue: dict, severity: str) -> Dict[str, Any]:
    """构建 repair_context 用于 Part 2.2 修复模式。

    状态机: null → active → consumed(null)

    Args:
        issue: 触发修复的 issue
        severity: issue 严重性（P1/P2）

    Returns:
        repair_context 字典
    """
    return {
        "from_phase": "routing",
        "routing_reason": f"{severity} issue: {issue.get('title', '')}",
        "target_issues": [issue.get("id", "")],
        "repair_plan": None,
        "attempt_number": 1,
        "review_required": severity == "P1",
        "affected_files": issue.get("affected_files", []),
        "hermes_guardrail_source": issue.get("source") == "hermes_guardrail",
    }


def _count_route_repeats(state: dict) -> int:
    """计算当前路由目标的重复次数。

    取 routing_repeat_tracker 中当前目标的最大值。

    Args:
        state: state 字典

    Returns:
        重复次数
    """
    tracker = state.get("routing_repeat_tracker", {})
    if not tracker:
        return 0
    return max(tracker.values())
