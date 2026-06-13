# -*- coding: utf-8 -*-
"""测试: schemas.py —— JSON Schema 定义与校验函数。"""

import json
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.schemas import (
    STATE_SCHEMA, TASK_LIST_SCHEMA, TEST_RESULTS_SCHEMA, ISSUE_LIST_SCHEMA,
    GATE_STATE_SCHEMA, REPAIR_CONTEXT_SCHEMA,
    validate_state, validate_task_list, validate_test_results,
    validate_issue_list, validate_gate_state, validate_repair_context,
)


def _minimal_state():
    """构造最小合法 state 字典。"""
    return {
        "schema_version": 1,
        "progress": {
            "phase": "init", "cycle": 0, "convergence_counter": 0,
        },
        "config": {"mode": "auto", "user_request": "test"},
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
    }


class TestStateSchema:
    """STATE_SCHEMA 校验测试。"""

    def test_valid_minimal_state_passes(self):
        """合法的最小 state 应通过校验。"""
        validate_state(_minimal_state())

    def test_invalid_state_missing_required_raises(self):
        """缺少必需字段应抛出 ValueError。"""
        with pytest.raises(ValueError):
            validate_state({"schema_version": 1})

    def test_state_schema_version_mismatch_raises(self):
        """不兼容的 schema_version 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            validate_state({
                "schema_version": 99,
                "progress": {}, "config": {},
                "tasks": {}, "issues": {}, "termination": {},
            })

    def test_invalid_mode_enum_raises(self):
        """非法 mode 枚举值应抛出 ValueError。"""
        state = _minimal_state()
        state["config"]["mode"] = "invalid_mode"
        with pytest.raises(ValueError):
            validate_state(state)

    def test_invalid_termination_status_raises(self):
        """非法 termination.status 应抛出 ValueError。"""
        state = _minimal_state()
        state["termination"]["status"] = "unknown_status"
        with pytest.raises(ValueError):
            validate_state(state)


class TestTaskListSchema:
    """TASK_LIST_SCHEMA 校验测试。"""

    def _minimal_task_list(self):
        return {
            "meta": {
                "project": "test",
                "generated_by_phase": "part_2_1",
                "generated_at": "2026-06-13T00:00:00Z",
            },
            "tasks": [
                {
                    "id": "task-01", "title": "test task",
                    "status": "pending", "priority": 1,
                    "module": "core", "assigned_files": ["a.py"],
                    "dependencies": [],
                }
            ],
            "summary": {"total": 0, "by_status": {}},
        }

    def test_valid_task_list_passes(self):
        """合法任务列表应通过校验。"""
        validate_task_list(self._minimal_task_list())

    def test_missing_meta_raises(self):
        """缺少 meta 应抛出 ValueError。"""
        data = self._minimal_task_list()
        del data["meta"]
        with pytest.raises(ValueError):
            validate_task_list(data)


class TestTestResultsSchema:
    """TEST_RESULTS_SCHEMA 校验测试。"""

    def _minimal_results(self):
        return {
            "meta": {
                "generated_by_phase": "part_2_6",
                "generated_at": "2026-06-13T00:00:00Z",
            },
            "results": [
                {"id": "t-1", "name": "test_foo", "status": "pass"},
            ],
            "summary": {},
            "promoted_issues": [],
        }

    def test_valid_results_passes(self):
        """合法测试结果应通过校验。"""
        validate_test_results(self._minimal_results())

    def test_invalid_status_raises(self):
        """非法 status 应抛出 ValueError。"""
        data = self._minimal_results()
        data["results"][0]["status"] = "unknown"
        with pytest.raises(ValueError):
            validate_test_results(data)


class TestIssueListSchema:
    """ISSUE_LIST_SCHEMA 校验测试。"""

    def _minimal_issues(self):
        return {
            "meta": {
                "generated_by_phase": "part_2_7",
                "generated_at": "2026-06-13T00:00:00Z",
            },
            "issues": [
                {
                    "id": "issue-001", "severity": "P1",
                    "title": "test issue", "source": "code_review",
                    "status": "open",
                }
            ],
            "summary": {},
        }

    def test_valid_issue_list_passes(self):
        """合法问题清单应通过校验。"""
        validate_issue_list(self._minimal_issues())

    def test_invalid_severity_raises(self):
        """非法 severity 应抛出 ValueError。"""
        data = self._minimal_issues()
        data["issues"][0]["severity"] = "P99"
        with pytest.raises(ValueError):
            validate_issue_list(data)


class TestGateStateSchema:
    """GATE_STATE_SCHEMA 校验测试。"""

    def _valid_gate(self):
        return {
            "content_safety_passed": False,
            "plan_confirmed": False,
            "file_modifications_this_cycle": 0,
            "dangerous_ops_blocked": [],
            "hermes_guardrail_events": [],
        }

    def test_valid_gate_state_passes(self):
        """合法闸门状态应通过校验。"""
        validate_gate_state(self._valid_gate())

    def test_missing_field_raises(self):
        """缺少必需字段应抛出 ValueError。"""
        data = self._valid_gate()
        del data["content_safety_passed"]
        with pytest.raises(ValueError):
            validate_gate_state(data)


class TestRepairContextSchema:
    """REPAIR_CONTEXT_SCHEMA 校验测试。"""

    def _valid_repair(self):
        return {
            "from_phase": "routing",
            "routing_reason": "P2 issue",
            "target_issues": ["issue-001"],
            "affected_files": ["src/main.py"],
        }

    def test_valid_repair_context_passes(self):
        """合法修复上下文应通过校验。"""
        validate_repair_context(self._valid_repair())

    def test_missing_required_raises(self):
        """缺少必需字段应抛出 ValueError。"""
        data = self._valid_repair()
        del data["affected_files"]
        with pytest.raises(ValueError):
            validate_repair_context(data)
