# -*- coding: utf-8 -*-
"""测试: gate_guard.py —— Gate State Guard。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_guard import (
    GateGuard, load_gate_state, atomic_write_gate_state,
    append_audit_log, get_global_guard, reset_global_guard,
    GATE_GUARD_VERSION, GATE_STATE_TEMPLATE,
)


class TestLoadGateState:
    """gate_state.json 加载测试。"""

    def test_load_creates_new_file(self):
        """无文件时应创建全新 gate_state.json。"""
        with tempfile.TemporaryDirectory() as td:
            gs = load_gate_state(td)
            assert gs["meta"]["version"] == GATE_GUARD_VERSION
            assert "G1" in gs["gates"]
            assert "G6" in gs["gates"]
            assert Path(td, "gate_state.json").exists()

    def test_load_existing_file(self):
        """已有文件时应正确加载。"""
        with tempfile.TemporaryDirectory() as td:
            gs1 = load_gate_state(td)
            gs1["gates"]["G1"]["passed"] = True
            atomic_write_gate_state(gs1, td)

            gs2 = load_gate_state(td)
            assert gs2["gates"]["G1"]["passed"] is True

    def test_load_corrupted_file_recreates(self):
        """损坏文件应从模板重建。"""
        with tempfile.TemporaryDirectory() as td:
            gs_file = Path(td) / "gate_state.json"
            gs_file.write_text("not valid json {{{")

            gs = load_gate_state(td)
            assert gs["meta"]["version"] == GATE_GUARD_VERSION


class TestAppendAuditLog:
    """审计日志追加测试。"""

    def test_append_increments_sequence(self):
        """追加审计日志应递增序列号。"""
        with tempfile.TemporaryDirectory() as td:
            gs = load_gate_state(td)
            seq1 = append_audit_log(gs, "G1", "test_start")
            assert seq1 == 1
            assert gs["meta"]["audit_sequence"] == 1

            seq2 = append_audit_log(gs, "G1", "test_end")
            assert seq2 == 2

    def test_audit_entry_has_required_fields(self):
        """审计条目应有必要字段。"""
        with tempfile.TemporaryDirectory() as td:
            gs = load_gate_state(td)
            append_audit_log(gs, "G1", "scan_completed",
                             {"findings": 0})
            entry = gs["audit_log"][0]
            assert "seq" in entry
            assert "gate_id" in entry
            assert "event" in entry
            assert "timestamp" in entry
            assert "chain_hash" in entry

    def test_audit_log_truncation(self):
        """审计日志应限制在 500 条。"""
        with tempfile.TemporaryDirectory() as td:
            gs = load_gate_state(td)
            for i in range(550):
                append_audit_log(gs, "G1", f"event_{i}")
            assert len(gs["audit_log"]) <= 500


class TestGateGuard:
    """GateGuard 类测试。"""

    def test_init_loads_gate_state(self):
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            assert guard.gs["meta"]["version"] == GATE_GUARD_VERSION

    def test_update_gate_passed(self):
        """更新闸门通过状态。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            guard.update_gate_passed("G1", True, findings_count=0)
            assert guard.gs["gates"]["G1"]["passed"] is True
            assert guard.gs["gates"]["G1"]["findings_count"] == 0

    def test_update_gate_passed_adds_audit(self):
        """更新闸门状态应追加审计日志。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            initial_log_len = len(guard.gs["audit_log"])
            guard.update_gate_passed("G2", True, confirmed_by="user")
            assert len(guard.gs["audit_log"]) > initial_log_len

    def test_record_gate_event(self):
        """记录闸门事件。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            guard.record_gate_event("G4", "command_blocked",
                                    {"command": "rm -rf /"})
            assert len(guard.gs["audit_log"]) > 0

    def test_aggregate_all_passed(self):
        """所有闸门通过时 aggregate 应为 all_passed。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            for gid in ("G1", "G2", "G3", "G4", "G5", "G6"):
                guard.update_gate_passed(gid, True)
            agg = guard.aggregate()
            assert agg["all_passed"] is True
            assert len(agg["blocking_gates"]) == 0

    def test_aggregate_with_blocking(self):
        """有阻塞闸门时 aggregate 应标记。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            for gid in ("G2", "G3", "G4", "G5"):
                guard.update_gate_passed(gid, True)
            # G1, G6 是阻塞性闸门
            guard.update_gate_passed("G1", False)
            guard.update_gate_passed("G6", True)
            agg = guard.aggregate()
            assert agg["all_passed"] is False
            assert "G1" in agg["blocking_gates"]

    def test_sync_to_state(self):
        """sync_to_state 应正确同步到 state dictionary。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            guard.update_gate_passed("G1", True)
            guard.update_gate_passed("G2", True, confirmed_by="user")

            state = {}
            guard.sync_to_state(state)
            assert state["gate_state"]["content_safety_passed"] is True
            assert state["gate_state"]["plan_confirmed"] is True
            assert state["gate_state"]["plan_confirmed_by"] == "user"

    def test_sync_from_state(self):
        """sync_from_state 应正确从 state 同步事件。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            state = {
                "gate_state": {
                    "hermes_guardrail_events": [
                        {
                            "type": "WARN",
                            "tool": "shell_call",
                            "message": "test warning",
                            "timestamp": "2026-01-01T00:00:00Z",
                        },
                    ],
                    "file_modifications_this_cycle": 5,
                },
            }
            guard.sync_from_state(state)
            assert guard.gs["gates"]["G5"]["files_changed"] == 5

    def test_get_audit_trail(self):
        """获取审计记录。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            guard.record_gate_event("G1", "event1")
            guard.record_gate_event("G2", "event2")
            guard.record_gate_event("G1", "event3")

            trail = guard.get_audit_trail(gate_id="G1")
            assert len(trail) == 2
            assert all(e["gate_id"] == "G1" for e in trail)

    def test_get_audit_trail_with_limit(self):
        """审计记录应尊重 limit 参数。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            for i in range(20):
                guard.record_gate_event("G1", f"event_{i}")
            trail = guard.get_audit_trail(limit=5)
            assert len(trail) <= 5

    def test_aggregate_passed_convenience(self):
        """aggregate_passed() 应返回布尔值。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            for gid in ("G1", "G2", "G3", "G4", "G5", "G6"):
                guard.update_gate_passed(gid, True)
            assert guard.aggregate_passed() is True

    def test_persist_after_update(self):
        """更新闸门后应持久化到磁盘。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            guard.update_gate_passed("G1", True)

            # 重新加载验证持久化
            gs2 = load_gate_state(td)
            assert gs2["gates"]["G1"]["passed"] is True

    def test_invalid_gate_id_raises(self):
        """无效闸门 ID 应抛出 ValueError。"""
        with tempfile.TemporaryDirectory() as td:
            guard = GateGuard(td)
            try:
                guard.update_gate_passed("G99", True)
                assert False, "应抛出 ValueError"
            except ValueError:
                pass


class TestGlobalGuard:
    """全局 GateGuard 单例测试。"""

    def setup_method(self):
        reset_global_guard()

    def teardown_method(self):
        reset_global_guard()

    def test_get_global_guard_creates_singleton(self):
        with tempfile.TemporaryDirectory() as td:
            g1 = get_global_guard(td)
            g2 = get_global_guard()
            assert g1 is g2

    def test_get_global_guard_requires_state_dir_first_time(self):
        reset_global_guard()
        try:
            get_global_guard()
            assert False, "应抛出 ValueError"
        except ValueError:
            pass

    def test_reset_global_guard(self):
        with tempfile.TemporaryDirectory() as td:
            g1 = get_global_guard(td)
            reset_global_guard()
            g2 = get_global_guard(td)
            assert g1 is not g2
