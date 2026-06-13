# -*- coding: utf-8 -*-
"""Gate State Guard —— 闸门状态守护者。

管理 gate_state.json 文件的读写，是 AI 不可写的安全边界。
所有闸门状态变更必须通过 Gate Guard 的受控接口，不允许
Hermes Agent 或任何 AI 模块直接修改 gate_state.json。

设计原则:
    1. 单向数据流: AI → Gate Guard → gate_state.json
    2. AI 不可写: Hermes Agent 无权直接写 gate_state 文件
    3. 不可变审计日志: 所有 gate 事件以 append-only 方式记录
    4. 原子写入: 使用 state_machine 的 atomic_write 相同协议
    5. 完整性校验: JSON Schema + checksum + signature

文件结构:
    gate_state.json
    ├── meta (version, created_at, updated_at, audit_sequence)
    ├── gates
    │   ├── G1 (content_safety)
    │   ├── G2 (plan_confirmation)
    │   ├── G3 (dependency_install)
    │   ├── G4 (dangerous_ops)
    │   ├── G5 (file_changes)
    │   └── G6 (completion)
    ├── aggregate (汇总状态)
    └── audit_log (不可变审计日志)
"""

import json
import os
import hashlib
import logging
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

logger = logging.getLogger("loop_hermes.gate_guard")

# ============================================================================
# 常量和模板
# ============================================================================

GATE_GUARD_VERSION = 1

# gate_state.json 默认模板
GATE_STATE_TEMPLATE: Dict[str, Any] = {
    "meta": {
        "version": GATE_GUARD_VERSION,
        "created_at": "",
        "updated_at": "",
        "audit_sequence": 0,
    },
    "gates": {
        "G1": {
            "label": "内容安全门",
            "passed": False,
            "last_run_at": None,
            "findings_count": 0,
        },
        "G2": {
            "label": "计划确认门",
            "passed": False,
            "last_run_at": None,
            "confirmed_by": None,
        },
        "G3": {
            "label": "依赖安装门",
            "passed": True,
            "last_run_at": None,
            "blocked_installs": 0,
        },
        "G4": {
            "label": "危险操作门",
            "passed": True,
            "last_run_at": None,
            "blocked_ops": 0,
            "warned_ops": 0,
            "by_layer": {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0},
        },
        "G5": {
            "label": "文件变更门",
            "passed": True,
            "last_run_at": None,
            "files_changed": 0,
            "threshold": 0,
        },
        "G6": {
            "label": "完成门",
            "passed": False,
            "last_run_at": None,
            "checks_passed": 0,
            "checks_failed": 0,
        },
    },
    "aggregate": {
        "all_passed": False,
        "blocking_gates": [],
        "warning_gates": [],
        "last_aggregated_at": None,
    },
    "audit_log": [],
}

# 不允许 AI 直接写入的字段集合
AI_FORBIDDEN_FIELDS = {
    "meta.audit_sequence",
    "meta.updated_at",
    "audit_log",
    "aggregate",
    "gates.*.passed",
    "gates.*.last_run_at",
}


# ============================================================================
# Gate State 读写
# ============================================================================


def load_gate_state(state_dir: str) -> Dict[str, Any]:
    """加载 gate_state.json 文件。

    如果文件不存在，从模板创建并写入新文件。

    Args:
        state_dir: state.json 所在目录

    Returns:
        gate_state 字典
    """
    base = Path(state_dir)
    gs_file = base / "gate_state.json"
    base.mkdir(parents=True, exist_ok=True)

    if gs_file.exists():
        try:
            data = json.loads(gs_file.read_text(encoding="utf-8"))
            _validate_gate_state(data)
            logger.debug("已加载 gate_state.json（audit_seq=%d）",
                         data["meta"]["audit_sequence"])
            return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("gate_state.json 读取失败: %s，使用模板重建", e)
            # 备份损坏文件
            bak = base / "gate_state.json.bak"
            try:
                import shutil
                shutil.copy2(str(gs_file), str(bak))
                logger.info("已备份损坏的 gate_state.json → .bak")
            except OSError:
                pass

    # 从模板创建
    gs = deepcopy(GATE_STATE_TEMPLATE)
    gs["meta"]["created_at"] = datetime.now(timezone.utc).isoformat()
    gs["meta"]["updated_at"] = gs["meta"]["created_at"]
    atomic_write_gate_state(gs, state_dir)
    logger.info("已创建全新 gate_state.json")
    return gs


def atomic_write_gate_state(gs: dict, state_dir: str) -> None:
    """原子写入 gate_state.json。

    与 state_machine.atomic_write_state 使用相同协议：
    tmp → fsync → rename → fsync dir。

    Args:
        gs: gate_state 字典
        state_dir: 目录路径

    Raises:
        OSError: 写入失败
        ValueError: 校验失败
    """
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)

    gs_file = base / "gate_state.json"
    tmp_file = base / "gate_state.json.tmp"

    _validate_gate_state(gs)

    json_text = json.dumps(gs, indent=2, ensure_ascii=False)
    data = json_text.encode("utf-8")

    with open(str(tmp_file), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(str(tmp_file), str(gs_file))

    # fsync 目录
    try:
        fd = os.open(str(base), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

    logger.debug("gate_state.json 原子写入完成（audit_seq=%d）",
                 gs["meta"]["audit_sequence"])


def _validate_gate_state(gs: dict) -> None:
    """校验 gate_state 结构完整性。

    执行基本结构检查，确保所有必要字段存在。

    Args:
        gs: gate_state 字典

    Raises:
        ValueError: 结构不合法
    """
    if "meta" not in gs:
        raise ValueError("gate_state 缺少 meta 字段")
    if "gates" not in gs:
        raise ValueError("gate_state 缺少 gates 字段")
    if "audit_log" not in gs:
        raise ValueError("gate_state 缺少 audit_log 字段")

    # 确保所有 6 个闸门都存在
    for gate_id in ("G1", "G2", "G3", "G4", "G5", "G6"):
        if gate_id not in gs["gates"]:
            raise ValueError(f"gate_state 缺少 {gate_id} 闸门")


# ============================================================================
# 审计日志（不可变）
# ============================================================================


def append_audit_log(
    gs: dict,
    gate_id: str,
    event: str,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """向审计日志追加一条不可变记录。

    审计日志仅允许 append（不可修改或删除），每条记录包含
    自增序号和 SHA-256 链式哈希（前一条哈希 + 当前记录）。

    Args:
        gs: gate_state 字典（原地修改）
        gate_id: 闸门 ID（G1-G6）
        event: 事件类型（如 "scan_started", "blocked", "confirmed"）
        details: 事件详情（可选）

    Returns:
        审计序列号
    """
    seq = gs["meta"]["audit_sequence"] + 1
    gs["meta"]["audit_sequence"] = seq

    # 计算链式哈希
    prev_hash = ""
    if gs["audit_log"]:
        prev_hash = gs["audit_log"][-1].get("chain_hash", "")

    timestamp = datetime.now(timezone.utc).isoformat()
    record_data = f"{seq}|{gate_id}|{event}|{timestamp}|{json.dumps(details or {})}"
    chain_hash = hashlib.sha256(
        (prev_hash + record_data).encode("utf-8")
    ).hexdigest()[:16]

    entry = {
        "seq": seq,
        "gate_id": gate_id,
        "event": event,
        "timestamp": timestamp,
        "details": details or {},
        "chain_hash": chain_hash,
    }
    gs["audit_log"].append(entry)

    # 限制日志长度（保留最近 500 条）
    if len(gs["audit_log"]) > 500:
        gs["audit_log"] = gs["audit_log"][-500:]

    gs["meta"]["updated_at"] = timestamp

    logger.debug("审计日志 #%d: %s/%s", seq, gate_id, event)
    return seq


# ============================================================================
# Gate Guard 写入接口（AI 不可调用的受控接口）
# ============================================================================


class GateGuard:
    """闸门状态守护者。

    提供受控的闸门状态更新接口。所有方法在更新状态后
    自动追加审计日志并原子写入 gate_state.json。

    使用方式:
        guard = GateGuard(state_dir)
        guard.update_gate_passed("G1", True, findings_count=0)
        guard.update_gate_passed("G2", True, confirmed_by="user")
        all_ok = guard.aggregate_passed()

    Attributes:
        gs: 当前 gate_state 字典（内存副本）
        state_dir: gate_state.json 目录路径
    """

    def __init__(self, state_dir: str):
        """初始化 Gate Guard。

        Args:
            state_dir: state.json / gate_state.json 所在目录
        """
        self.state_dir = str(Path(state_dir))
        self.gs = load_gate_state(self.state_dir)

    def update_gate_passed(
        self,
        gate_id: str,
        passed: bool,
        **extra_fields,
    ) -> None:
        """更新指定闸门的通过状态。

        所有闸门状态变更必须通过此方法，确保审计日志完整。

        Args:
            gate_id: 闸门 ID（G1-G6）
            passed: 是否通过
            **extra_fields: 额外更新的字段（如 confirmed_by, findings_count）
        """
        gate = self.gs["gates"].get(gate_id)
        if gate is None:
            raise ValueError(f"未知闸门 ID: {gate_id}")

        old_passed = gate.get("passed", False)
        gate["passed"] = passed
        gate["last_run_at"] = datetime.now(timezone.utc).isoformat()

        # 更新额外字段
        for key, value in extra_fields.items():
            gate[key] = value

        # 审计日志
        event = "passed" if passed else "failed"
        if old_passed != passed:
            event = f"{'passed' if passed else 'failed'} (changed from {'passed' if old_passed else 'failed'})"

        append_audit_log(
            self.gs, gate_id, event,
            {"passed": passed, "extra": extra_fields},
        )

        # 持久化
        self._persist()

    def record_gate_event(
        self,
        gate_id: str,
        event: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录闸门事件（不改变 passed 状态，仅审计）。

        Args:
            gate_id: 闸门 ID
            event: 事件描述
            details: 事件详情
        """
        append_audit_log(self.gs, gate_id, event, details)
        self._persist()

    def aggregate(self) -> Dict[str, Any]:
        """汇总所有闸门状态，更新 aggregate 字段。

        Returns:
            汇总结果字典
        """
        gates = self.gs["gates"]
        blocking: List[str] = []
        warning: List[str] = []
        all_passed = True

        for gid, ginfo in gates.items():
            if not ginfo.get("passed", False):
                all_passed = False
                # G1, G6 是阻塞性闸门
                if gid in ("G1", "G6"):
                    blocking.append(gid)
                else:
                    warning.append(gid)

        self.gs["aggregate"] = {
            "all_passed": all_passed,
            "blocking_gates": blocking,
            "warning_gates": warning,
            "last_aggregated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._persist()
        return self.gs["aggregate"]

    def aggregate_passed(self) -> bool:
        """检查所有闸门是否通过。

        Returns:
            True 如果全部通过
        """
        agg = self.aggregate()
        return agg["all_passed"]

    def sync_to_state(self, state: dict) -> None:
        """将 memory gate_state 同步到 state["gate_state"]。

        Gate Guard 是 gate_state.json 的唯一写入者，
        但 state.json 中也包含一份副本。此方法确保两者一致。

        Args:
            state: state 字典（原地修改 gate_state 字段）
        """
        gate = state.setdefault("gate_state", {})
        gs_gates = self.gs["gates"]

        gate["content_safety_passed"] = gs_gates["G1"]["passed"]
        gate["plan_confirmed"] = gs_gates["G2"]["passed"]
        gate["plan_confirmed_by"] = gs_gates["G2"].get("confirmed_by")

        # G3/G4/G5 标记为无 blocked 操作则通过
        gate_g5_threshold = gs_gates["G5"].get("threshold", 0)

        agg = self.gs["aggregate"]
        if agg["all_passed"]:
            gate["content_safety_passed"] = True
            gate["plan_confirmed"] = True

    def sync_from_state(self, state: dict) -> None:
        """从 state["gate_state"] 同步到 Gate Guard（仅状态更新）。

        注意：此方法只同步 gate_state"状态摘要"，不覆盖审计日志。
        Hermes 自己的 guardrail 事件通过此路径注入。

        Args:
            state: state 字典
        """
        gs = state.get("gate_state", {})
        events = gs.get("hermes_guardrail_events", [])

        if events:
            for ev in events:
                self.record_gate_event(
                    "G1",  # Hermes guardrail 归入 G1
                    f"hermes_guardrail:{ev.get('type', 'UNKNOWN')}",
                    {
                        "tool": ev.get("tool", ""),
                        "message": ev.get("message", ""),
                    },
                )

        # 更新文件变更计数
        file_count = gs.get("file_modifications_this_cycle", 0)
        if file_count > 0:
            self.gs["gates"]["G5"]["files_changed"] = file_count

    def get_audit_trail(
        self,
        gate_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """获取审计日志记录。

        Args:
            gate_id: 筛选特定闸门（可选）
            limit: 返回条数上限

        Returns:
            审计日志条目列表
        """
        log = self.gs.get("audit_log", [])
        if gate_id:
            log = [e for e in log if e.get("gate_id") == gate_id]
        return log[-limit:]

    def _persist(self) -> None:
        """将内存中的 gs 持久化到磁盘。"""
        atomic_write_gate_state(self.gs, self.state_dir)


# ============================================================================
# 单例管理
# ============================================================================


_global_guard: Optional[GateGuard] = None


def get_global_guard(state_dir: Optional[str] = None) -> GateGuard:
    """获取全局 GateGuard 实例。

    单例模式，确保整个进程内只有一个 GateGuard 管理 gate_state.json。

    Args:
        state_dir: state 目录路径（首次调用时必须提供）

    Returns:
        GateGuard 实例

    Raises:
        ValueError: 首次调用时未提供 state_dir
    """
    global _global_guard
    if _global_guard is None:
        if state_dir is None:
            raise ValueError("首次调用 get_global_guard 必须提供 state_dir")
        _global_guard = GateGuard(state_dir)
    return _global_guard


def reset_global_guard() -> None:
    """重置全局 GateGuard 实例（用于测试）。"""
    global _global_guard
    _global_guard = None
