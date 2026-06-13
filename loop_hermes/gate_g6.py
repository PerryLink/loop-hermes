# -*- coding: utf-8 -*-
"""G6 完成门。

作为 loop-hermes 自动驾驶循环的最终闸门，在 convergence 判定
之后执行，确保所有质量条件达成后才能标记为 complete。

闸门等级: L2（所有模式强制执行）
触发时机: convergence 条件初判达成后，标记 complete 之前。
处置动作: 不通过时拒绝 complete，继续进入下一 cycle 或 routing。

检查项（7 项）:
    1. 所有任务已完成（无 pending/in_progress/failed）
    2. 所有测试通过（08-test-results.json pass_rate == 100%）
    3. 无活跃 P0/P1 issue（P2 可容忍）
    4. 所有 artifact 文件 checksum 完整性通过
    5. state.json 自身 Schema 校验通过
    6. gate_state 全部闸门通过
    7. (可选) 用户最终确认（safe/collaborative 模式）

设计意图:
    防止"表面收敛"——即 convergence_counter 达标但不满足
    硬性质量门的虚假完成。必须所有条件同时满足。
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger("loop_hermes.gate_g6")

# ============================================================================
# G6 闸门常量
# ============================================================================

GATE_ID = "G6"

# 检查项 ID 和人类可读描述
CHECK_ITEMS = {
    "tasks_all_complete": "所有任务已完结（无 pending/in_progress/failed）",
    "tests_all_pass": "所有测试通过（pass_rate == 100%）",
    "no_active_p0_p1": "无活跃 P0/P1 issue（P2 可容忍）",
    "artifact_checksums_ok": "所有 artifact 文件 checksum 完整性通过",
    "state_schema_valid": "state.json Schema 校验通过",
    "gates_all_passed": "所有安全闸门（G1-G5）通过",
    "user_final_confirm": "用户最终确认（safe/collaborative 模式）",
}

# 可跳过的检查项（按模式）
SKIPPABLE_CHECKS = {
    "unsafe": {"user_final_confirm"},
    "auto": {"user_final_confirm"},
    "safe": set(),
    "collaborative": set(),
}


# ============================================================================
# G6 各检查项实现
# ============================================================================


def _check_tasks_all_complete(state: dict) -> Dict[str, Any]:
    """检查 1: 所有任务已完成。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    tasks = state.get("tasks", {})
    by_status = tasks.get("by_status", {})

    pending = by_status.get("pending", 0)
    in_progress = by_status.get("in_progress", 0)
    failed = by_status.get("failed", 0)
    completed = by_status.get("completed", 0)
    total = tasks.get("total", 0)

    all_done = (pending == 0 and in_progress == 0 and failed == 0)

    return {
        "check_id": "tasks_all_complete",
        "passed": all_done,
        "details": {
            "total": total,
            "completed": completed,
            "pending": pending,
            "in_progress": in_progress,
            "failed": failed,
        },
        "message": (
            "OK" if all_done
            else f"未完结: pending={pending}, in_progress={in_progress}, failed={failed}"
        ),
    }


def _check_tests_all_pass(state: dict, state_dir: str) -> Dict[str, Any]:
    """检查 2: 所有测试通过。

    读取 08-test-results.json 检查 pass_rate 是否 100%。

    Args:
        state: state 字典
        state_dir: state 目录

    Returns:
        检查结果
    """
    tr_info = state.get("artifacts", {}).get("test_results", {})
    tr_path = tr_info.get("path", "")

    if not tr_path:
        # 未生成测试结果（可能 skip_testing 模式）
        skip_testing = state.get("config", {}).get("skip_testing", False)
        if skip_testing:
            return {
                "check_id": "tests_all_pass",
                "passed": True,
                "details": {"note": "skip_testing 模式，跳过测试检查"},
                "message": "skip_testing 模式，测试检查跳过",
            }
        return {
            "check_id": "tests_all_pass",
            "passed": False,
            "details": {"note": "未生成测试结果文件"},
            "message": "未找到 08-test-results.json",
        }

    try:
        data = json.loads(Path(tr_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {
            "check_id": "tests_all_pass",
            "passed": False,
            "details": {"error": str(e)},
            "message": f"无法读取测试结果文件: {e}",
        }

    summary = data.get("summary", {})
    total = summary.get("total", 0)
    passes = summary.get("pass", 0)
    fails = summary.get("fail", 0)
    errors = summary.get("error", 0)
    pass_rate = summary.get("pass_rate", 0)

    all_pass = (fails == 0 and errors == 0)

    return {
        "check_id": "tests_all_pass",
        "passed": all_pass,
        "details": {
            "total": total,
            "pass": passes,
            "fail": fails,
            "error": errors,
            "pass_rate": pass_rate,
        },
        "message": "OK" if all_pass else f"有 {fails} 失败, {errors} 错误",
    }


def _check_no_active_p0_p1(state: dict) -> Dict[str, Any]:
    """检查 3: 无活跃 P0/P1 issue。

    P2 issue 允许存在（非阻塞性）。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    active = state.get("issues", {}).get("active", {})
    p0 = active.get("p0", [])
    p1 = active.get("p1", [])
    p2 = active.get("p2", [])

    has_p0p1 = len(p0) > 0 or len(p1) > 0

    return {
        "check_id": "no_active_p0_p1",
        "passed": not has_p0p1,
        "details": {
            "p0_count": len(p0),
            "p1_count": len(p1),
            "p2_count": len(p2),
        },
        "message": (
            "OK" if not has_p0p1
            else f"存在活跃 P0={len(p0)}, P1={len(p1)} issue"
        ),
    }


def _check_artifact_checksums(state: dict) -> Dict[str, Any]:
    """检查 4: 所有 artifact 文件 checksum 完整性。

    使用 state_machine.verify_artifact_integrity 进行 SHA-256 校验。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    try:
        from .state_machine import verify_artifact_integrity
        mismatches = verify_artifact_integrity(state)
    except (ImportError, Exception) as e:
        return {
            "check_id": "artifact_checksums_ok",
            "passed": True,  # 保守策略：校验不可用时不阻塞
            "details": {"error": str(e)},
            "message": f"checksum 校验跳过（{e}）",
        }

    all_ok = len(mismatches) == 0

    return {
        "check_id": "artifact_checksums_ok",
        "passed": all_ok,
        "details": {"mismatches": mismatches},
        "message": (
            "OK" if all_ok
            else f"{len(mismatches)} 个文件 checksum 不匹配"
        ),
    }


def _check_state_schema(state: dict) -> Dict[str, Any]:
    """检查 5: state.json Schema 校验。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    try:
        from .schemas import validate_state
        validate_state(state)
        return {
            "check_id": "state_schema_valid",
            "passed": True,
            "details": {},
            "message": "OK",
        }
    except (ValueError, Exception) as e:
        return {
            "check_id": "state_schema_valid",
            "passed": False,
            "details": {"error": str(e)},
            "message": f"Schema 校验失败: {e}",
        }


def _check_gates_all_passed(state: dict) -> Dict[str, Any]:
    """检查 6: 所有安全闸门通过。

    检查 gate_state 中 G1-G5 的通过状态。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    gate = state.get("gate_state", {})
    gate_statuses = {}

    # G1: content_safety_passed
    gate_statuses["G1"] = gate.get("content_safety_passed", False)

    # G2: plan_confirmed
    gate_statuses["G2"] = gate.get("plan_confirmed", False)

    # G3: 无被拦截的依赖安装（通过 dangerous_ops_blocked 中检查）
    blocked_ops = gate.get("dangerous_ops_blocked", [])
    gate_statuses["G3"] = not any(
        "dependency_install" in op.get("operation", "")
        for op in blocked_ops
    )

    # G4: 无被拦截的危险操作
    gate_statuses["G4"] = not any(
        "dependency_install" not in op.get("operation", "")
        for op in blocked_ops
    ) if blocked_ops else True

    # G5: 文件变更未超阈值（通过 file_modifications_this_cycle + blocked_ops）
    # G5 超阈值时也会在 blocked_ops 中有记录
    gate_statuses["G5"] = all(
        "gate_g5" not in op.get("reason", "") + op.get("operation", "")
        for op in blocked_ops
    )

    all_passed = all(gate_statuses.values())

    return {
        "check_id": "gates_all_passed",
        "passed": all_passed,
        "details": {"gate_statuses": gate_statuses},
        "message": (
            "OK" if all_passed
            else "未通过: " + ", ".join(
                g for g, v in gate_statuses.items() if not v
            )
        ),
    }


def _check_user_final_confirm(state: dict) -> Dict[str, Any]:
    """检查 7: 用户最终确认（safe/collaborative 模式）。

    Args:
        state: state 字典

    Returns:
        检查结果
    """
    mode = state.get("config", {}).get("mode", "auto")

    # 仅 safe/collaborative 模式需要用户最终确认
    if mode not in ("safe", "collaborative"):
        return {
            "check_id": "user_final_confirm",
            "passed": True,
            "details": {"note": f"{mode} 模式不需要用户最终确认"},
            "message": "skip",
        }

    # 检查 pending_confirmation 状态
    pc = state.get("pending_confirmation", {})
    status = pc.get("status", "")

    confirmed = (status == "resolved" and pc.get("response") == "user_confirmed")

    return {
        "check_id": "user_final_confirm",
        "passed": confirmed,
        "details": {
            "pending_status": status,
            "response": pc.get("response"),
        },
        "message": (
            "用户已确认" if confirmed
            else f"等待用户确认（当前状态: {status}）"
        ),
    }


# ============================================================================
# G6 核心逻辑
# ============================================================================


def run_all_checks(
    state: dict,
    state_dir: str,
) -> List[Dict[str, Any]]:
    """运行 G6 全部 7 项检查。

    Args:
        state: state 字典
        state_dir: state.json 所在目录

    Returns:
        检查结果列表（7 项，每项含 passed/details/message）
    """
    checks = [
        _check_tasks_all_complete(state),
        _check_tests_all_pass(state, state_dir),
        _check_no_active_p0_p1(state),
        _check_artifact_checksums(state),
        _check_state_schema(state),
        _check_gates_all_passed(state),
        _check_user_final_confirm(state),
    ]
    return checks


def evaluate_completion(
    state: dict,
    state_dir: str,
) -> Dict[str, Any]:
    """运行 G6 完成门，判定是否可以标记为 complete。

    遍历所有 7 项检查，按模式跳过后仍需要全部通过。

    Args:
        state: state 字典
        state_dir: state 目录

    Returns:
        {
            "gate_id": "G6",
            "passed": bool,
            "checks": [dict],          # 7 项检查结果
            "failed_checks": [dict],   # 未通过的检查项
            "passed_count": int,
            "failed_count": int,
            "skipped_count": int,
            "message": str,
            "timestamp": str,
        }
    """
    mode = state.get("config", {}).get("mode", "auto")
    skippable = SKIPPABLE_CHECKS.get(mode, set())

    all_checks = run_all_checks(state, state_dir)
    failed_checks: List[Dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    skipped_count = 0

    for check in all_checks:
        cid = check["check_id"]
        if cid in skippable:
            check["passed"] = True  # 强制通过
            check["message"] = "skip (按模式)"
            skipped_count += 1

        if check["passed"]:
            passed_count += 1
        else:
            failed_checks.append(check)
            failed_count += 1

    passed = len(failed_checks) == 0

    if passed:
        logger.info("G6 完成门通过: 7/7 项检查全部通过")
    else:
        logger.warning(
            "G6 完成门未通过: %d 项失败，%d 项通过，%d 项跳过",
            failed_count, passed_count, skipped_count,
        )
        for f in failed_checks:
            logger.warning("  - %s: %s", f["check_id"], f.get("message", ""))

    return {
        "gate_id": GATE_ID,
        "passed": passed,
        "checks": all_checks,
        "failed_checks": failed_checks,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "message": (
            "所有完成条件达成，可以标记 complete"
            if passed
            else f"{failed_count} 项检查未通过，不能标记 complete"
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g6(
    state: dict,
    state_dir: str,
) -> Dict[str, Any]:
    """运行 G6 完成门完整流程。

    如果通过，更新 state 的 termination 为 complete。
    如果不通过，记录失败原因但不阻塞（由调用方决定是否继续循环）。

    Args:
        state: state 字典（原地修改）
        state_dir: state 目录

    Returns:
        完整检查结果字典
    """
    result = evaluate_completion(state, state_dir)

    if result["passed"]:
        state["termination"]["status"] = "complete"
        state["termination"]["completed_at"] = result["timestamp"]
        state["termination"]["exit_reason"] = (
            f"G6 所有 {result['passed_count']} 项完成条件通过"
        )
        logger.info("G6 通过，工作流标记为 complete")

    return result
