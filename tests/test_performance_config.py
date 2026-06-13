# -*- coding: utf-8 -*-
"""性能基准测试 —— 配置加载、checksum 计算、Schema 校验。

覆盖三个关键性能路径:
    P1: 配置模块导入 + 默认实例化
    P2: checksum 计算 —— 不同文件大小的 SHA-256 吞吐
    P3: jsonschema 校验 —— state.json 校验耗时
"""

import sys
import time
import timeit
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile


# ====================================================================
# 辅助工具
# ====================================================================


def _time_execution_ms(fn, *args, **kwargs):
    """测量单次函数执行时间（毫秒）。"""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return result, elapsed_ms


# ====================================================================
# P1: config 模块性能
# ====================================================================


class TestConfigModulePerformance:
    """配置模块加载和实例化性能基准。"""

    def test_config_module_import_latency(self):
        """config 模块导入应在 200ms 内完成。"""
        start = time.perf_counter()
        from loop_hermes.config import LoopHermesConfig, RunMode  # noqa: F401
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 200, (
            f"config 模块导入耗时 {elapsed_ms:.1f}ms，超过 200ms 阈值"
        )

    def test_config_default_instantiation(self):
        """默认配置实例化应在 10ms 内完成。"""
        from loop_hermes.config import LoopHermesConfig
        _, elapsed = _time_execution_ms(LoopHermesConfig)
        assert elapsed < 10, (
            f"LoopHermesConfig 默认构造耗时 {elapsed:.1f}ms，超过 10ms 阈值"
        )

    def test_config_full_instantiation(self):
        """完整参数配置实例化应在 10ms 内完成。"""
        from loop_hermes.config import LoopHermesConfig
        _, elapsed = _time_execution_ms(
            LoopHermesConfig,
            mode="collaborative",
            user_request="build a web app",
            state_dir="/tmp/test",
            max_cycles=10,
            convergence_rounds=3,
            max_part1_rounds=7,
            route_repeat_max=5,
            hermes_model="claude-sonnet-4-20250514",
            hermes_toolsets=["code", "shell", "browser"],
            hermes_commit_pin="abc123def",
            provider_fallback_chain=["claude", "openai"],
            skip_testing=True,
        )
        assert elapsed < 10, (
            f"LoopHermesConfig 完整构造耗时 {elapsed:.1f}ms，超过 10ms 阈值"
        )

    def test_get_env_api_key_performance(self):
        """get_env_api_key 调用应在 1ms 内完成。"""
        from loop_hermes.config import get_env_api_key
        _, elapsed = _time_execution_ms(get_env_api_key, "claude")
        assert elapsed < 1, (
            f"get_env_api_key 耗时 {elapsed:.1f}ms，超过 1ms 阈值"
        )

    def test_get_available_providers_performance(self):
        """get_available_providers 调用应在 2ms 内完成。"""
        from loop_hermes.config import get_available_providers
        _, elapsed = _time_execution_ms(get_available_providers)
        assert elapsed < 2, (
            f"get_available_providers 耗时 {elapsed:.1f}ms，超过 2ms 阈值"
        )

    def test_run_mode_class_operations(self):
        """RunMode 类方法调用应在 0.5ms 内完成。"""
        from loop_hermes.config import RunMode
        t0 = time.perf_counter()
        assert RunMode.is_valid("auto") is True
        assert RunMode.is_valid("invalid") is False
        assert RunMode.default() == "auto"
        elapsed = (time.perf_counter() - t0) * 1000
        assert elapsed < 0.5, (
            f"RunMode 操作耗时 {elapsed:.1f}ms，超过 0.5ms 阈值"
        )


# ====================================================================
# P2: checksum 计算性能
# ====================================================================


class TestChecksumPerformance:
    """SHA-256 checksum 计算在各种文件大小下的性能基准。"""

    def test_checksum_small_file_1kb(self):
        """1KB 文件 checksum 计算应在 5ms 内。"""
        from loop_hermes.checksum import compute_checksum, compute_checksum_from_content

        content = "Hello loop-hermes\n" * 50  # ~1KB
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            # 预热
            compute_checksum(tmp_path)
            # 测量
            _, elapsed = _time_execution_ms(compute_checksum, tmp_path)
            assert elapsed < 5, (
                f"1KB 文件 checksum 耗时 {elapsed:.1f}ms，超过 5ms 阈值"
            )

            # 内容 checksum
            _, elapsed2 = _time_execution_ms(compute_checksum_from_content, content)
            assert elapsed2 < 5, (
                f"1KB 内容 checksum 耗时 {elapsed2:.1f}ms，超过 5ms 阈值"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_checksum_medium_file_100kb(self):
        """100KB 文件 checksum 计算应在 20ms 内。"""
        from loop_hermes.checksum import compute_checksum

        content = "x" * 100 * 1024  # 100KB
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dat", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            # 预热
            compute_checksum(tmp_path)
            # 测量
            _, elapsed = _time_execution_ms(compute_checksum, tmp_path)
            assert elapsed < 20, (
                f"100KB 文件 checksum 耗时 {elapsed:.1f}ms，超过 20ms 阈值"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_checksum_large_file_1mb(self):
        """1MB 文件 checksum 计算应在 100ms 内。"""
        from loop_hermes.checksum import compute_checksum

        content = "y" * 1024 * 1024  # 1MB
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dat", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            # 预热
            compute_checksum(tmp_path)
            # 测量
            _, elapsed = _time_execution_ms(compute_checksum, tmp_path)
            assert elapsed < 100, (
                f"1MB 文件 checksum 耗时 {elapsed:.1f}ms，超过 100ms 阈值"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_checksum_deterministic(self):
        """同一文件多次计算应产生一致的 checksum。"""
        from loop_hermes.checksum import compute_checksum

        content = "Deterministic checksum test data.\n" * 100
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            results = [compute_checksum(tmp_path) for _ in range(5)]
            assert all(r == results[0] for r in results), (
                "同一文件的 checksum 多次计算结果不一致"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_checksum_empty_file(self):
        """空文件 checksum 计算应在 5ms 内。"""
        from loop_hermes.checksum import compute_checksum

        with tempfile.NamedTemporaryFile(mode="w", suffix=".empty", delete=False) as f:
            f.write("")
            tmp_path = f.name

        try:
            _, elapsed = _time_execution_ms(compute_checksum, tmp_path)
            assert elapsed < 5, (
                f"空文件 checksum 耗时 {elapsed:.1f}ms，超过 5ms 阈值"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_compute_checksum_from_content_consistency(self):
        """compute_checksum_from_content 与 compute_checksum 对相同内容结果一致。"""
        from loop_hermes.checksum import compute_checksum, compute_checksum_from_content

        content = "Consistency check\n" * 50
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name

        try:
            cs_file = compute_checksum(tmp_path)
            cs_content = compute_checksum_from_content(content)
            assert cs_file == cs_content, (
                f"compute_checksum 和 compute_checksum_from_content 结果不一致: "
                f"{cs_file[:16]}... vs {cs_content[:16]}..."
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ====================================================================
# P3: Schema 校验性能
# ====================================================================


class TestSchemaValidationPerformance:
    """jsonschema 校验在各种 state 尺寸下的性能基准。"""

    @pytest.fixture
    def base_state(self):
        """返回一个有效的基准 state 字典。"""
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE
        return deepcopy(DEFAULT_STATE_TEMPLATE)

    def test_validate_state_empty_benchmark(self, base_state):
        """最小 state（无 issues/tasks）校验应在 50ms 内。"""
        from loop_hermes.schemas import validate_state

        state = deepcopy(base_state)
        state["progress"]["phase"] = "init"
        state["progress"]["cycle"] = 0

        # 预热
        validate_state(state)
        # 测量
        _, elapsed = _time_execution_ms(validate_state, state)
        assert elapsed < 50, (
            f"最小 state 校验耗时 {elapsed:.1f}ms，超过 50ms 阈值"
        )

    def test_validate_state_with_10_p0_issues(self, base_state):
        """10 个 P0 issue 的 state 校验应在 100ms 内。"""
        from loop_hermes.schemas import validate_state

        state = deepcopy(base_state)
        state["progress"]["phase"] = "part_2_7"
        state["progress"]["cycle"] = 2
        for i in range(10):
            state["issues"]["active"]["p0"].append({
                "id": f"issue-{i:03d}",
                "severity": "P0",
                "title": f"Critical bug #{i}",
                "source": "test_failure",
                "status": "open",
            })
        state["issues"]["all_time"]["p0_total"] = 10

        # 预热
        validate_state(state)
        # 测量
        _, elapsed = _time_execution_ms(validate_state, state)
        assert elapsed < 100, (
            f"10-issue state 校验耗时 {elapsed:.1f}ms，超过 100ms 阈值"
        )

    def test_validate_state_with_20_p1_issues(self, base_state):
        """20 个 P1 issue 的 state 校验应在 150ms 内。"""
        from loop_hermes.schemas import validate_state

        state = deepcopy(base_state)
        state["progress"]["phase"] = "part_2_7"
        state["progress"]["cycle"] = 3
        for i in range(20):
            state["issues"]["active"]["p1"].append({
                "id": f"warn-{i:03d}",
                "severity": "P1",
                "title": f"Warning #{i}",
                "source": "code_review",
                "status": "open",
            })
        state["issues"]["all_time"]["p1_total"] = 20

        # 预热
        validate_state(state)
        # 测量
        _, elapsed = _time_execution_ms(validate_state, state)
        assert elapsed < 150, (
            f"20-issue state 校验耗时 {elapsed:.1f}ms，超过 150ms 阈值"
        )

    def test_validate_task_list_benchmark(self):
        """10 个 task 的 task_list 校验应在 50ms 内。"""
        from loop_hermes.schemas import validate_task_list

        data = {
            "meta": {
                "project": "test",
                "generated_by_phase": "part_2_1",
                "generated_at": "2025-01-01T00:00:00Z",
                "version": 1,
                "total_estimated_hours": 8.0,
            },
            "tasks": [
                {
                    "id": f"t-{i:03d}",
                    "title": f"Task {i}",
                    "description": f"Description for task {i}",
                    "status": "pending",
                    "priority": i + 1,
                    "module": f"module_{i % 3}",
                    "assigned_files": [f"src/module_{i}.py"],
                    "dependencies": [f"t-{j:03d}" for j in range(max(0, i - 2), i)],
                }
                for i in range(10)
            ],
            "summary": {
                "total": 10,
                "by_status": {
                    "completed": 0,
                    "in_progress": 0,
                    "pending": 10,
                    "failed": 0,
                    "skipped": 0,
                },
            },
        }

        # 预热
        validate_task_list(data)
        # 测量
        _, elapsed = _time_execution_ms(validate_task_list, data)
        assert elapsed < 50, (
            f"task_list 校验耗时 {elapsed:.1f}ms，超过 50ms 阈值"
        )

    def test_validate_test_results_benchmark(self):
        """100 条测试结果的校验应在 100ms 内。"""
        from loop_hermes.schemas import validate_test_results

        data = {
            "meta": {
                "generated_by_phase": "part_2_6",
                "generated_at": "2025-01-01T00:00:00Z",
                "test_framework": "pytest",
                "total_duration_ms": 5000,
            },
            "results": [
                {
                    "id": f"tr-{i:03d}",
                    "name": f"test_case_{i}",
                    "status": "pass" if i < 85 else "fail" if i < 95 else "skip",
                    "duration_ms": i * 2,
                    "error_message": "" if i < 85 else f"Assertion error #{i}",
                    "module": f"tests.test_module_{i % 5}",
                    "linked_task_id": f"t-{i % 10:03d}",
                }
                for i in range(100)
            ],
            "summary": {
                "total": 100,
                "pass": 85,
                "fail": 10,
                "skip": 5,
                "error": 0,
                "pass_rate": 0.85,
            },
            "promoted_issues": [],
        }

        # 预热
        validate_test_results(data)
        # 测量
        _, elapsed = _time_execution_ms(validate_test_results, data)
        assert elapsed < 100, (
            f"test_results 校验耗时 {elapsed:.1f}ms，超过 100ms 阈值"
        )

    def test_validate_issue_list_benchmark(self):
        """30 条 issue 清单的校验应在 100ms 内。"""
        from loop_hermes.schemas import validate_issue_list

        data = {
            "meta": {
                "generated_by_phase": "part_2_7",
                "generated_at": "2025-01-01T00:00:00Z",
                "version": 1,
            },
            "issues": [
                {
                    "id": f"iss-{i:03d}",
                    "severity": ["P0", "P1", "P2"][i % 3],
                    "title": f"Issue title {i}",
                    "description": f"Detailed description for issue {i}",
                    "source": ["test_failure", "code_review", "lint_warning"][i % 3],
                    "status": ["open", "in_progress", "fixed"][i % 3],
                    "affected_files": [f"src/file_{i % 5}.py"],
                    "linked_task_ids": [f"t-{i:03d}"],
                    "fix_strategy": "Fix the bug",
                    "discovered_in_phase": "part_2_7",
                    "source_ref": f"ref-{i:03d}",
                }
                for i in range(30)
            ],
            "summary": {
                "total": 30,
                "by_severity": {"p0": 10, "p1": 10, "p2": 10},
                "by_status": {"open": 10, "in_progress": 10, "fixed": 10, "verified": 0},
            },
        }

        # 预热
        validate_issue_list(data)
        # 测量
        _, elapsed = _time_execution_ms(validate_issue_list, data)
        assert elapsed < 100, (
            f"issue_list 校验耗时 {elapsed:.1f}ms，超过 100ms 阈值"
        )

    def test_validate_gate_state_benchmark(self):
        """gate_state 校验应在 20ms 内。"""
        from loop_hermes.schemas import validate_gate_state

        data = {
            "content_safety_passed": True,
            "plan_confirmed": True,
            "plan_confirmed_by": "user",
            "file_modifications_this_cycle": 5,
            "dangerous_ops_blocked": [
                {
                    "operation": "rm -rf /",
                    "reason": "irreversible",
                    "blocked_at": "2025-01-01T00:00:00Z",
                }
            ],
            "hermes_guardrail_events": [
                {
                    "type": "WARN",
                    "tool": "Bash",
                    "message": "high file count",
                    "timestamp": "2025-01-01T00:00:00Z",
                }
            ],
        }

        # 预热
        validate_gate_state(data)
        # 测量
        _, elapsed = _time_execution_ms(validate_gate_state, data)
        assert elapsed < 20, (
            f"gate_state 校验耗时 {elapsed:.1f}ms，超过 20ms 阈值"
        )

    def test_validate_repair_context_benchmark(self):
        """repair_context 校验应在 10ms 内。"""
        from loop_hermes.schemas import validate_repair_context

        data = {
            "from_phase": "routing",
            "routing_reason": "P1 implementation-level fix needed",
            "target_issues": ["iss-001", "iss-002", "iss-003"],
            "repair_plan": "Fix the type errors in module_a.py",
            "attempt_number": 1,
            "review_required": True,
            "affected_files": ["src/module_a.py", "tests/test_module_a.py"],
            "hermes_guardrail_source": False,
        }

        # 预热
        validate_repair_context(data)
        # 测量
        _, elapsed = _time_execution_ms(validate_repair_context, data)
        assert elapsed < 10, (
            f"repair_context 校验耗时 {elapsed:.1f}ms，超过 10ms 阈值"
        )

    def test_schema_validation_consistency(self, base_state):
        """批量连续校验 50 次，验证性能稳定性（无内存泄漏/退化）。"""
        from loop_hermes.schemas import validate_state

        state = deepcopy(base_state)
        state["progress"]["phase"] = "part_2_1"
        state["progress"]["cycle"] = 1

        # 循环外预热
        validate_state(state)

        times = []
        for _ in range(50):
            s = deepcopy(state)
            _, elapsed = _time_execution_ms(validate_state, s)
            times.append(elapsed)

        avg = sum(times) / len(times)
        max_time = max(times)

        # 平均耗时应在 30ms 内
        assert avg < 30, (
            f"50 次 state 校验平均耗时 {avg:.1f}ms，超过 30ms 阈值"
        )
        # 最大耗时不应超过平均的 3 倍（排除异常 spike）
        assert max_time < avg * 3, (
            f"state 校验最大耗时 {max_time:.1f}ms 是平均值 {avg:.1f}ms 的 "
            f"{max_time / avg:.1f}x，可能存在性能退化"
        )
