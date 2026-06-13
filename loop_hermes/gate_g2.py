# -*- coding: utf-8 -*-
"""G2 计划确认门。

在 Part 1 设计气泡（part_1_1/part_1_2/part_1_3）完成后，
检查实施计划是否获得确认（用户/自动），按模式分级处置。

闸门等级: L2（safe/collaborative 模式暂停等待确认，auto 模式自动通过）
触发时机: Part 1.3 solution 产出后，进入 Part 2.1 之前。
处置动作:
    - safe 模式: 暂停 180s 等待用户确认，超时自动降级
    - collaborative 模式: 30min 超时，超时后自动降级到 auto
    - auto 模式: 自动确认（确认者标记为 "auto"）
    - unsafe 模式: 跳过确认

确认状态存储:
    state["gate_state"]["plan_confirmed"]: bool
    state["gate_state"]["plan_confirmed_by"]: str | None
    state["pending_confirmation"]: dict
"""

import os
import sys
import time
import select
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger("loop_hermes.gate_g2")

# ============================================================================
# G2 闸门常量
# ============================================================================

GATE_ID = "G2"

# 各模式下的确认超时（秒）
MODE_TIMEOUTS = {
    "safe": 180,           # 3 分钟，等待用户手动确认
    "collaborative": 1800,  # 30 分钟，协作模式超时
    "auto": 0,              # 不超时，立即自动确认
    "unsafe": 0,            # 不超时，跳过确认
}

# 超时后的降级动作
MODE_TIMEOUT_ACTION = {
    "safe": "auto_degrade",         # 超时自动降级
    "collaborative": "auto_degrade",  # 超时自动降级
}


# ============================================================================
# 确认函数
# ============================================================================


def check_plan_confirmed(state: dict) -> bool:
    """检查 state 中计划是否已确认。

    Args:
        state: state 字典

    Returns:
        True 如果 gate_state.plan_confirmed 为 True
    """
    return state.get("gate_state", {}).get("plan_confirmed", False)


def auto_confirm(state: dict) -> Dict[str, Any]:
    """自动确认计划（auto/unsafe 模式使用）。

    Args:
        state: state 字典（原地修改）

    Returns:
        确认结果字典
    """
    gate = state.setdefault("gate_state", {})
    gate["plan_confirmed"] = True
    gate["plan_confirmed_by"] = "auto"

    # 清理 pending_confirmation
    pc = state.setdefault("pending_confirmation", {})
    pc["status"] = "resolved"
    pc["response"] = "auto_confirmed"
    pc["resolved_at"] = datetime.now(timezone.utc).isoformat()

    logger.info("G2 自动确认完成（模式=%s）", state["config"]["mode"])
    return {
        "gate_id": GATE_ID,
        "confirmed": True,
        "confirmed_by": "auto",
        "timestamp": pc["resolved_at"],
    }


def request_user_confirmation(
    state: dict,
    plan_summary: str = "",
) -> Dict[str, Any]:
    """在 safe/collaborative 模式下请求用户确认计划。

    流程:
        1. 打印 plan_summary 到 stdout
        2. 等待用户输入 y/n 或超时
        3. 超时则根据 timeout_action 自动降级

    Args:
        state: state 字典（原地修改）
        plan_summary: Part 1 产出的方案摘要（显示给用户）

    Returns:
        确认结果字典
    """
    mode = state["config"].get("mode", "auto")
    timeout_seconds = MODE_TIMEOUTS.get(mode, 0)
    timeout_action = MODE_TIMEOUT_ACTION.get(mode, "auto_degrade")

    # 设置 pending_confirmation
    pc = state.setdefault("pending_confirmation", {})
    pc.update({
        "id": f"g2-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "status": "pending",
        "phase": state["progress"].get("phase", "unknown"),
        "context": plan_summary[:500] if plan_summary else "Part 1 方案已完成",
        "options": ["y (确认)", "n (拒绝)", "timeout (超时自动降级)"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timeout_minutes": timeout_seconds // 60,
        "timeout_action": timeout_action,
        "response": None,
        "resolved_at": None,
        "attempt": pc.get("attempt", 0),
    })

    # 打印确认提示
    _print_confirmation_prompt(state, plan_summary, timeout_seconds)

    # 等待用户输入
    response = None
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        remaining = timeout_seconds - int(time.time() - start_time)
        if _has_input_available():
            line = sys.stdin.readline().strip().lower()
            if line in ("y", "yes", "确认", "ok"):
                response = "user_confirmed"
                break
            elif line in ("n", "no", "拒绝", "cancel"):
                response = "user_denied"
                break
        time.sleep(0.5)

    # 超时处理
    if response is None:
        logger.warning("G2 确认超时 (%ds)，执行降级: %s", timeout_seconds, timeout_action)
        response = "auto_degrade"

    # 处理响应
    gate = state.setdefault("gate_state", {})
    pc["response"] = response
    pc["resolved_at"] = datetime.now(timezone.utc).isoformat()

    if response == "user_confirmed":
        gate["plan_confirmed"] = True
        gate["plan_confirmed_by"] = "user"
        pc["status"] = "resolved"
        logger.info("G2 用户确认通过")
        return {
            "gate_id": GATE_ID,
            "confirmed": True,
            "confirmed_by": "user",
            "timestamp": pc["resolved_at"],
        }

    elif response == "user_denied":
        gate["plan_confirmed"] = False
        gate["plan_confirmed_by"] = None
        pc["status"] = "denied"
        logger.warning("G2 用户拒绝确认")
        return {
            "gate_id": GATE_ID,
            "confirmed": False,
            "confirmed_by": None,
            "reason": "user_denied",
            "timestamp": pc["resolved_at"],
        }

    else:  # auto_degrade
        gate["plan_confirmed"] = True
        gate["plan_confirmed_by"] = "auto_degrade"
        pc["status"] = "degraded"
        logger.info("G2 超时自动降级，确认通过")
        return {
            "gate_id": GATE_ID,
            "confirmed": True,
            "confirmed_by": "auto_degrade",
            "reason": "timeout",
            "timestamp": pc["resolved_at"],
        }


def _print_confirmation_prompt(
    state: dict,
    plan_summary: str,
    timeout_seconds: int,
) -> None:
    """打印计划确认提示到 stdout。

    Args:
        state: state 字典
        plan_summary: 方案摘要
        timeout_seconds: 超时秒数
    """
    print("\n" + "=" * 60)
    print("  [G2] Part 1 方案确认")
    print("=" * 60)
    print(f"  需求: {state['config'].get('user_request', '(未指定)')[:80]}")
    print(f"  模式: {state['config'].get('mode', 'auto')}")
    if plan_summary:
        print(f"  摘要: {plan_summary[:200]}")
    print("-" * 60)
    print(f"  请在 {timeout_seconds}s 内确认:")
    print(f"    y/yes/确认  → 通过，继续 Part 2")
    print(f"    n/no/拒绝   → 拒绝，返回 Part 1")
    print(f"    超时        → 自动降级，继续执行")
    print("=" * 60)
    print("", flush=True)


def _has_input_available() -> bool:
    """检查 stdin 是否有数据可读（非阻塞）。

    跨平台实现：
        - Unix: select.select
        - Windows: msvcrt.kbhit

    Returns:
        True 如果有输入待读取
    """
    if sys.platform == "win32":
        try:
            import msvcrt
            return msvcrt.kbhit()
        except ImportError:
            pass

    # Unix fallback / Windows 降级
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(r)
    except (OSError, ValueError):
        return False


# ============================================================================
# 准入判定
# ============================================================================


def should_confirm(state: dict) -> bool:
    """判断当前是否需要运行 G2 计划确认。

    触发条件:
        - phase 为 part_1_3（方案产出刚完成）
        - 或 plan_confirmed 为 False 且 phase 即将进入 part_2_1

    Args:
        state: state 字典

    Returns:
        True 需要确认
    """
    mode = state["config"].get("mode", "auto")
    phase = state["progress"].get("phase", "")
    plan_confirmed = state.get("gate_state", {}).get("plan_confirmed", False)

    # unsafe 模式永远不需要确认
    if mode == "unsafe":
        return False

    # auto 模式第一次自动确认后不再确认
    if mode == "auto" and plan_confirmed:
        return False

    # Part 1 刚完成（part_1_3 → 需要确认才能进入 Part 2）
    if phase in ("part_1_3", "part_2_1") and not plan_confirmed:
        return True

    return False


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g2(
    state: dict,
    plan_summary: str = "",
) -> Dict[str, Any]:
    """运行 G2 计划确认门完整流程。

    根据运行模式决定确认策略:
        - safe: 请求用户确认，180s 超时
        - collaborative: 请求用户确认，30min 超时
        - auto: 自动确认
        - unsafe: 跳过

    Args:
        state: state 字典（原地修改）
        plan_summary: 方案摘要（safe/collaborative 模式下展示给用户）

    Returns:
        确认结果字典
    """
    mode = state["config"].get("mode", "auto")

    if not should_confirm(state):
        logger.debug("G2 跳过：不需要确认（mode=%s, phase=%s）",
                      mode, state["progress"].get("phase"))
        return {
            "gate_id": GATE_ID,
            "confirmed": True,
            "confirmed_by": "skipped",
            "reason": f"mode={mode}, plan already confirmed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if mode in ("safe", "collaborative"):
        return request_user_confirmation(state, plan_summary)
    else:
        return auto_confirm(state)
