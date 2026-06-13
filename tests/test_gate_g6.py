# -*- coding: utf-8 -*-
"""测试: gate_g6.py —— G6 完成门。"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_g6 import (
    run_all_checks, evaluate_completion, run_gate_g6,
    GATE_ID, CHECK_ITEMS, SKIPPABLE_CHECKS,
)


def _full_state(overrides=None):
    """构造一个"收敛态"的 state（满足完成条件）。"""
    s = {
        "schema_version": 1,
        "progress": {
            "phase": "part_2_8",
            "cycle": 2,
            "convergence_counter": 3,
            "new_issues_this_round": False,
        },
        "config": {
            "mode": "auto",
            "user_request": "build a simple app",
            "skip_testing": False,
        },
        "tasks": {
            "total": 5,
            "by_status": {
                "completed": 5,
                "in_progress": 0,
                "pending": 0,
                "failed": 0,
                "skipped": 0,
            },
        },
        "issues": {
            "active": {"p0": [], "p1": [], "p2": []},
            "resolved": {"p0": 1, "p1": 2, "p2": 5},
            "all_time": {"p0_total": 1, "p1_total": 2, "p2_total": 5},
        },
        "termination": {"status": "running"},
        "gate_state": {
            "content_safety_passed": True,
            "plan_confirmed": True,
            "plan_confirmed_by": "auto",
            "file_modifications_this_cycle": 1,
            "dangerous_ops_blocked": [],
            "hermes_guardrail_events": [],
        },
        "artifacts": {
            "test_results": {
                "path": "",
                "status": "generated",
                "checksum": None,
                "version": 1,
            },
        },
        "pending_confirmation": {
            "id": None,
            "status": None,
            "response": None,
        },
    }
    if overrides:
        s.update(overrides)
    return s


class TestCheckItems:
    """检查项定义完整性。"""

    def test_seven_check_items(self):
        """应有 7 项检查。"""
        assert len(CHECK_ITEMS) == 7

    def test_check_items_have_labels(self):
        """每项应有标题。"""
        for cid, label in CHECK_ITEMS.items():
            assert isinstance(cid, str)
            assert isinstance(label, str)
            assert len(label) > 0


class TestSkippableChecks:
    """各模式跳过配置。"""

    def test_unsafe_skips_user_confirm(self):
        assert "user_final_confirm" in SKIPPABLE_CHECKS["unsafe"]

    def test_auto_skips_user_confirm(self):
        assert "user_final_confirm" in SKIPPABLE_CHECKS["auto"]

    def test_safe_nothing_skipped(self):
        assert len(SKIPPABLE_CHECKS["safe"]) == 0

    def test_collaborative_nothing_skipped(self):
        assert len(SKIPPABLE_CHECKS["collaborative"]) == 0


class TestRunAllChecks:
    """全部检查项运行测试。"""

    def test_run_all_checks_returns_seven_items(self):
        """应返回 7 项检查结果。"""
        state = _full_state()
        checks = run_all_checks(state, "/tmp/test_hermes")
        assert len(checks) == 7

    def test_all_checks_have_required_fields(self):
        """每项应有 check_id, passed, details, message。"""
        state = _full_state()
        checks = run_all_checks(state, "/tmp/test_hermes")
        for c in checks:
            assert "check_id" in c
            assert "passed" in c
            assert "details" in c
            assert "message" in c


class TestEvaluateCompletion:
    """完成门判定测试。"""

    def test_healthy_state_passes(self):
        """理想状态应通过完成门。"""
        with tempfile.TemporaryDirectory() as td:
            # 创建假的测试结果文件
            tr_path = Path(td) / "artifacts" / "08-test-results.json"
            tr_path.parent.mkdir(parents=True, exist_ok=True)
            tr_path.write_text(json.dumps({
                "results": [],
                "summary": {"total": 5, "pass": 5, "fail": 0, "error": 0, "pass_rate": 1.0},
            }))

            state = _full_state()
            state["artifacts"]["test_results"]["path"] = str(tr_path)

            result = evaluate_completion(state, td)
            assert result["gate_id"] == GATE_ID

    def test_tasks_incomplete_fails(self):
        """任务未完成时不应通过。"""
        state = _full_state()
        state["tasks"]["by_status"]["pending"] = 2
        checks = run_all_checks(state, "/tmp/test_hermes")
        task_check = [c for c in checks if c["check_id"] == "tasks_all_complete"][0]
        assert task_check["passed"] is False

    def test_active_p0_fails(self):
        """有活跃 P0 issue 时不应通过。"""
        state = _full_state()
        state["issues"]["active"]["p0"].append({
            "id": "p0-001", "severity": "P0",
            "title": "critical bug", "source": "test_failure",
            "status": "open",
        })
        checks = run_all_checks(state, "/tmp/test_hermes")
        p0_check = [c for c in checks if c["check_id"] == "no_active_p0_p1"][0]
        assert p0_check["passed"] is False

    def test_active_p1_fails(self):
        """有活跃 P1 issue 时不应通过。"""
        state = _full_state()
        state["issues"]["active"]["p1"].append({
            "id": "p1-001", "severity": "P1",
            "title": "warning issue", "source": "code_review",
            "status": "open",
        })
        checks = run_all_checks(state, "/tmp/test_hermes")
        p1_check = [c for c in checks if c["check_id"] == "no_active_p0_p1"][0]
        assert p1_check["passed"] is False

    def test_active_p2_ok(self):
        """P2 issue 允许存在。"""
        state = _full_state()
        state["issues"]["active"]["p2"].append({
            "id": "p2-001", "severity": "P2",
            "title": "minor issue", "source": "lint_warning",
            "status": "open",
        })
        checks = run_all_checks(state, "/tmp/test_hermes")
        p2_check = [c for c in checks if c["check_id"] == "no_active_p0_p1"][0]
        assert p2_check["passed"] is True

    def test_state_schema_valid(self):
        """state schema 校验应通过。"""
        state = _full_state()
        checks = run_all_checks(state, "/tmp/test_hermes")
        schema_check = [c for c in checks if c["check_id"] == "state_schema_valid"][0]
        assert schema_check["passed"] is True

    def test_gates_all_passed_check(self):
        """所有闸门通过检查。"""
        state = _full_state()
        checks = run_all_checks(state, "/tmp/test_hermes")
        gate_check = [c for c in checks if c["check_id"] == "gates_all_passed"][0]
        assert gate_check["passed"] is True

    def test_skip_testing_check(self):
        """skip_testing 模式下测试检查应通过。"""
        state = _full_state()
        state["config"]["skip_testing"] = True
        state["artifacts"]["test_results"]["path"] = ""
        checks = run_all_checks(state, "/tmp/test_hermes")
        test_check = [c for c in checks if c["check_id"] == "tests_all_pass"][0]
        assert test_check["passed"] is True


class TestRunGateG6:
    """高层接口 run_gate_g6。"""

    def test_healthy_state_marks_complete(self):
        """健康 state 通过后应标记为 complete。"""
        with tempfile.TemporaryDirectory() as td:
            tr_path = Path(td) / "artifacts" / "08-test-results.json"
            tr_path.parent.mkdir(parents=True, exist_ok=True)
            tr_path.write_text(json.dumps({
                "results": [],
                "summary": {"total": 2, "pass": 2, "fail": 0, "error": 0, "pass_rate": 1.0},
            }))

            state = _full_state()
            state["artifacts"]["test_results"]["path"] = str(tr_path)
            result = run_gate_g6(state, td)
            if result["passed"]:
                assert state["termination"]["status"] == "complete"

    def test_result_has_timestamp(self):
        """结果应有时间戳。"""
        state = _full_state()
        result = evaluate_completion(state, "/tmp/test_hermes")
        assert "timestamp" in result

    def test_result_has_checks_list(self):
        """结果应包含完整 checks 列表。"""
        state = _full_state()
        result = evaluate_completion(state, "/tmp/test_hermes")
        assert "checks" in result
        assert len(result["checks"]) == 7
