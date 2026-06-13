# -*- coding: utf-8 -*-
"""测试: guardrail_mapper.py —— Hermes Guardrail 映射。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.guardrail_mapper import (
    GUARDRAIL_SEVERITY_MAP,
    GUARDRAIL_ACTION_MAP,
    map_guardrail_to_severity,
    map_guardrail_to_action,
    is_terminating_guardrail,
    guardrail_event_to_issue,
    process_guardrail_events,
    inject_guardrail_issues_into_state,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE


class TestGuardrailSeverityMap:
    """Guardrail 类型→严重性等级映射测试。"""

    def test_hardline_maps_to_p0(self):
        """HARDLINE 应映射为 P0。"""
        assert map_guardrail_to_severity("HARDLINE") == "P0"
        assert map_guardrail_to_severity("HARDLINE_BLOCK") == "P0"

    def test_warn_maps_to_p1(self):
        """WARN 应映射为 P1。"""
        assert map_guardrail_to_severity("WARN") == "P1"
        assert map_guardrail_to_severity("WARN_PATTERN") == "P1"

    def test_approval_maps_to_p2(self):
        """APPROVAL_DENY 应映射为 P2。"""
        assert map_guardrail_to_severity("APPROVAL_DENY") == "P2"
        assert map_guardrail_to_severity("APPROVAL_TIMEOUT") == "P2"

    def test_unknown_maps_to_p2_default(self):
        """未知类型默认映射为 P2。"""
        assert map_guardrail_to_severity("UNKNOWN_TYPE") == "P2"

    def test_empty_severity_default(self):
        """空字符串默认映射为 P2。"""
        assert map_guardrail_to_severity("") == "P2"


class TestGuardrailActionMap:
    """Guardrail 类型→处置动作映射测试。"""

    def test_hardline_action_is_retreat(self):
        """HARDLINE 处置动作应为 RETREAT_TO_PART1。"""
        assert map_guardrail_to_action("HARDLINE") == "RETREAT_TO_PART1"

    def test_hardline_block_action_is_terminate(self):
        """HARDLINE_BLOCK 处置动作应为 TERMINATE。"""
        assert map_guardrail_to_action("HARDLINE_BLOCK") == "TERMINATE"

    def test_warn_action_is_decision_tree(self):
        """WARN 处置动作应为 ROUTE_TO_DECISION_TREE。"""
        assert map_guardrail_to_action("WARN") == "ROUTE_TO_DECISION_TREE"

    def test_approval_action_is_repair(self):
        """APPROVAL_DENY 处置动作应为 REPAIR。"""
        assert map_guardrail_to_action("APPROVAL_DENY") == "REPAIR"

    def test_block_action_is_terminate(self):
        """BLOCK 处置动作应为 TERMINATE。"""
        assert map_guardrail_to_action("BLOCK") == "TERMINATE"


class TestTerminatingGuardrail:
    """终止级 guardrail 检测测试。"""

    def test_block_is_terminating(self):
        """BLOCK 应标记为终止级。"""
        assert is_terminating_guardrail("BLOCK") is True

    def test_hardline_block_is_terminating(self):
        """HARDLINE_BLOCK 应标记为终止级。"""
        assert is_terminating_guardrail("HARDLINE_BLOCK") is True

    def test_hardline_is_not_terminating(self):
        """HARDLINE（非 BLOCK）不应标记为终止级。"""
        assert is_terminating_guardrail("HARDLINE") is False

    def test_warn_is_not_terminating(self):
        """WARN 不应标记为终止级。"""
        assert is_terminating_guardrail("WARN") is False


class TestGuardrailEventToIssue:
    """Guardrail 事件→Issue 转换测试。"""

    def test_converts_hardline_event(self):
        """HARDLINE 事件应生成 P0 issue。"""
        event = {
            "type": "HARDLINE",
            "tool": "shell_call",
            "message": "blocked: rm -rf /",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        issue = guardrail_event_to_issue(event, "part_2_2")
        assert issue["severity"] == "P0"
        assert issue["source"] == "hermes_guardrail"
        assert "HARDLINE" in issue["title"]
        assert issue["status"] == "open"
        assert "P0" in issue["fix_strategy"]

    def test_converts_warn_event(self):
        """WARN 事件应生成 P1 issue。"""
        event = {
            "type": "WARN",
            "tool": "file_write",
            "message": "sensitive file access",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        issue = guardrail_event_to_issue(event, "part_2_2")
        assert issue["severity"] == "P1"
        assert issue["source"] == "hermes_guardrail"

    def test_converts_approval_event(self):
        """APPROVAL_DENY 事件应生成 P2 issue。"""
        event = {
            "type": "APPROVAL_DENY",
            "tool": "http_request",
            "message": "approval required",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        issue = guardrail_event_to_issue(event, "part_2_2")
        assert issue["severity"] == "P2"

    def test_issue_has_unique_id(self):
        """每个 issue 应有唯一 ID。"""
        event = {"type": "WARN", "tool": "test", "message": "test", "timestamp": ""}
        i1 = guardrail_event_to_issue(event, "init")
        i2 = guardrail_event_to_issue(event, "init")
        assert i1["id"] != i2["id"]
        assert i1["id"].startswith("guardrail-")

    def test_empty_event_fields_default(self):
        """空字段应有默认值。"""
        event = {"type": "WARN"}
        issue = guardrail_event_to_issue(event, "init")
        assert issue["severity"] == "P1"
        assert "WARN" in issue["title"]


class TestProcessGuardrailEvents:
    """批量 guardrail 事件处理测试。"""

    def test_empty_events(self):
        """空事件列表返回空结果。"""
        issues, summary = process_guardrail_events([], "init")
        assert len(issues) == 0
        assert summary["total"] == 0

    def test_multiple_events_groups_by_severity(self):
        """多个事件应按严重性分组统计。"""
        events = [
            {"type": "HARDLINE", "tool": "t1", "message": "m1", "timestamp": ""},
            {"type": "WARN", "tool": "t2", "message": "m2", "timestamp": ""},
            {"type": "APPROVAL_DENY", "tool": "t3", "message": "m3", "timestamp": ""},
        ]
        issues, summary = process_guardrail_events(events, "part_2_2")
        assert len(issues) == 3
        assert summary["total"] == 3
        assert summary["by_severity"]["P0"] == 1
        assert summary["by_severity"]["P1"] == 1
        assert summary["by_severity"]["P2"] == 1
        assert summary["terminating"] is False

    def test_terminating_event_detected(self):
        """BLOCK 事件应标记为 terminating。"""
        events = [
            {"type": "BLOCK", "tool": "t1", "message": "m1", "timestamp": ""},
        ]
        issues, summary = process_guardrail_events(events, "init")
        assert summary["terminating"] is True
        assert "TERMINATE" in summary["actions"]

    def test_highest_severity_tracked(self):
        """应跟踪最高严重性等级。"""
        events = [
            {"type": "WARN", "tool": "t1", "message": "m1", "timestamp": ""},
            {"type": "HARDLINE", "tool": "t2", "message": "m2", "timestamp": ""},
            {"type": "APPROVAL_DENY", "tool": "t3", "message": "m3", "timestamp": ""},
        ]
        issues, summary = process_guardrail_events(events, "init")
        assert summary["highest_severity"] == "P0"


class TestInjectGuardrailIssuesIntoState:
    """Guardrail 事件注入 state 测试。"""

    def test_injects_p0_issue_into_state(self):
        """HARDLINE 事件应生成 P0 issue 并写入 state.active.p0。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        events = [
            {"type": "HARDLINE", "tool": "shell_call",
             "message": "blocked", "timestamp": "2026-01-01T00:00:00Z"},
        ]
        result = inject_guardrail_issues_into_state(state, events, "part_2_2")
        assert result["total"] == 1
        assert len(state["issues"]["active"]["p0"]) == 1
        assert state["issues"]["active"]["p0"][0]["severity"] == "P0"
        assert state["issues"]["active"]["p0"][0]["source"] == "hermes_guardrail"

    def test_injects_multiple_severities(self):
        """多个不同严重性事件应分别注入对应列表。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        events = [
            {"type": "HARDLINE", "tool": "t1", "message": "m1", "timestamp": ""},
            {"type": "WARN", "tool": "t2", "message": "m2", "timestamp": ""},
            {"type": "APPROVAL_DENY", "tool": "t3", "message": "m3", "timestamp": ""},
        ]
        inject_guardrail_issues_into_state(state, events, "part_2_2")
        assert len(state["issues"]["active"]["p0"]) == 1
        assert len(state["issues"]["active"]["p1"]) == 1
        assert len(state["issues"]["active"]["p2"]) == 1
        assert state["progress"]["new_issues_this_round"] is True

    def test_records_in_gate_state(self):
        """Guardrail 事件应记录在 gate_state 中。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        events = [
            {"type": "WARN", "tool": "t1", "message": "m1", "timestamp": ""},
        ]
        inject_guardrail_issues_into_state(state, events, "init")
        recorded = state["gate_state"]["hermes_guardrail_events"]
        assert len(recorded) == 1
        assert recorded[0]["type"] == "WARN"

    def test_terminating_event_sets_failed(self):
        """终止级 guardrail 应将 state 标记为 failed。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        events = [
            {"type": "BLOCK", "tool": "t1", "message": "m1", "timestamp": ""},
        ]
        inject_guardrail_issues_into_state(state, events, "init")
        assert state["termination"]["status"] == "failed"
        assert "BLOCK" in state["termination"]["exit_reason"]

    def test_empty_events_no_change(self):
        """空事件列表不应修改 state。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        original = json.dumps(state)
        inject_guardrail_issues_into_state(state, [], "init")
        assert json.dumps(state) == original

    def test_all_time_counter_incremented(self):
        """all_time 计数器应递增。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        events = [
            {"type": "HARDLINE", "tool": "t1", "message": "m1", "timestamp": ""},
        ]
        inject_guardrail_issues_into_state(state, events, "init")
        assert state["issues"]["all_time"]["p0_total"] == 1
