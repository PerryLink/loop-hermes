# -*- coding: utf-8 -*-
"""测试: scheduler.py —— HLOOP_STATE 解析、终止判定、调度器逻辑。"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.scheduler import (
    parse_hloop_state,
    should_terminate,
    is_termination_condition_met,
    generate_cron_entry,
    generate_schtasks_command,
    generate_launchd_plist,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE


class TestParseHloopState:

    def test_parse_simple_block(self):
        """解析标准 key: value 格式的 HLOOP_STATE block。"""
        sample = (
            "<<<HLOOP_STATE>>>\n"
            "phase: part_2_3\n"
            "cycle: 2\n"
            "convergence_counter: 1\n"
            "new_issues_this_round: false\n"
            "issues_active_p0: 0\n"
            "issues_active_p1: 1\n"
            "issues_active_p2: 2\n"
            "all_test_status: pass\n"
            "all_issue_status: none_open\n"
            "pending_confirmation_status: null\n"
            "termination_status: running\n"
            "max_cycles: 5\n"
            "convergence_rounds: 2\n"
            "hermes_guardrail_hardlines: 0\n"
            "hermes_guardrail_warns: 0\n"
            "<<<END_HLOOP_STATE>>>"
        )
        result = parse_hloop_state(sample)
        assert result["phase"] == "part_2_3"
        assert result["cycle"] == 2
        assert result["convergence_counter"] == 1
        assert result["new_issues_this_round"] is False
        assert result["issues_active_p0"] == 0
        assert result["issues_active_p1"] == 1
        assert result["pending_confirmation_status"] is None
        assert result["max_cycles"] == 5

    def test_parse_json_mode_block(self):
        """解析 JSON 格式的 HLOOP_STATE block。"""
        payload = {
            "phase": "part_2_1", "cycle": 1, "convergence_counter": 0,
            "termination_status": "running", "convergence_rounds": 2,
            "max_cycles": 5,
        }
        sample = (
            "<<<HLOOP_STATE>>>\n"
            + json.dumps(payload) +
            "\n<<<END_HLOOP_STATE>>>"
        )
        result = parse_hloop_state(sample)
        assert result["phase"] == "part_2_1"
        assert result["cycle"] == 1

    def test_parse_empty_returns_dict(self):
        """无 HLOOP_STATE block 时返回空字典。"""
        result = parse_hloop_state("just some random output")
        assert result == {}

    def test_parse_type_conversion(self):
        """数字和布尔值应正确转换类型。"""
        sample = (
            "<<<HLOOP_STATE>>>\n"
            "cycle: 42\n"
            "new_issues_this_round: true\n"
            "termination_status: failed\n"
            "<<<END_HLOOP_STATE>>>"
        )
        result = parse_hloop_state(sample)
        assert result["cycle"] == 42
        assert isinstance(result["cycle"], int)
        assert result["new_issues_this_round"] is True


class TestShouldTerminate:

    def test_complete_status_stops(self):
        """termination_status=complete → 停止。"""
        hstate = {"termination_status": "complete"}
        stop, reason = should_terminate(hstate)
        assert stop
        assert "complete" in reason.lower()

    def test_paused_status_stops(self):
        """termination_status=paused → 停止。"""
        hstate = {"termination_status": "paused"}
        stop, reason = should_terminate(hstate)
        assert stop

    def test_failed_status_stops(self):
        """termination_status=failed → 停止。"""
        hstate = {"termination_status": "failed"}
        stop, reason = should_terminate(hstate)
        assert stop

    def test_convergence_stops(self):
        """收敛达成 → 停止。"""
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 0, "issues_active_p1": 0, "issues_active_p2": 0,
            "convergence_counter": 2, "convergence_rounds": 2,
            "cycle": 2, "max_cycles": 5,
        }
        stop, reason = should_terminate(hstate)
        assert stop
        assert "convergence" in reason.lower()

    def test_max_cycles_stops(self):
        """达到 max_cycles → 停止。"""
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 0, "issues_active_p1": 1, "issues_active_p2": 0,
            "convergence_counter": 0, "convergence_rounds": 2,
            "cycle": 5, "max_cycles": 5,
        }
        stop, reason = should_terminate(hstate)
        assert stop
        assert "max cycles" in reason.lower()

    def test_running_continues(self):
        """正常运行中 → 继续。"""
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 0, "issues_active_p1": 0, "issues_active_p2": 0,
            "convergence_counter": 0, "convergence_rounds": 2,
            "cycle": 1, "max_cycles": 5,
        }
        stop, _ = should_terminate(hstate)
        assert not stop

    def test_issues_block_convergence(self):
        """有活跃 issue 时应阻止收敛。"""
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 0, "issues_active_p1": 1, "issues_active_p2": 0,
            "convergence_counter": 2, "convergence_rounds": 2,
            "cycle": 2, "max_cycles": 5,
        }
        stop, _ = should_terminate(hstate)
        assert not stop  # 有 issue 不应收敛


class TestIsTerminationConditionMet:

    def test_terminated_state(self):
        """已终止 state 应被检测到。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["termination"]["status"] = "paused"
        stop, reason = is_termination_condition_met(state)
        assert stop

    def test_converged_state(self):
        """收敛 state 应被检测到。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["config"]["convergence_rounds"] = 2
        state["progress"]["convergence_counter"] = 2
        state["progress"]["cycle"] = 2
        # 所有 issue 为空（默认）
        stop, reason = is_termination_condition_met(state)
        assert stop

    def test_running_continues(self):
        """运行中 state 应继续。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        stop, _ = is_termination_condition_met(state)
        assert not stop


class TestSchedulerConfigGeneration:

    def test_generate_cron_entry(self):
        """生成 crontab 配置条目。"""
        entry = generate_cron_entry(".hermes/loop-hermes", 5)
        assert "*/5" in entry
        assert ".hermes/loop-hermes" in entry
        assert "--no-pause" in entry

    def test_generate_schtasks_command(self):
        """生成 Windows Task Scheduler 命令。"""
        cmd = generate_schtasks_command(".hermes/loop-hermes", 5)
        assert "schtasks" in cmd
        assert "loop-hermes-scheduler" in cmd
        assert ".hermes/loop-hermes" in cmd

    def test_generate_launchd_plist(self):
        """生成 macOS launchd plist。"""
        plist = generate_launchd_plist(".hermes/loop-hermes", 300)
        assert "com.loop-hermes.scheduler" in plist
        assert "StartInterval" in plist
        assert "300" in plist
