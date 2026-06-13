# -*- coding: utf-8 -*-
"""测试: sanity_check.py —— 15 项启动检查。"""

import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.sanity_check import (
    run_sanity_check, SANITY_CHECKS,
    get_failed_checks, get_blocking_failures,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE, atomic_write_state


class TestSanityCheckCount:

    def test_has_15_checks(self):
        """应恰好包含 15 项检查。"""
        assert len(SANITY_CHECKS) == 15, f"期望 15 项，实际 {len(SANITY_CHECKS)}"
        ids = [c["id"] for c in SANITY_CHECKS]
        for i in range(1, 16):
            assert i in ids, f"检查 #{i} 缺失"


class TestSanityCheckResults:

    def test_all_15_results_returned(self):
        """应返回恰好 15 条结果。"""
        with tempfile.TemporaryDirectory() as tmp:
            results = run_sanity_check(tmp)
            assert len(results) == 15

    def test_results_are_structured(self):
        """每条结果应包含 check_id/name/passed 字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            results = run_sanity_check(tmp)
            for r in results:
                assert "check_id" in r
                assert "name" in r
                assert "passed" in r
                if not r["passed"]:
                    assert "error" in r or r["check_id"] == 10  # 10 总是 pass

    def test_python_version_check_passes(self):
        """Python 版本检查应该通过（我们要求 >= 3.10）。"""
        with tempfile.TemporaryDirectory() as tmp:
            results = run_sanity_check(tmp)
            check1 = next(r for r in results if r["check_id"] == 1)
            assert check1["passed"], f"Python 版本检查失败: {sys.version}"

    def test_state_dir_creation_succeeds(self):
        """state_dir 应能被自动创建。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            results = run_sanity_check(str(state_dir))
            check3 = next(r for r in results if r["check_id"] == 3)
            assert check3["passed"]
            assert state_dir.exists()


class TestSanityCheckWithState:

    def test_loads_existing_state(self):
        """已有 state.json 时应正确检测配置项。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            atomic_write_state(state, str(state_dir))

            results = run_sanity_check(str(state_dir))
            # 检查 5: state.json 合法
            check5 = next(r for r in results if r["check_id"] == 5)
            assert check5["passed"]

            # 检查 11: config.mode 合法
            check11 = next(r for r in results if r["check_id"] == 11)
            assert check11["passed"], f"mode 检查失败: {check11.get('error')}"

    def test_mode_invalid_detected(self):
        """非法的 config.mode 应被检测到。
        注意：atomic_write_state 会做 Schema 校验并拒绝非法 mode，
        因此需要绕过原子写入直接写盘来模拟损坏的 state.json。
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["config"]["mode"] = "invalid_mode"
            # 绕过 atomic_write 直接写盘（模拟文件损坏/版本回退场景）
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )

            results = run_sanity_check(str(state_dir))
            check11 = next(r for r in results if r["check_id"] == 11)
            assert not check11["passed"]


class TestFiltering:

    def test_get_failed_checks(self):
        """筛选失败检查。"""
        results = [
            {"check_id": 1, "passed": True},
            {"check_id": 2, "passed": False, "error": "fail"},
            {"check_id": 3, "passed": True},
        ]
        failed = get_failed_checks(results)
        assert len(failed) == 1
        assert failed[0]["check_id"] == 2

    def test_get_blocking_failures(self):
        """阻断检查应包括 #1 和 #2。"""
        results = [
            {"check_id": 1, "passed": False, "error": "Python too old"},
            {"check_id": 2, "passed": False, "error": "No Hermes"},
            {"check_id": 3, "passed": False, "error": "No state_dir"},
        ]
        blocking = get_blocking_failures(results)
        assert len(blocking) == 2
        blocking_ids = {b["check_id"] for b in blocking}
        assert blocking_ids == {1, 2}
