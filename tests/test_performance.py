# -*- coding: utf-8 -*-
"""性能基准测试: loop-hermes 核心路径延迟基准。

测试覆盖:
    - Perf-1: 单 phase 执行时间（mock invoke_hermes）
    - Perf-2: 原子写入延迟（10 次采样，均值/中位数/P95）
    - Perf-3: Provider 切换延迟（decide_provider / reset / get_all_states）

所有测试均标记 @pytest.mark.slow，使用 CI 友好的宽松阈值。
使用 time.perf_counter() 进行高精度计时。
"""

import json
import statistics
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.phase_dispatch import build_hermes_prompt, dispatch_phase
from loop_hermes.provider_fallback import (
    ProviderFallbackManager,
    ProviderStatus,
    FallbackAction,
)
from loop_hermes.schemas import validate_state
from loop_hermes.state_machine import (
    DEFAULT_STATE_TEMPLATE,
    atomic_write_state,
    load_or_init_state,
)

# ============================================================================
# 辅助函数
# ============================================================================


def _calc_p95(latencies):
    """计算 P95 延迟（线性插值法）。

    Args:
        latencies: 已排序的延迟列表（秒）。

    Returns:
        P95 延迟值（秒）。
    """
    n = len(latencies)
    k = 0.95 * (n - 1)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return latencies[lo] * (1 - frac) + latencies[hi] * frac


# ============================================================================
# Perf-1: 单 phase 执行时间
# ============================================================================


@pytest.mark.slow
class TestPerfSinglePhaseExecution:
    """Perf-1: 单 phase 执行时间性能基准。

    在 mock invoke_hermes（无真实 LLM 调用）条件下，
    验证 dispatch_phase("init") 和 build_hermes_prompt 的延迟。
    """

    def test_dispatch_phase_init_under_one_second(self):
        """Mock invoke_hermes 下 dispatch_phase("init") 应在 1.0 秒内完成。"""
        mock_result = {
            "success": True,
            "output": "mock perf output",
            "engine": "cli",
            "guardrail_events": [],
            "error": None,
            "guardrail_summary": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = load_or_init_state(tmp)
            with mock.patch(
                "loop_hermes.hermes_client.invoke_hermes",
                return_value=mock_result,
            ):
                t0 = time.perf_counter()
                result = dispatch_phase(state, tmp)
                elapsed = time.perf_counter() - t0
            assert result["status"] == "ok", f"dispatch_phase 返回 status={result.get('status')}"
            assert elapsed < 1.0, (
                f"dispatch_phase(init) 耗时 {elapsed:.4f}s，超过 1.0s 阈值"
            )

    def test_build_hermes_prompt_under_100ms(self):
        """build_hermes_prompt("init") 应在 0.1 秒内完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = load_or_init_state(tmp)
            state["config"]["user_request"] = "性能测试需求"
            t0 = time.perf_counter()
            prompt = build_hermes_prompt("init", state)
            elapsed = time.perf_counter() - t0
            assert len(prompt) > 0, "prompt 不应为空"
            assert elapsed < 0.1, (
                f"build_hermes_prompt 耗时 {elapsed:.4f}s，超过 0.1s 阈值"
            )


# ============================================================================
# Perf-2: 原子写入延迟
# ============================================================================


@pytest.mark.slow
class TestPerfAtomicWriteLatency:
    """Perf-2: 原子写入延迟性能基准。

    对 atomic_write_state() 执行 10 次采样，计算均值/中位数/P95，
    并单独测量 validate_state() 耗时。
    """

    def test_atomic_write_latency_statistics(self):
        """10 次原子写入：均值 < 50ms，P95 < 200ms。"""
        latencies = []
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(10):
                state = deepcopy(DEFAULT_STATE_TEMPLATE)
                t0 = time.perf_counter()
                atomic_write_state(state, tmp)
                latencies.append(time.perf_counter() - t0)
        avg = statistics.mean(latencies)
        med = statistics.median(latencies)
        p95 = _calc_p95(sorted(latencies))
        assert avg < 0.05, (
            f"原子写入平均延迟 {avg*1000:.2f}ms，超过 50ms 阈值"
        )
        assert p95 < 0.2, (
            f"原子写入 P95 延迟 {p95*1000:.2f}ms，超过 200ms 阈值（CI 宽松）"
        )

    def test_validate_state_call_latency(self):
        """validate_state() 单独计时：10 次采样。"""
        latencies = []
        for _ in range(10):
            state = deepcopy(DEFAULT_STATE_TEMPLATE)
            t0 = time.perf_counter()
            validate_state(state)
            latencies.append(time.perf_counter() - t0)
        avg = statistics.mean(latencies)
        med = statistics.median(latencies)
        # jsonschema 校验有一定开销，使用 0.1s 宽松阈值
        assert avg < 0.1, (
            f"validate_state 平均延迟 {avg*1000:.2f}ms，超过 100ms 阈值"
        )


# ============================================================================
# Perf-3: Provider 切换延迟
# ============================================================================


@pytest.mark.slow
class TestPerfProviderSwitchLatency:
    """Perf-3: Provider 切换延迟性能基准。

    测量 decide_provider() 切换延迟、reset() 耗时、
    以及 get_all_states() 调用时间。纯内存操作，预期极低延迟。
    """

    def test_decide_provider_switch_under_10ms(self):
        """降级首个 provider 后，decide_provider 切换应在 10ms 内。"""
        mgr = ProviderFallbackManager(chain=["claude", "openai", "deepseek"])
        # 先将 claude 降级为 DEGRADED
        for _ in range(5):
            mgr.report_failure("claude", "perf degrade error")
        assert mgr.states["claude"].status == ProviderStatus.DEGRADED
        latencies = []
        for _ in range(100):
            mgr.reset()
            for __ in range(5):
                mgr.report_failure("claude", "perf degrade error")
            t0 = time.perf_counter()
            result = mgr.decide_provider()
            latencies.append(time.perf_counter() - t0)
        avg = statistics.mean(latencies)
        # 应切换到 openai
        assert result.provider == "openai", f"未切换到 openai，当前={result.provider}"
        assert result.action == FallbackAction.USE_CURRENT
        assert avg < 0.01, (
            f"decide_provider 切换平均延迟 {avg*1000:.3f}ms，超过 10ms 阈值"
        )

    def test_reset_latency_under_10ms(self):
        """reset() 操作应在 10ms 内完成。"""
        mgr = ProviderFallbackManager(chain=["claude", "openai", "deepseek"])
        latencies = []
        for _ in range(100):
            # 先污染状态再重置
            for __ in range(5):
                mgr.report_failure("claude", "perf error")
            t0 = time.perf_counter()
            mgr.reset()
            latencies.append(time.perf_counter() - t0)
        avg = statistics.mean(latencies)
        # 验证重置后所有 provider 均为 HEALTHY
        for name in mgr.chain:
            assert mgr.states[name].status == ProviderStatus.HEALTHY
        assert avg < 0.01, (
            f"reset 平均延迟 {avg*1000:.3f}ms，超过 10ms 阈值"
        )

    def test_get_all_states_latency(self):
        """get_all_states() 调用时间测量（3 个 provider）。"""
        mgr = ProviderFallbackManager(chain=["claude", "openai", "deepseek"])
        latencies = []
        for _ in range(100):
            t0 = time.perf_counter()
            states = mgr.get_all_states()
            latencies.append(time.perf_counter() - t0)
        avg = statistics.mean(latencies)
        assert len(states) == 3, f"应返回 3 个 provider 状态，实际={len(states)}"
        for name in mgr.chain:
            assert name in states
            assert states[name].status == ProviderStatus.HEALTHY
        assert avg < 0.01, (
            f"get_all_states 平均延迟 {avg*1000:.3f}ms，超过 10ms 阈值"
        )
