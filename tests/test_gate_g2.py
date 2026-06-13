# -*- coding: utf-8 -*-
"""测试: gate_g2.py —— G2 计划确认门。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_g2 import (
    check_plan_confirmed, auto_confirm, should_confirm,
    run_gate_g2, MODE_TIMEOUTS, GATE_ID,
)


def _state(mode="auto", phase="part_1_3", plan_confirmed=False):
    """构造测试用 state。"""
    return {
        "schema_version": 1,
        "progress": {"phase": phase, "cycle": 0, "convergence_counter": 0},
        "config": {"mode": mode, "user_request": "test"},
        "tasks": {
            "total": 0,
            "by_status": {
                "completed": 0, "in_progress": 0,
                "pending": 0, "failed": 0, "skipped": 0,
            },
        },
        "issues": {
            "active": {"p0": [], "p1": [], "p2": []},
            "resolved": {"p0": 0, "p1": 0, "p2": 0},
            "all_time": {"p0_total": 0, "p1_total": 0, "p2_total": 0},
        },
        "termination": {"status": "running"},
        "gate_state": {
            "content_safety_passed": True,
            "plan_confirmed": plan_confirmed,
            "plan_confirmed_by": None,
            "file_modifications_this_cycle": 0,
            "dangerous_ops_blocked": [],
            "hermes_guardrail_events": [],
        },
        "pending_confirmation": {
            "id": None, "status": None, "phase": None,
            "context": None, "options": [],
            "created_at": None, "timeout_minutes": 30,
            "timeout_action": "auto_degrade",
            "response": None, "resolved_at": None, "attempt": 0,
        },
    }


class TestCheckPlanConfirmed:
    """检查 plan_confirmed 状态。"""

    def test_not_confirmed_initially(self):
        """初始状态应为未确认。"""
        s = _state()
        assert check_plan_confirmed(s) is False

    def test_confirmed_after_auto(self):
        """自动确认后应变为 True。"""
        s = _state()
        result = auto_confirm(s)
        assert result["confirmed"] is True
        assert check_plan_confirmed(s) is True

    def test_confirmed_by_auto_sets_user_field(self):
        """自动确认应设置 confirmed_by = "auto"。"""
        s = _state()
        auto_confirm(s)
        assert s["gate_state"]["plan_confirmed_by"] == "auto"


class TestAutoConfirm:
    """自动确认测试。"""

    def test_auto_confirm_returns_correct_structure(self):
        """返回结果应有完整字段。"""
        s = _state()
        result = auto_confirm(s)
        assert result["gate_id"] == GATE_ID
        assert result["confirmed"] is True
        assert result["confirmed_by"] == "auto"
        assert "timestamp" in result

    def test_auto_confirm_cleans_pending(self):
        """自动确认后 pending_confirmation 应被清理。"""
        s = _state()
        auto_confirm(s)
        assert s["pending_confirmation"]["status"] == "resolved"
        assert s["pending_confirmation"]["response"] == "auto_confirmed"


class TestShouldConfirm:
    """确认触发条件判断。"""

    def test_auto_mode_already_confirmed_returns_false(self):
        """auto 模式已确认不应再确认。"""
        s = _state(mode="auto", plan_confirmed=True)
        assert should_confirm(s) is False

    def test_auto_mode_not_confirmed_returns_true(self):
        """auto 模式未确认在 part_1_3 应确认。"""
        s = _state(mode="auto", phase="part_1_3")
        assert should_confirm(s) is True

    def test_safe_mode_should_confirm_in_part_1_3(self):
        """safe 模式在 part_1_3 应确认。"""
        s = _state(mode="safe", phase="part_1_3")
        assert should_confirm(s) is True

    def test_unsafe_mode_never_confirms(self):
        """unsafe 模式永远不需要确认。"""
        s = _state(mode="unsafe", phase="part_1_3")
        assert should_confirm(s) is False

    def test_init_phase_no_confirm(self):
        """init phase 不需要确认。"""
        s = _state(mode="auto", phase="init")
        assert should_confirm(s) is False

    def test_part_2_after_confirm_no_confirm(self):
        """Part 2 且已确认则不再确认。"""
        s = _state(mode="auto", phase="part_2_1", plan_confirmed=True)
        assert should_confirm(s) is False


class TestRunGateG2:
    """高层接口 run_gate_g2 测试。"""

    def test_auto_mode_auto_confirm(self):
        """auto 模式自动确认。"""
        s = _state(mode="auto", phase="part_1_3")
        result = run_gate_g2(s)
        assert result["confirmed"] is True
        assert result["confirmed_by"] in ("auto", "skipped")

    def test_unsafe_mode_skips(self):
        """unsafe 模式跳过确认。"""
        s = _state(mode="unsafe", phase="part_1_3")
        result = run_gate_g2(s)
        assert result["confirmed"] is True

    def test_already_confirmed_skips(self):
        """已确认的情况下跳过。"""
        s = _state(mode="safe", phase="part_2_1", plan_confirmed=True)
        result = run_gate_g2(s)
        assert result["confirmed"] is True

    def test_not_in_confirm_phase_skips(self):
        """非确认 phase 跳过。"""
        s = _state(mode="safe", phase="init")
        result = run_gate_g2(s)
        # should_confirm returns False → skipped
        assert result["confirmed"] is True
        assert result.get("confirmed_by") == "skipped"


class TestModeTimeouts:
    """超时配置验证。"""

    def test_safe_timeout_is_reasonable(self):
        """safe 模式超时应为 180s（3 分钟）。"""
        assert MODE_TIMEOUTS["safe"] == 180

    def test_collaborative_timeout_is_reasonable(self):
        """collaborative 模式超时应为 1800s（30 分钟）。"""
        assert MODE_TIMEOUTS["collaborative"] == 1800

    def test_auto_timeout_is_zero(self):
        """auto 模式超时应为 0（立即自动确认）。"""
        assert MODE_TIMEOUTS["auto"] == 0

    def test_unsafe_timeout_is_zero(self):
        """unsafe 模式超时应为 0（跳过）。"""
        assert MODE_TIMEOUTS["unsafe"] == 0
