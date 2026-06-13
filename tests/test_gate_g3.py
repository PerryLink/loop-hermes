# -*- coding: utf-8 -*-
"""测试: gate_g3.py —— G3 依赖安装门。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.gate_g3 import (
    check_package_name, extract_packages_from_pip_command,
    extract_packages_from_npm_command,
    audit_install_command, inject_g3_issues_into_state,
    run_gate_g3, _levenshtein_distance,
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


class TestLevenshteinDistance:
    """Levenshtein 距离计算。"""

    def test_identical_strings(self):
        assert _levenshtein_distance("abc", "abc") == 0

    def test_one_char_difference(self):
        assert _levenshtein_distance("abc", "abd") == 1

    def test_completely_different(self):
        assert _levenshtein_distance("abc", "xyz") == 3

    def test_empty_string(self):
        assert _levenshtein_distance("", "abc") == 3
        assert _levenshtein_distance("abc", "") == 3

    def test_typosquatting_distance(self):
        """Typo 常见距离为 1-2。"""
        assert _levenshtein_distance("numpy", "nummy") == 1
        assert _levenshtein_distance("pandas", "pandass") == 1


class TestCheckPackageName:
    """包名安全性检测。"""

    def test_trusted_package(self):
        """已知信任包应返回 trusted。"""
        r = check_package_name("numpy", "pypi")
        assert r["trusted"] is True
        assert r["suspicious"] is False

    def test_trusted_npm_package(self):
        """已知信任 npm 包应返回 trusted。"""
        r = check_package_name("react", "npm")
        assert r["trusted"] is True

    def test_unknown_package_not_trusted(self):
        """未知包名不应 trusted。"""
        r = check_package_name("some-random-pkg-xyz", "pypi")
        assert r["trusted"] is False

    def test_typosquatting_detection(self):
        """Typo 包名应被标记为 suspicious。"""
        r = check_package_name("nummy", "pypi")
        assert r["suspicious"] is True
        assert len(r["warnings"]) > 0

    def test_very_short_name(self):
        """过短包名应 suspicious。"""
        r = check_package_name("x", "pypi")
        assert r["suspicious"] is True


class TestExtractPackages:
    """从命令中提取包名。"""

    def test_simple_pip_install(self):
        pkg = extract_packages_from_pip_command("pip install requests numpy")
        assert "requests" in pkg
        assert "numpy" in pkg

    def test_pip_install_with_version(self):
        pkg = extract_packages_from_pip_command("pip install flask>=2.0")
        assert "flask" in pkg

    def test_pip3_install(self):
        pkg = extract_packages_from_pip_command("pip3 install httpx")
        assert "httpx" in pkg

    def test_python_m_pip(self):
        pkg = extract_packages_from_pip_command("python -m pip install rich click")
        assert "rich" in pkg
        assert "click" in pkg

    def test_npm_install(self):
        pkg = extract_packages_from_npm_command("npm install express lodash")
        assert "express" in pkg
        assert "lodash" in pkg


class TestAuditInstallCommand:
    """安装命令审计。"""

    def test_safe_pip_install(self):
        """安全 pip install 应通过。"""
        r = audit_install_command("pip install requests")
        assert r["passed"] is True

    def test_sudo_pip_blocked(self):
        """sudo pip install 在 auto 模式应拦截。"""
        r = audit_install_command("sudo pip install flask")
        assert r["blocked"] is True
        assert r["passed"] is False

    def test_sudo_pip_allowed_unsafe(self):
        """sudo pip install 在 unsafe 模式不拦截。"""
        r = audit_install_command("sudo pip install flask", mode="unsafe")
        assert r["blocked"] is False

    def test_break_system_packages_blocked(self):
        """--break-system-packages 应拦截。"""
        r = audit_install_command("pip install --break-system-packages somepkg")
        assert r["blocked"] is True

    def test_ecosystem_detection(self):
        """应立即识别生态系统。"""
        r = audit_install_command("pip install requests")
        assert r["ecosystem"] == "pypi"

        r2 = audit_install_command("npm install express")
        assert r2["ecosystem"] == "npm"


class TestInjectG3Issues:
    """G3 issue 注入。"""

    def test_blocked_audit_injects_issues(self):
        """被拦截的审计应注入 issue。"""
        state = _minimal_state()
        audit = audit_install_command("sudo pip install flask")
        count = inject_g3_issues_into_state(state, audit)
        assert count > 0

    def test_safe_audit_no_injection(self):
        """安全的审计不注入 issue。"""
        state = _minimal_state()
        audit = audit_install_command("pip install requests")
        count = inject_g3_issues_into_state(state, audit)
        assert count == 0


class TestRunGateG3:
    """高层接口 run_gate_g3。"""

    def test_safe_command_passes(self):
        state = _minimal_state()
        result = run_gate_g3("pip install numpy", state)
        assert result["passed"] is True

    def test_dangerous_command_updates_state(self):
        state = _minimal_state()
        result = run_gate_g3("sudo pip install flask", state)
        assert result["blocked"] is True
        # dangerous_ops_blocked 应被更新
        assert len(state["gate_state"]["dangerous_ops_blocked"]) > 0
