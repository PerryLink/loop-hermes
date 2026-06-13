# -*- coding: utf-8 -*-
r"""Phase 分发器模块。

根据 state.json 的 phase 字段分发到对应 prompt 模板，构造完整 prompt 并调用 Hermes。

核心流程:
    1. 读取 state["progress"]["phase"] 确定当前阶段
    2. loading 对应 prompt 模板（prompts/*.txt）
    3. 注入 state.json 数据（user_request, artifacts, context_summary）
    4. 调用 Hermes Agent（via hermes_client.invoke_hermes）
    5. 解析输出 → 更新 state → 原子写入

Part 1 设计气泡:
    三个阶段（1.1→1.2→1.3）在同一次进程调用中顺序推进
    每个子阶段完成后写盘 checkpoint
    内部支持回退: 任一子阶段失败可重试

Part 2 实施链路:
    每个子阶段为一次独立的 loop-hermes 进程调用
    repair_context 协议: 路由产生 repair_context → Part 2.2 修复模式激活
    hard_gate 协议: Part 2.8 最终验证闸门

跨 artifact 数据流规则:
    Rule 1: Test failures → Issues（test_failure → P1/P2 issue）
    Rule 2: Orphan tasks → Issues（pending/failed tasks → P1/P2 issues）
    Rule 3: Issues → Tasks（P0/P1 issue → repair task）
    Rule 4: Context summary 同步
    Rule 5: Convergence 权威定义
"""

import json
import uuid
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from .state_machine import atomic_write_state
from .checksum import update_checksum_in_state

logger = logging.getLogger("loop_hermes.phase_dispatch")

# ============================================================================
# Phase 顺序与映射
# ============================================================================

PHASE_ORDER = [
    "init",
    "part_1_1", "part_1_2", "part_1_3",
    "part_2_1", "part_2_2", "part_2_3", "part_2_4",
    "part_2_5", "part_2_6", "part_2_7", "part_2_8",
    "routing",
]

PHASE_NEXT = {
    "init": "part_1_1",
    "part_1_1": "part_1_2",
    "part_1_2": "part_1_3",
    "part_1_3": "part_2_1",
    "part_2_1": "part_2_2",
    "part_2_2": "part_2_3",
    "part_2_3": "part_2_4",
    "part_2_4": "part_2_5",
    "part_2_5": "part_2_6",
    "part_2_6": "part_2_7",
    "part_2_7": "part_2_8",
    "part_2_8": "routing",
}

PART_1_PHASES = {"part_1_1", "part_1_2", "part_1_3"}

# 所有 11 个 phase 的 prompt 模板文件映射
PROMPT_FILES = {
    "init": "init.txt",
    "part_1_1": "part_1_1_requirements.txt",
    "part_1_2": "part_1_2_direction.txt",
    "part_1_3": "part_1_3_solution.txt",
    "part_2_1": "part_2_1_plan.txt",
    "part_2_2": "part_2_2_implement.txt",
    "part_2_3": "part_2_3_review.txt",
    "part_2_4": "part_2_4_e2e_strategy.txt",
    "part_2_5": "part_2_5_test_plan.txt",
    "part_2_6": "part_2_6_test_execute.txt",
    "part_2_7": "part_2_7_audit.txt",
    "part_2_8": "part_2_8_hard_gate.txt",
}

# Artifact 占位符 → 人类可读标签映射
_ARTIFACT_PLACEHOLDER_MAP = {
    "requirements": "01-requirements.md content",
    "direction": "02-direction.md content",
    "solution": "03-solution.md content",
    "impl_plan": "04-implementation-plan.md content",
    "task_list": "05-task-list.json content",
    "implementation_diff": "05b-implementation-diff.patch content",
    "code_review": "06-code-review.md content",
    "test_plan": "07-test-plan.md content",
    "test_results": "08-test-results.json content",
    "issue_list": "09-issue-list.json content",
    "verification": "10-verification.md content",
    "context_summary": "context_summary",
}


# ============================================================================
# Prompt 模板加载与构造
# ============================================================================


def load_prompt_template(phase: str) -> str:
    """加载指定 phase 的 prompt 模板文件。

    Args:
        phase: phase 名称（如 "part_2_2"）

    Returns:
        prompt 模板文本。模板不存在时返回默认回退 prompt。
    """
    prompt_dir = Path(__file__).parent / "prompts"
    filename = PROMPT_FILES.get(phase)
    if not filename:
        logger.warning("phase [%s] 无对应 prompt 模板，使用默认回退", phase)
        return f"You are loop-hermes. Phase: {phase}. Complete this phase according to the state."
    file_path = prompt_dir / filename
    if not file_path.exists():
        logger.warning("prompt 模板文件不存在: %s，使用默认回退", file_path)
        return f"You are loop-hermes. Phase: {phase}. Complete this phase according to the state."
    return file_path.read_text(encoding="utf-8")


def build_hermes_prompt(phase: str, state: dict) -> str:
    """构造完整的 Hermes Agent prompt。

    注入内容包括:
        - user_request（用户目标）
        - context_summary（上下文摘要，截取最后 5000 字符）
        - 各 artifact 内容（截取前 8000 字符）
        - HLOOP_STATE 变量（cycle, convergence_counter, repair_context）
        - repair_context 协议块（Part 2.2 修复模式）

    Args:
        phase: 当前 phase 名称
        state: state 字典

    Returns:
        构造完成的 prompt 字符串
    """
    template = load_prompt_template(phase)

    # 基础替换
    prompt = template.replace("{user_request}", state["config"].get("user_request", ""))

    # context_summary（截取最后 5000 字符避免 prompt 过长）
    ctx_path = state.get("artifacts", {}).get("context_summary", {}).get("path", "")
    ctx_text = ""
    if ctx_path and Path(ctx_path).exists():
        raw = Path(ctx_path).read_text(encoding="utf-8")
        ctx_text = raw[-5000:] if len(raw) > 5000 else raw
    prompt = prompt.replace("{context_summary}", ctx_text)

    # 各 artifact 内容注入（截取前 8000 字符）
    for art_key, placeholder_label in _ARTIFACT_PLACEHOLDER_MAP.items():
        placeholder = "{" + placeholder_label + "}"
        content = ""
        info = state.get("artifacts", {}).get(art_key, {})
        file_path = info.get("path", "")
        if file_path and Path(file_path).exists():
            raw = Path(file_path).read_text(encoding="utf-8")
            content = raw[:8000] if len(raw) > 8000 else raw
        prompt = prompt.replace(placeholder, content)

    # HLOOP_STATE 变量注入
    progress = state.get("progress", {})
    prompt = prompt.replace("{cycle}", str(progress.get("cycle", 0)))
    prompt = prompt.replace(
        "{convergence_counter}",
        str(progress.get("convergence_counter", 0))
    )
    rc = progress.get("repair_context")
    prompt = prompt.replace("{repair_context}", json.dumps(rc, ensure_ascii=False) if rc else "null")

    # repair_context 协议块（Part 2.2 修复模式专用）
    if phase == "part_2_2" and rc:
        repair_block = _build_repair_context_block(rc)
        prompt += "\n\n" + repair_block

    return prompt


def _build_repair_context_block(repair_ctx: dict) -> str:
    """构建 repair_context 协议块（注入到 Part 2.2 prompt 末尾）。

    repair_context 状态机:
        null → active → consumed(null)

    Args:
        repair_ctx: repair_context 字典

    Returns:
        格式化的 repair_context 协议块
    """
    affected_files = repair_ctx.get("affected_files", [])
    target_issues = repair_ctx.get("target_issues", [])
    reason = repair_ctx.get("routing_reason", "unknown")
    attempt = repair_ctx.get("attempt_number", 1)

    block = "=== REPAIR CONTEXT (Active) ===\n"
    block += f"Routing reason: {reason}\n"
    block += f"Attempt: {attempt}\n"
    block += f"Target issues: {json.dumps(target_issues)}\n"
    block += f"Restricted to files: {json.dumps(affected_files)}\n"
    block += "MODE: REPAIR ONLY\n"
    block += "- Only modify files listed above\n"
    block += "- Do NOT change architecture or add new features\n"
    block += "- Fix the target issues minimally\n"
    block += "- After fixing, set repair_context to null\n"
    block += "=== END REPAIR CONTEXT ===\n"
    return block


# ============================================================================
# Phase 分发主函数
# ============================================================================


def dispatch_phase(state: dict, state_dir: str) -> Dict[str, Any]:
    """主 Phase 分发入口。

    根据当前 phase 决定执行路径:
        - routing → 执行路由逻辑
        - Part 1 阶段 → 设计气泡（同进程内 1.1-1.3 顺序推进）
        - Part 2 阶段 → 单个 phase 执行
        - 终止状态 → 直接返回

    Args:
        state: state 字典（原地修改）
        state_dir: state.json 所在目录路径

    Returns:
        执行结果字典: {phase, status, output, ...}
    """
    current_phase = state["progress"]["phase"]
    state_dir_path = Path(state_dir)
    mode = state["config"].get("mode", "auto")

    logger.info("Phase 分发: phase=%s, cycle=%d, mode=%s", current_phase, state["progress"]["cycle"], mode)

    # 终止状态检查
    if state["termination"]["status"] in ("complete", "paused", "failed"):
        logger.info("工作流已终止: %s", state["termination"]["status"])
        return {
            "phase": current_phase,
            "status": "terminated",
            "reason": state["termination"]["status"],
        }

    # routing 阶段
    if current_phase == "routing":
        return _execute_routing(state, state_dir_path)

    # Part 1 设计气泡
    if current_phase in PART_1_PHASES:
        return _execute_part1_bubble(state, state_dir_path, mode)

    # Part 2 单 phase 执行
    return _execute_single_phase(state, current_phase, state_dir_path, mode)


# ============================================================================
# Part 1 设计气泡（1.1→1.2→1.3 同进程内顺序推进）
# ============================================================================


def _execute_part1_bubble(state: dict, state_dir: Path, mode: str) -> Dict[str, Any]:
    """执行 Part 1 设计气泡：三个阶段在同一次进程调用中顺序推进。

    流程:
        1. 从当前 sub-phase 开始执行
        2. 每个 sub-phase 调用 Hermes → 产出 artifact
        3. 更新 state 并写盘 checkpoint
        4. 下一个 sub-phase 自动推进
        5. 全部完成后进入 Part 2.1
        6. 内部回退: 任一 sub-phase 失败可重试（受 max_part1_rounds 限制）

    Args:
        state: state 字典（原地修改）
        state_dir: state_dir Path
        mode: 运行模式

    Returns:
        执行结果
    """
    logger.info("Part 1 设计气泡启动")
    phases = ["part_1_1", "part_1_2", "part_1_3"]
    max_rounds = state["config"].get("max_part1_rounds", 5)
    round_num = state["progress"].get("part1_round", 0)

    # 确定起始位置
    current_phase = state["progress"]["phase"]
    try:
        start_idx = phases.index(current_phase)
    except ValueError:
        start_idx = 0

    for idx in range(start_idx, len(phases)):
        phase = phases[idx]
        if round_num >= max_rounds:
            logger.warning("Part 1 已达最大轮次 %d，强制推进", max_rounds)
            state["progress"]["phase"] = "part_2_1"
            _record_transition(state, phase, "part_2_1")
            atomic_write_state(state, str(state_dir))
            return {"phase": "part_2_1", "status": "forced_advance"}

        logger.info("Part 1 sub-phase: %s (round %d/%d)", phase, round_num + 1, max_rounds)
        state["progress"]["phase"] = phase
        state["phase_contracts"]["active_phase"] = phase
        state["phase_contracts"]["declared_at"] = datetime.now(timezone.utc).isoformat()

        result = _execute_single_phase(state, phase, state_dir, mode)

        if result.get("status") in ("error", "gate_failed"):
            round_num += 1
            state["progress"]["part1_round"] = round_num
            logger.warning("Part 1 sub-phase [%s] 失败，重试 %d/%d", phase, round_num, max_rounds)
            # 回退到当前 sub-phase 重试
            if round_num < max_rounds:
                state["progress"]["phase"] = phase
                atomic_write_state(state, str(state_dir))
                continue
            else:
                logger.error("Part 1 重试次数耗尽")
                return {"phase": phase, "status": "error", "error": "max part1 rounds exceeded"}

        # 每个 sub-phase 完成后写盘 checkpoint
        state["progress"]["phase"] = phase  # 确保持续跟踪
        atomic_write_state(state, str(state_dir))

    # Part 1 完成 → Part 2
    state["progress"]["phase"] = "part_2_1"
    state["progress"]["cycle"] += 1
    _record_transition(state, "part_1_3", "part_2_1")
    atomic_write_state(state, str(state_dir))
    logger.info("Part 1 设计气泡完成，进入 Part 2")
    return {"phase": "part_2_1", "status": "part1_complete"}


# ============================================================================
# 单 Phase 执行
# ============================================================================


def _execute_single_phase(
    state: dict, phase: str, state_dir: Path, mode: str
) -> Dict[str, Any]:
    """执行单个 phase（调用 Hermes Agent）。

    执行流程:
        1. 检查终止条件
        2. 检查 pending_confirmation（协作模式超时处理）
        3. 构建 Hermes prompt
        4. 调用 Hermes Agent
        5. 解析结果 → 更新 state
        6. Hard gate 检查（Part 2.8）
        7. 更新 artifact checksum → 原子写盘

    Args:
        state: state 字典（原地修改）
        phase: phase 名称
        state_dir: state_dir Path
        mode: 运行模式

    Returns:
        执行结果
    """
    from .hermes_client import invoke_hermes

    # 终止条件检查
    if state["termination"]["status"] in ("complete", "paused", "failed"):
        return {"phase": phase, "status": "terminated", "reason": state["termination"]["status"]}

    # pending_confirmation 超时检查
    pc = state.get("pending_confirmation", {})
    if pc.get("status") == "awaiting_user":
        timeout_result = _check_confirmation_timeout(state, phase)
        if timeout_result:
            return timeout_result

    # 构建 prompt
    prompt = build_hermes_prompt(phase, state)
    logger.debug("Phase [%s] prompt 长度: %d 字符", phase, len(prompt))

    # 调用 Hermes
    try:
        result = invoke_hermes(prompt, phase, state)
    except Exception as e:
        logger.error("Hermes 调用失败 [%s]: %s", phase, e)
        return {"phase": phase, "status": "error", "error": str(e)}

    if not result.get("success"):
        # 检查是否有 guardrail 终止事件
        guardrail_summary = result.get("guardrail_summary") or {}
        if guardrail_summary.get("terminating"):
            state["termination"]["status"] = "failed"
            state["termination"]["exit_reason"] = (
                "Guardrail 终止: 检测到 BLOCK/HARDLINE_BLOCK 事件"
            )
            atomic_write_state(state, str(state_dir))
            return {
                "phase": phase, "status": "terminated",
                "reason": "guardrail_block",
                "error": result.get("error", "unknown"),
                "output": result.get("output", ""),
            }
        return {
            "phase": phase, "status": "error",
            "error": result.get("error", "unknown"),
            "output": result.get("output", ""),
        }

    output = result.get("output", "")
    logger.info("Phase [%s] 执行成功，输出长度: %d", phase, len(output))

    # Guardrail 处理后的终止检查
    guardrail_summary = result.get("guardrail_summary") or {}
    if guardrail_summary.get("terminating"):
        atomic_write_state(state, str(state_dir))
        return {
            "phase": phase, "status": "terminated",
            "reason": "guardrail_block",
            "output_preview": output[:500],
        }

    # Hard gate 检查（Part 2.8）
    if phase == "part_2_8":
        gate_result = _check_hard_gate(state, state_dir)
        if gate_result:
            return gate_result

    # 更新 artifact checksum
    _update_phase_artifact(state, phase, state_dir)

    # 推进 phase
    if phase in PHASE_NEXT:
        next_phase = PHASE_NEXT[phase]
        _record_transition(state, phase, next_phase)
        state["progress"]["phase"] = next_phase
        atomic_write_state(state, str(state_dir))
        logger.info("Phase 推进: %s → %s", phase, next_phase)
        return {"phase": next_phase, "status": "ok", "output_preview": output[:500]}

    atomic_write_state(state, str(state_dir))
    return {"phase": phase, "status": "ok", "output_preview": output[:500]}


# ============================================================================
# Routing 执行
# ============================================================================


def _execute_routing(state: dict, state_dir: Path) -> Dict[str, Any]:
    """执行路由逻辑并写盘。

    Args:
        state: state 字典（原地修改）
        state_dir: state_dir Path

    Returns:
        路由决策结果
    """
    from .routing import execute_routing
    decision = execute_routing(state)
    atomic_write_state(state, str(state_dir))
    logger.info("Routing 完成: target=%s, reason=%s", decision.get("target"), decision.get("reason"))
    return decision


# ============================================================================
# pending_confirmation 超时处理
# ============================================================================


def _check_confirmation_timeout(state: dict, phase: str) -> Optional[Dict[str, Any]]:
    """检查协作模式下的 pending_confirmation 是否超时。

    超时行为:
        - timeout_action = "auto_degrade" → 自动选择默认选项并继续
        - 未超时 → 返回 paused 状态

    Args:
        state: state 字典
        phase: 当前 phase

    Returns:
        超时未决时返回结果 dict；可继续时返回 None
    """
    pc = state.get("pending_confirmation", {})
    created = pc.get("created_at")
    timeout_min = pc.get("timeout_minutes", 30)

    if not created:
        return {"phase": phase, "status": "paused", "reason": "awaiting_confirmation"}

    try:
        created_dt = datetime.fromisoformat(created)
        elapsed = (datetime.now(timezone.utc) - created_dt).total_seconds()
    except (ValueError, TypeError):
        return {"phase": phase, "status": "paused", "reason": "awaiting_confirmation"}

    if elapsed > timeout_min * 60:
        logger.warning("Confirmation 超时 (%dmin)，自动降级", timeout_min)
        options = pc.get("options", [])
        default_opt = next((o for o in options if o.get("is_default")), options[0] if options else None)
        pc["status"] = "auto_resolved"
        pc["resolved_at"] = datetime.now(timezone.utc).isoformat()
        pc["response"] = default_opt
        return None  # 超时已处理，继续执行

    # 仍然等待
    remaining = int(timeout_min * 60 - elapsed)
    logger.info("等待用户确认中...剩余 %d 秒", remaining)
    return {"phase": phase, "status": "paused_awaiting_user", "remaining_seconds": remaining}


# ============================================================================
# Hard Gate 检查（Part 2.8）
# ============================================================================


def _check_hard_gate(state: dict, state_dir: Path) -> Optional[Dict[str, Any]]:
    """Part 2.8 硬验证闸门。

    检查项:
        1. 所有必需 artifact 文件存在
        2. 所有任务完成
        3. 无活跃 P0/P1 问题

    Args:
        state: state 字典
        state_dir: state_dir Path

    Returns:
        闸门未通过时返回失败结果；通过时返回 None
    """
    artifacts_dir = state_dir / "artifacts"

    # 必需 artifact 清单
    required_artifacts = [
        "04-implementation-plan.md", "05-task-list.json",
        "05b-implementation-diff.patch", "06-code-review.md",
        "08-test-results.json", "09-issue-list.json",
    ]
    missing = [f for f in required_artifacts if not (artifacts_dir / f).exists()]
    if missing:
        logger.warning("Hard gate: 缺失 artifact: %s", missing)
        return {
            "phase": "part_2_8", "status": "gate_failed",
            "error": f"Missing artifacts: {missing}",
        }

    # 活跃问题检查
    active = state["issues"]["active"]
    active_p0 = len(active["p0"])
    active_p1 = len(active["p1"])
    if active_p0 > 0 or active_p1 > 0:
        logger.warning("Hard gate: 仍有活跃问题 P0=%d, P1=%d", active_p0, active_p1)
        return {
            "phase": "part_2_8", "status": "gate_blocked",
            "error": f"Cannot complete: {active_p0} P0, {active_p1} P1 issues remain",
        }

    # 任务完成检查
    task_path = artifacts_dir / "05-task-list.json"
    if task_path.exists():
        try:
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            tasks = task_data.get("tasks", [])
            incomplete = [t for t in tasks if t.get("status") not in ("completed", "skipped")]
            if incomplete:
                logger.warning("Hard gate: %d 任务未完成", len(incomplete))
                return {
                    "phase": "part_2_8", "status": "gate_blocked",
                    "error": f"{len(incomplete)} tasks not completed",
                }
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Hard gate: 无法读取 task list: %s", e)

    logger.info("Hard gate: 通过")
    return None


# ============================================================================
# Artifact checksum 更新
# ============================================================================


def _update_phase_artifact(state: dict, phase: str, state_dir: Path) -> None:
    """根据 phase 更新对应 artifact 的 checksum。

    每个 phase 产出固定 artifact，此函数在 phase 完成后更新
    对应 artifact 的 SHA-256 checksum 到 state 中。
    Part 2.1 额外更新 task_list 的 checksum。

    Args:
        state: state 字典（原地修改 artifacts.<key>.checksum）。
        phase: 当前 phase 名称。
        state_dir: state_dir Path（用于 checksum 计算）。

    Raises:
        KeyError: artifact key 在 state 中不存在时。
        OSError: artifact 文件不可读时。
    """
    phase_to_artifact = {
        "init": "context_summary",
        "part_1_1": "requirements",
        "part_1_2": "direction",
        "part_1_3": "solution",
        "part_2_1": "impl_plan",
        "part_2_2": "implementation_diff",
        "part_2_3": "code_review",
        "part_2_5": "test_plan",
        "part_2_6": "test_results",
        "part_2_7": "issue_list",
        "part_2_8": "verification",
    }

    art_key = phase_to_artifact.get(phase)
    if art_key:
        try:
            update_checksum_in_state(state, art_key)
        except (KeyError, OSError) as e:
            logger.warning("更新 artifact [%s] checksum 失败: %s", art_key, e)

    # Part 2.1 同时产出 task_list
    if phase == "part_2_1":
        try:
            update_checksum_in_state(state, "task_list")
        except (KeyError, OSError) as e:
            logger.warning("更新 task_list checksum 失败: %s", e)


# ============================================================================
# Phase 转移记录
# ============================================================================


def _record_transition(state: dict, from_phase: str, to_phase: str) -> None:
    """记录 phase 转移历史。

    将 {from, to, at} 三元组追加到 state["progress"]["phase_transitions"]
    列表中，用于审计和回滚。

    Args:
        state:       state 字典（原地修改 progress.phase_transitions）。
        from_phase:  转换前的 phase 名称。
        to_phase:    转换后的 phase 名称。
    """
    now = datetime.now(timezone.utc).isoformat()
    entry = {"from": from_phase, "to": to_phase, "at": now}
    transitions = state.setdefault("progress", {}).setdefault("phase_transitions", [])
    transitions.append(entry)
    logger.debug("Phase 转移记录: %s → %s", from_phase, to_phase)


# ============================================================================
# 跨 artifact 数据流规则
# ============================================================================


def promote_test_failures_to_issues(state: dict, state_dir: Path) -> int:
    """Rule 1: 将测试失败提升为 P2 issue。

    从 08-test-results.json 读取失败测试，创建对应 issue 加入 state.issues.active.p2。

    Args:
        state: state 字典（原地修改 issues）
        state_dir: state_dir Path

    Returns:
        新创建的 issue 数量
    """
    tr_path = state_dir / "artifacts" / "08-test-results.json"
    if not tr_path.exists():
        return 0

    try:
        data = json.loads(tr_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("无法读取测试结果: %s", e)
        return 0

    count = 0
    for result in data.get("results", []):
        if result.get("status") != "fail":
            continue

        issue = {
            "id": f"issue-test-{uuid.uuid4().hex[:8]}",
            "severity": "P2",
            "title": f"Test failure: {result.get('name', 'unknown')}",
            "description": result.get("error_message", "Test failed")[:500],
            "source": "test_failure",
            "source_ref": f"08-test-results.json#{result.get('id', '')}",
            "discovered_in_phase": "part_2_7",
            "status": "open",
            "affected_files": [],
            "linked_task_ids": [result.get("linked_task_id")] if result.get("linked_task_id") else [],
            "fix_strategy": "Investigate and fix the failing test",
        }
        state["issues"]["active"]["p2"].append(issue)
        state["issues"]["all_time"]["p2_total"] += 1
        state["progress"]["new_issues_this_round"] = True
        count += 1

    if count > 0:
        logger.info("Rule 1: %d 个测试失败提升为 P2 issue", count)
    return count


def promote_orphan_tasks_to_issues(state: dict, state_dir: Path) -> int:
    """Rule 2: 将孤儿任务（pending/failed）提升为 issue。

    从 05-task-list.json 读取未完成任务，创建对应 issue。

    Args:
        state: state 字典（原地修改 issues）
        state_dir: state_dir Path

    Returns:
        新创建的 issue 数量
    """
    tl_path = state_dir / "artifacts" / "05-task-list.json"
    if not tl_path.exists():
        return 0

    try:
        data = json.loads(tl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("无法读取任务列表: %s", e)
        return 0

    count = 0
    for task in data.get("tasks", []):
        status = task.get("status", "")
        if status not in ("pending", "failed"):
            continue

        sev = "P1" if status == "failed" else "P2"
        issue = {
            "id": f"issue-orphan-{uuid.uuid4().hex[:8]}",
            "severity": sev,
            "title": f"Orphan task: {task.get('title', task.get('id', 'unknown'))}",
            "description": f"Task '{task.get('id')}' left in status '{status}'",
            "source": "self_check",
            "source_ref": f"05-task-list.json#{task.get('id', '')}",
            "discovered_in_phase": "part_2_7",
            "status": "open",
            "affected_files": task.get("assigned_files", []),
            "linked_task_ids": [task.get("id")],
            "fix_strategy": "Complete or fix the orphaned task",
        }
        state["issues"]["active"][sev.lower()].append(issue)
        state["issues"]["all_time"][f"{sev.lower()}_total"] += 1
        state["progress"]["new_issues_this_round"] = True
        count += 1

    if count > 0:
        logger.info("Rule 2: %d 个孤儿任务提升为 issue", count)
    return count


def sync_context_summary(state: dict, state_dir: Path, summary_line: str) -> None:
    """Rule 4: 同步上下文摘要到 context-summary.md。

    追加一行带时间戳和 phase 标记的摘要到 context-summary.md 文件末尾，
    并更新 checksum。

    Args:
        state:        state 字典。
        state_dir:    state_dir Path。
        summary_line: 要追加的摘要行（自动添加时间戳前缀）。
    """
    ctx_path = state_dir / "artifacts" / "context-summary.md"
    ctx_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{timestamp}] [{state['progress']['phase']}] {summary_line}\n"

    with open(ctx_path, "a", encoding="utf-8") as f:
        f.write(line)

    # 更新 artifact meta
    try:
        update_checksum_in_state(state, "context_summary")
    except (KeyError, OSError):
        pass


# ============================================================================
# 并行派发集成
# ============================================================================


def dispatch_phase_parallel(
    state: dict,
    state_dir: str,
    task_specs: Optional[List[Any]] = None,
    max_parallel: int = 4,
    fail_fast: bool = True,
) -> Dict[str, Any]:
    """并行派发 sub-agent 执行任务（用于 Part 2.2 / Part 2.6 等）。

    从 state 中提取任务列表，构造 TaskSpec 并通过
    ParallelDelegateManager 并行执行。结果合并后更新 state。

    Args:
        state: state 字典
        state_dir: state 目录
        task_specs: 预先构造的 TaskSpec 列表，为 None 时从 state 自动提取
        max_parallel: 最大并发数
        fail_fast: 是否启用 fail-fast

    Returns:
        执行结果字典: {
            "phase": str,
            "status": str,
            "merge_result": MergeResult,
            "output_preview": str,
        }
    """
    from .parallel_manager import (
        ParallelDelegateManager, TaskSpec,
        merge_results, detect_file_conflicts,
        setup_agent_workspace,
    )
    from .hermes_client import invoke_hermes

    phase = state["progress"]["phase"]
    state_dir_path = Path(state_dir)

    # 自动从 state 构造 TaskSpec
    if task_specs is None:
        task_specs = _build_task_specs_from_state(state)

    if not task_specs:
        logger.info("无并行任务需要派发")
        return {"phase": phase, "status": "no_tasks", "merge_result": None}

    logger.info("并行派发 %d 个 task，max_parallel=%d", len(task_specs), max_parallel)

    # 获取并行配置
    parallel_config = state.get("config", {}).get("parallel_config", {})
    effective_max = parallel_config.get("max_parallel_agents", max_parallel)
    effective_timeout = parallel_config.get("parallel_total_timeout_seconds", 3600)
    effective_fail_fast = parallel_config.get("parallel_fail_fast", fail_fast)

    # 为每个 agent 创建 workspace
    for spec in task_specs:
        try:
            setup_agent_workspace(state_dir, spec.agent_id)
        except OSError as e:
            logger.warning("Agent workspace 创建失败 [%s]: %s", spec.agent_id, e)

    # 定义 executor 函数
    def _agent_executor(spec: TaskSpec) -> Any:
        """Sub-agent 执行器：调用 Hermes 并返回 SubAgentResult。"""
        from .parallel_manager import SubAgentResult
        start = time.time() if 'time' in dir() else __import__('time').time()

        try:
            resp = invoke_hermes(spec.prompt, phase, state)
            modified = spec.assigned_files.copy() if resp.get("success") else []
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="success" if resp.get("success") else "failed",
                output={"response": resp.get("output", "")},
                modified_files=modified,
                guardrail_events=resp.get("guardrail_events", []),
                error=resp.get("error"),
            )
        except Exception as e:
            logger.error("Sub-agent [%s] 执行异常: %s", spec.agent_id, e)
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="failed",
                error=str(e),
            )

    # 创建管理器并执行
    mgr = ParallelDelegateManager(
        max_parallel=effective_max,
        total_timeout=effective_timeout,
        fail_fast=effective_fail_fast,
        state_dir=state_dir,
    )

    import time as _time
    try:
        results = mgr.delegate(task_specs, _agent_executor)
    except Exception as e:
        logger.error("并行派发异常: %s", e)
        return {"phase": phase, "status": "error", "error": str(e)}

    # 合并结果
    merged = merge_results(results)

    # 冲突检测
    if parallel_config.get("parallel_conflict_detection", True):
        conflicts = detect_file_conflicts(results)
        if conflicts:
            logger.warning("并行执行冲突: %s", conflicts)
            merged.merged_output["conflicts"] = conflicts

    # 更新 state
    # 注入 sub-agent 产出的 guardrail 事件
    if parallel_config.get("parallel_guardrail_aggregation", True):
        _inject_parallel_guardrails(state, merged.all_guardrail_events, phase)

    # 更新 artifact checksum
    _update_phase_artifact(state, phase, state_dir_path)

    # 推进 phase
    if phase in PHASE_NEXT:
        next_phase = PHASE_NEXT[phase]
        _record_transition(state, phase, next_phase)
        state["progress"]["phase"] = next_phase

    atomic_write_state(state, str(state_dir_path))

    logger.info(
        "Parallel dispatch 完成: %d/%d 成功",
        merged.succeeded, merged.total,
    )
    return {
        "phase": state["progress"]["phase"],
        "status": "ok" if merged.failed == 0 else "partial_failure",
        "merge_result": merged,
        "output_preview": (
            f"Parallel: {merged.succeeded}/{merged.total} succeeded, "
            f"{merged.failed} failed, {merged.timeout} timeout"
        ),
    }


def _build_task_specs_from_state(state: dict) -> List[Any]:
    """从 state 中的 task_list 自动构造 TaskSpec 列表。

    读取 05-task-list.json，为每个 pending 任务创建 TaskSpec。

    Args:
        state: state 字典

    Returns:
        TaskSpec 列表
    """
    from .parallel_manager import TaskSpec
    import json as _json

    task_list_path = state.get("artifacts", {}).get("task_list", {}).get("path", "")
    if not task_list_path or not Path(task_list_path).exists():
        logger.debug("无 task_list，无法构造 TaskSpec")
        return []

    try:
        data = _json.loads(Path(task_list_path).read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError) as e:
        logger.warning("读取 task_list 失败: %s", e)
        return []

    specs = []
    phase = state["progress"]["phase"]
    for task in data.get("tasks", []):
        status = task.get("status", "")
        if status in ("completed", "skipped"):
            continue

        spec = TaskSpec(
            task_id=task.get("id", f"task-{len(specs)}"),
            prompt=(
                f"Phase: {phase}\n"
                f"Task: {task.get('title', task.get('id', ''))}\n"
                f"Description: {task.get('description', '')}\n"
                f"Module: {task.get('module', '')}\n"
                f"Files: {task.get('assigned_files', [])}\n"
                f"Complete this task and report results."
            ),
            assigned_files=task.get("assigned_files", []),
            module=task.get("module", ""),
            priority=task.get("priority", 1),
            metadata={"source": "task_list", "original_task": task},
        )
        specs.append(spec)

    logger.info("从 task_list 构造了 %d 个 TaskSpec", len(specs))
    return specs


def _inject_parallel_guardrails(
    state: dict,
    guardrail_events: List[dict],
    phase: str,
) -> None:
    """将并行执行中产生的 guardrail 事件注入到 state。

    优先通过 guardrail_mapper 映射为 issues，若不可用则
    直接追加到 gate_state.hermes_guardrail_events。

    Args:
        state:            state 字典（原地修改 issues 或 gate_state）。
        guardrail_events: guardrail 事件列表。
        phase:            当前 phase 名称（用于 issue 溯源）。
    """
    if not guardrail_events:
        return

    try:
        from .guardrail_mapper import inject_guardrail_issues_into_state
        inject_guardrail_issues_into_state(state, guardrail_events, phase)
    except ImportError:
        # 手动记录事件到 gate_state
        gate = state.setdefault("gate_state", {})
        gate.setdefault("hermes_guardrail_events", []).extend(guardrail_events)
        logger.warning("guardrail_mapper 不可用，guardrail 事件仅记录未映射")
