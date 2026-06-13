# -*- coding: utf-8 -*-
r"""状态机核心模块。

管理 state.json 文件的全生命周期:
    - 首次初始化（从 DEFAULT_STATE_TEMPLATE 克隆）
    - 原子写入协议（tmp → fsync → rename → fsync dir）
    - .lock 文件管理（僵尸锁自动清理，超时 300s）
    - 自动备份（原子写入前自动复制 state.json → state.json.bak）
    - 损坏恢复（state.json 不可读时从 .bak 恢复）
    - Schema 版本兼容性检测（仅支持 v1）
    - Default-FAIL 合约：任何异常导致 state.json 写入失败时不吞错
    - checksum 协议 3 层：artifact files → SHA-256 → state.json 记录

并发安全:
    使用文件级锁（.lock 文件），每次写入前获取锁。
    锁文件记录当前进程 PID，超时 5 分钟自动清理僵尸锁。
"""

import json
import os
import time
import shutil
import hashlib
import logging
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from .schemas import validate_state

logger = logging.getLogger("loop_hermes.state_machine")

# ============================================================================
# 常量定义
# ============================================================================

# 僵尸锁清理阈值（秒）
LOCK_ZOMBIE_TIMEOUT = 300

# 当前支持的 schema_version
SUPPORTED_SCHEMA_VERSION = 1


# ============================================================================
# DEFAULT_STATE_TEMPLATE —— state.json 默认模板
# ============================================================================

DEFAULT_STATE_TEMPLATE: Dict[str, Any] = {
    "schema_version": 1,
    "progress": {
        "phase": "init",
        "cycle": 0,
        "convergence_counter": 0,
        "part1_round": 0,
        "new_issues_this_round": False,
        "new_issues_last_round": False,
        "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0},
        "retry_count_this_phase": 0,
        "verification_pass_count": 0,
        "hermes_engine": "unknown",
        "repair_context": None,
        "phase_transitions": [],
    },
    "config": {
        "mode": "auto",
        "skip_testing": False,
        "max_cycles": 5,
        "max_part1_rounds": 5,
        "convergence_rounds": 2,
        "route_repeat_max": 3,
        "user_request": "",
        "provider_fallback_chain": ["claude", "openai", "deepseek"],
        "hermes_model": "claude-sonnet-4-20250514",
        "hermes_toolsets": ["code", "shell"],
        "hermes_commit_pin": "",
        "gate_file_count_threshold": {"safe": 3, "auto": 10, "unsafe": 999},
        "gate_irreversible_ops_blocked_in": ["safe", "auto"],
    },
    "tasks": {
        "total": 0,
        "by_status": {
            "completed": 0,
            "in_progress": 0,
            "pending": 0,
            "failed": 0,
            "skipped": 0,
        },
    },
    "issues": {
        "active": {"p0": [], "p1": [], "p2": []},
        "resolved": {"p0": 0, "p1": 0, "p2": 0},
        "all_time": {"p0_total": 0, "p1_total": 0, "p2_total": 0},
    },
    "artifacts": {
        "requirements": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "direction": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "solution": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "impl_plan": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "task_list": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "implementation_diff": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "code_review": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "test_plan": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "test_results": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "issue_list": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "verification": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
        "context_summary": {
            "path": "", "status": "not_generated",
            "generated_at": None, "generated_in_phase": None,
            "checksum": None, "version": 0,
        },
    },
    "routing_history": [],
    "routing_repeat_tracker": {},
    "gate_state": {
        "content_safety_passed": False,
        "plan_confirmed": False,
        "plan_confirmed_by": None,
        "file_modifications_this_cycle": 0,
        "dangerous_ops_blocked": [],
        "hermes_guardrail_events": [],
    },
    "termination": {
        "status": "running",
        "completed_at": None,
        "exit_reason": None,
    },
    "pending_confirmation": {
        "id": None,
        "status": None,
        "phase": None,
        "context": None,
        "options": [],
        "created_at": None,
        "timeout_minutes": 30,
        "timeout_action": "auto_degrade",
        "response": None,
        "resolved_at": None,
        "attempt": 0,
    },
    "phase_contracts": {
        "active_phase": "init",
        "declared_at": "",
        "contracts": {},
    },
    "context_snapshot": {
        "last_action": "",
        "key_decisions": [],
        "narrative_1k": "",
    },
    "housekeeping": {
        "invocation_count": 0,
        "total_tokens_estimated": 0,
        "lock_file": "",
    },
}

# ============================================================================
# 模式映射（CLI 标志 → config.mode）
# ============================================================================

_MODE_FLAG_MAP = [
    ("safe", "safe"),
    ("unsafe", "unsafe"),
    ("interactive", "collaborative"),
]


# ============================================================================
# Artifact 文件路径映射
# ============================================================================

ARTIFACT_FILE_MAP = {
    "requirements": "01-requirements.md",
    "direction": "02-direction.md",
    "solution": "03-solution.md",
    "impl_plan": "04-implementation-plan.md",
    "task_list": "05-task-list.json",
    "implementation_diff": "05b-implementation-diff.patch",
    "code_review": "06-code-review.md",
    "test_plan": "07-test-plan.md",
    "test_results": "08-test-results.json",
    "issue_list": "09-issue-list.json",
    "verification": "10-verification.md",
    "context_summary": "context-summary.md",
}


# ============================================================================
# state.json 读写函数
# ============================================================================

def load_or_init_state(state_dir: str, args=None) -> dict:
    """加载 state.json 或从模板初始化全新 state。

    加载优先级:
        1. state.json 存在且合法 → 返回
        2. state.json 存在但不合法 → 从 state.json.bak 恢复
        3. 均不存在 → 从 DEFAULT_STATE_TEMPLATE 克隆并原子写入

    Args:
        state_dir: state.json 所在目录路径
        args: CLI 参数对象（可选），含 mode/goal/max_cycles 等字段

    Returns:
        完整的 state 字典

    Raises:
        ValueError: state.json 损坏且 .bak 也不可用
    """
    base = Path(state_dir)
    state_file = base / "state.json"
    lock_file = base / ".lock"

    base.mkdir(parents=True, exist_ok=True)

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            v = state.get("schema_version")
            if v != SUPPORTED_SCHEMA_VERSION:
                raise ValueError(
                    f"不支持的 schema_version: {v}（当前仅支持 v{SUPPORTED_SCHEMA_VERSION}）"
                )
            validate_state(state)
            logger.info("已加载现有 state.json（phase=%s, cycle=%d）",
                        state["progress"]["phase"], state["progress"]["cycle"])
            return state
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("state.json 读取失败: %s，尝试从 .bak 恢复", e)
            restored = restore_from_backup(str(base))
            if restored is not None:
                validate_state(restored)
                logger.info("已从 state.json.bak 恢复")
                # 将恢复的数据写回 state.json
                atomic_write_state(restored, str(base))
                return restored
            raise ValueError(
                f"无法加载 state.json（{e}）且备份文件不可用，"
                f"请手动检查 {state_file} 和 {base / 'state.json.bak'}"
            ) from e

    # 首次初始化
    state = deepcopy(DEFAULT_STATE_TEMPLATE)

    if args is not None:
        _apply_args_to_state(state, args)

    state["housekeeping"]["lock_file"] = str(lock_file)
    _init_artifact_paths(state, base)
    atomic_write_state(state, str(base))
    logger.info("已创建全新 state.json（phase=init）")
    return state


def _apply_args_to_state(state: dict, args) -> None:
    """将 CLI 参数映射到 state 配置字段。

    Args:
        state: state 字典（原地修改）
        args: CLI 参数对象
    """
    cfg = state["config"]

    # 运行模式
    for attr, mode_val in _MODE_FLAG_MAP:
        if getattr(args, attr, False):
            cfg["mode"] = mode_val
            break

    # 用户需求
    goal = getattr(args, "goal", None) or getattr(args, "requirement", None)
    if goal:
        cfg["user_request"] = goal

    # 循环参数
    for arg_name, cfg_key in [
        ("max_cycles", "max_cycles"),
        ("convergence_rounds", "convergence_rounds"),
    ]:
        val = getattr(args, arg_name, None)
        if val is not None:
            cfg[cfg_key] = val

    # Hermes 模型
    model = getattr(args, "hermes_model", None)
    if model:
        cfg["hermes_model"] = model

    # 工具集
    toolsets = getattr(args, "hermes_toolsets", None)
    if toolsets:
        cfg["hermes_toolsets"] = toolsets.split(",") if isinstance(toolsets, str) else toolsets

    # provider 回退链
    provider_fb = getattr(args, "provider_fallback", None)
    if provider_fb:
        cfg["provider_fallback_chain"] = (
            provider_fb.split(",") if isinstance(provider_fb, str) else provider_fb
        )

    # 跳过测试
    if getattr(args, "skip_testing", False):
        cfg["skip_testing"] = True


def _init_artifact_paths(state: dict, base: Path) -> None:
    """初始化 artifacts/ 目录并设置各 artifact 的完整路径。

    Args:
        state: state 字典（原地修改 artifacts 中的 path 字段）
        base: state_dir 的 Path 对象
    """
    artifacts_dir = base / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    for art_key, filename in ARTIFACT_FILE_MAP.items():
        info = state["artifacts"].get(art_key)
        if info is not None:
            info["path"] = str(artifacts_dir / filename)


# ============================================================================
# 并发锁管理
# ============================================================================

def acquire_lock(state_dir: str) -> bool:
    """获取 state.json 写入锁。

    线程/进程安全：通过文件存在性 + PID 记录实现简单的互斥。
    自动清理僵尸锁（超过 LOCK_ZOMBIE_TIMEOUT 秒的过期锁）。

    Args:
        state_dir: state.json 所在目录路径

    Returns:
        True 表示成功获取锁；False 表示锁被其他进程持有
    """
    lock_file = Path(state_dir) / ".lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            age = time.time() - lock_file.stat().st_mtime
            if age > LOCK_ZOMBIE_TIMEOUT:
                logger.warning("检测到僵尸锁（%d 秒），自动清理", int(age))
                lock_file.unlink()
            else:
                return False
        except OSError:
            return False

    try:
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        logger.error("无法创建锁文件 %s", lock_file)
        return False


def release_lock(state_dir: str) -> None:
    """释放 state.json 写入锁。

    Args:
        state_dir: state.json 所在目录路径
    """
    lock_file = Path(state_dir) / ".lock"
    if lock_file.exists():
        try:
            lock_file.unlink()
        except OSError:
            logger.warning("无法释放锁文件 %s", lock_file)


# ============================================================================
# 备份与恢复
# ============================================================================

def backup_state(state_dir: str) -> None:
    """将 state.json 复制为 state.json.bak（原子写入前自动调用）。

    Args:
        state_dir: state.json 所在目录路径
    """
    src = Path(state_dir) / "state.json"
    dst = Path(state_dir) / "state.json.bak"
    if src.exists():
        shutil.copy2(src, dst)
        logger.debug("已备份 state.json → state.json.bak")


def restore_from_backup(state_dir: str) -> Optional[dict]:
    """从 state.json.bak 恢复 state 数据。

    Args:
        state_dir: state.json 所在目录路径

    Returns:
        恢复的 state 字典；备份文件不存在或损坏时返回 None
    """
    bak = Path(state_dir) / "state.json.bak"
    if not bak.exists():
        return None
    try:
        return json.loads(bak.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("备份文件损坏: %s", e)
        return None


# ============================================================================
# 原子写入协议（核心）
# ============================================================================

def atomic_write_state(state: dict, state_dir: str) -> None:
    """原子写入 state.json —— 三层保护协议。

    协议步骤:
        1. Schema 校验（validate_state）—— Default-FAIL 合约：校验失败直接抛异常
        2. 自动备份 state.json → state.json.bak
        3. 写入 state.json.tmp
        4. fsync tmp 文件（确保落盘）
        5. os.replace(tmp → state.json)（原子重命名）
        6. fsync 目录（确保目录元数据落盘）

    Default-FAIL 合约:
        任何步骤失败均抛出异常（不吞错），确保调用方感知写入失败。
        崩溃安全: 最坏情况下留下 state.json.tmp 残留文件（可安全删除）。

    Args:
        state: 符合 STATE_SCHEMA 的 state 字典
        state_dir: state.json 所在目录路径

    Raises:
        ValueError: Schema 校验失败
        OSError: 文件写入或 fsync 失败
    """
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)

    # 第 1 层：Schema 校验（Default-FAIL）
    validate_state(state)

    state_file = base / "state.json"
    tmp_file = base / "state.json.tmp"

    # 第 2 层：自动备份
    backup_state(str(base))

    # 第 3 层：写入 tmp → fsync → rename → fsync dir
    json_text = json.dumps(state, indent=2, ensure_ascii=False)

    # 以二进制写模式打开 tmp 文件，手动写入 + flush + fsync
    # Windows 上 os.fsync 要求文件描述符以写模式打开
    data = json_text.encode("utf-8")
    with open(str(tmp_file), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    # 原子重命名
    os.replace(str(tmp_file), str(state_file))

    # fsync 目录（确保 rename 落盘）
    # Windows 上目录句柄不支持 fsync，跳过
    try:
        fd = os.open(str(base), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # Windows 不支持目录 fsync

    logger.debug("原子写入完成: %s", state_file)


# ============================================================================
# checksum 协议（3 层）
# ============================================================================

def compute_artifact_checksum(file_path: str) -> str:
    """计算 artifact 文件的 SHA-256 checksum。

    Args:
        file_path: artifact 文件路径

    Returns:
        十六进制 SHA-256 哈希字符串

    Raises:
        FileNotFoundError: 文件不存在
    """
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def update_artifact_meta(state: dict, art_key: str) -> None:
    """更新 state.json 中 artifact 的 checksum、版本、时间戳。

    第 1 层：计算文件 SHA-256 → checksum 字段
    第 2 层：递增 version 计数器
    第 3 层：更新 status / generated_at / generated_in_phase

    Args:
        state: state 字典（原地修改 artifacts 字段）
        art_key: artifact 键名（如 "requirements", "task_list" 等）

    Raises:
        KeyError: art_key 不在 ARTIFACT_FILE_MAP 中
    """
    info = state["artifacts"].get(art_key)
    if info is None:
        raise KeyError(f"未知的 artifact 键: {art_key}")

    file_path = info.get("path", "")
    path = Path(file_path) if file_path else None

    if path and path.exists():
        info["checksum"] = compute_artifact_checksum(file_path)
        info["version"] = info.get("version", 0) + 1
        info["status"] = "generated" if info.get("status") == "not_generated" else "updated"
        info["generated_at"] = datetime.now(timezone.utc).isoformat()
        info["generated_in_phase"] = state["progress"]["phase"]
    else:
        logger.warning("artifact 文件不存在，跳过 checksum 更新: %s", file_path)


def verify_artifact_integrity(state: dict) -> list:
    """校验所有已生成 artifact 的 checksum 完整性。

    第 3 层：对比 state.json 记录的 checksum 与文件实际 SHA-256。
    用于 Sanity Check #15。

    Args:
        state: state 字典

    Returns:
        checksum 不匹配的 artifact 键名列表（空列表表示全部通过）
    """
    mismatches = []
    for art_key, info in state.get("artifacts", {}).items():
        file_path = info.get("path", "")
        recorded_checksum = info.get("checksum")
        if not file_path or not recorded_checksum:
            continue
        path = Path(file_path)
        if not path.exists():
            continue
        actual = compute_artifact_checksum(file_path)
        if actual != recorded_checksum:
            mismatches.append({
                "artifact": art_key,
                "file": file_path,
                "recorded": recorded_checksum,
                "actual": actual,
            })
    return mismatches


# ============================================================================
# 辅助函数
# ============================================================================

def get_state_dir_path(state_dir: str) -> Path:
    """获取标准化的 state 目录 Path 对象。

    Args:
        state_dir: 目录路径字符串

    Returns:
        Path 对象
    """
    return Path(state_dir).resolve()


def state_exists(state_dir: str) -> bool:
    """检查 state.json 是否已初始化。

    Args:
        state_dir: state.json 所在目录路径

    Returns:
        True 如果 state.json 文件存在
    """
    return (Path(state_dir) / "state.json").exists()


def is_terminated(state: dict) -> bool:
    """检查工作流是否已终止（complete / paused / failed）。

    Args:
        state: state 字典

    Returns:
        True 如果 termination.status 不为 "running"
    """
    return state.get("termination", {}).get("status", "running") != "running"


def is_cycle_exceeded(state: dict) -> bool:
    """检查是否超过最大循环轮次。

    Args:
        state: state 字典

    Returns:
        True 如果 cycle >= max_cycles
    """
    return state["progress"]["cycle"] >= state["config"]["max_cycles"]


def is_converged(state: dict) -> bool:
    """检查收敛条件是否满足。

    条件：convergence_counter >= convergence_rounds 且没有活跃 issues。

    Args:
        state: state 字典

    Returns:
        True 如果收敛条件达成
    """
    active = state["issues"]["active"]
    total_active = len(active["p0"]) + len(active["p1"]) + len(active["p2"])
    return (
        state["progress"]["convergence_counter"] >= state["config"]["convergence_rounds"]
        and total_active == 0
    )
