# -*- coding: utf-8 -*-
"""测试: provider_fallback.py —— Provider 回退管理器。"""

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.provider_fallback import (
    ProviderStatus,
    FallbackAction,
    ProviderState,
    FallbackResult,
    ProviderFallbackManager,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_PROBE_INTERVAL_SECONDS,
    create_provider_fallback_manager,
    get_global_fallback_manager,
)


class TestProviderFallbackManagerInit:
    """初始化测试。"""

    def test_default_chain(self):
        """默认回退链应为 claude→openai→deepseek。"""
        mgr = ProviderFallbackManager()
        assert mgr.chain == ["claude", "openai", "deepseek"]

    def test_custom_chain(self):
        """自定义链应被正确存储。"""
        mgr = ProviderFallbackManager(chain=["claude", "openai"])
        assert mgr.chain == ["claude", "openai"]
        assert len(mgr.states) == 2

    def test_all_providers_healthy_on_init(self):
        """初始化时所有 provider 应为 HEALTHY。"""
        mgr = ProviderFallbackManager()
        for name in mgr.chain:
            assert mgr.states[name].status == ProviderStatus.HEALTHY

    def test_default_threshold(self):
        """默认失败阈值应为 5。"""
        mgr = ProviderFallbackManager()
        assert mgr.failure_threshold == 5

    def test_custom_threshold(self):
        """自定义阈值应生效。"""
        mgr = ProviderFallbackManager(failure_threshold=3)
        assert mgr.failure_threshold == 3


class TestDecideProvider:
    """provider 决策测试。"""

    def test_first_call_returns_first_provider(self):
        """首次调用应返回链中的第一个 provider。"""
        mgr = ProviderFallbackManager(chain=["a", "b", "c"])
        result = mgr.decide_provider()
        assert result.provider == "a"
        assert result.action == FallbackAction.USE_CURRENT
        assert result.success is True

    def test_stays_on_healthy_provider(self):
        """持续调用应保持当前 provider。"""
        mgr = ProviderFallbackManager(chain=["a", "b", "c"])
        for _ in range(5):
            result = mgr.decide_provider()
            assert result.provider == "a"

    def test_no_probe_when_not_due(self):
        """未到探测间隔时不应触发探测。"""
        mgr = ProviderFallbackManager(
            chain=["a", "b"],
            failure_threshold=1,
            probe_interval=300,
        )
        mgr.report_failure("a", "test")
        mgr.report_failure("a", "test again")  # threshold=1, 第一次失败 count=1, 第二次 count=2>=1 → DEGRADED
        # probe_interval=300s，刚降级不应触发探测，应回退到 b
        result = mgr.decide_provider()
        assert result.provider == "b"
        assert result.action in (FallbackAction.USE_CURRENT, FallbackAction.FALLBACK)


class TestReportFailureAndDegradation:
    """失败报告和降级测试。"""

    def test_single_failure_increments_counter(self):
        """单次失败应递增计数器。"""
        mgr = ProviderFallbackManager()
        mgr.report_failure("claude", "timeout")
        st = mgr.get_provider_state("claude")
        assert st.failure_count == 1
        assert st.status == ProviderStatus.HEALTHY

    def test_reaches_threshold_triggers_degradation(self):
        """达到阈值应触发降级到 DEGRADED。"""
        mgr = ProviderFallbackManager(failure_threshold=3)
        for i in range(3):
            mgr.report_failure("claude", f"fail {i}")
        st = mgr.get_provider_state("claude")
        assert st.failure_count == 3
        assert st.status == ProviderStatus.DEGRADED

    def test_degraded_continues_to_circuit_open(self):
        """DEGRADED 状态下继续失败应进入 CIRCUIT_OPEN。"""
        mgr = ProviderFallbackManager(failure_threshold=2, chain=["claude"])
        # 先达到 DEGRADED
        for i in range(2):
            mgr.report_failure("claude", f"fail {i}")
        assert mgr.get_provider_state("claude").status == ProviderStatus.DEGRADED
        # 继续失败
        for i in range(2):
            mgr.report_failure("claude", f"continue {i}")
        st = mgr.get_provider_state("claude")
        assert st.status == ProviderStatus.CIRCUIT_OPEN

    def test_fallback_to_next_provider(self):
        """当前 provider 降级后应回退到下一个。"""
        mgr = ProviderFallbackManager(
            chain=["a", "b", "c"],
            failure_threshold=1,
        )
        mgr.report_failure("a", "fail")
        mgr.report_failure("a", "fail again")  # threshold=1, 立即降级
        result = mgr.decide_provider()
        assert result.provider == "b"


class TestReportSuccessAndRecovery:
    """成功报告和恢复测试。"""

    def test_success_resets_failure_counter(self):
        """成功调用应重置失败计数器。"""
        mgr = ProviderFallbackManager()
        mgr.report_failure("claude", "fail")
        mgr.report_failure("claude", "fail")
        mgr.report_success("claude")
        st = mgr.get_provider_state("claude")
        assert st.failure_count == 0
        assert st.status == ProviderStatus.HEALTHY

    def test_success_recovers_from_degraded(self):
        """成功后应从 DEGRADED 恢复到 HEALTHY。"""
        mgr = ProviderFallbackManager(failure_threshold=3)
        # 先让 claude 进入 DEGRADED
        for i in range(3):
            mgr.report_failure("claude", f"fail {i}")
        assert mgr.get_provider_state("claude").status == ProviderStatus.DEGRADED
        # 成功后恢复
        mgr.report_success("claude")
        assert mgr.get_provider_state("claude").status == ProviderStatus.HEALTHY


class TestAllExhausted:
    """全部耗尽测试。"""

    def test_detects_all_exhausted(self):
        """全部 provider 降级后应检测到。"""
        mgr = ProviderFallbackManager(
            chain=["a", "b"],
            failure_threshold=1,
        )
        for _ in range(2):
            mgr.report_failure("a", "fail")
        for _ in range(2):
            mgr.report_failure("b", "fail")
        assert mgr.all_exhausted() is True

    def test_not_exhausted_with_one_healthy(self):
        """还有一个 HEALTHY 时不应判定为耗尽。"""
        mgr = ProviderFallbackManager(chain=["a", "b"], failure_threshold=1)
        for _ in range(2):
            mgr.report_failure("a", "fail")
        assert mgr.all_exhausted() is False

    def test_all_exhausted_returns_fallback(self):
        """全部耗尽时 decide_provider 应返回 ALL_EXHAUSTED。"""
        mgr = ProviderFallbackManager(
            chain=["a", "b"],
            failure_threshold=1,
        )
        for _ in range(2):
            mgr.report_failure("a", "fail")
        for _ in range(2):
            mgr.report_failure("b", "fail")
        result = mgr.decide_provider()
        assert result.action == FallbackAction.ALL_EXHAUSTED
        assert result.success is False


class TestResetAndRecovery:
    """重置和手动恢复测试。"""

    def test_reset_all_restores_health(self):
        """reset() 应恢复所有 provider 为 HEALTHY。"""
        mgr = ProviderFallbackManager(chain=["a", "b"], failure_threshold=1)
        for _ in range(2):
            mgr.report_failure("a", "fail")
        for _ in range(2):
            mgr.report_failure("b", "fail")
        mgr.reset()
        for name in mgr.chain:
            st = mgr.get_provider_state(name)
            assert st.status == ProviderStatus.HEALTHY
            assert st.failure_count == 0

    def test_reset_single_provider(self):
        """reset_provider() 应重置单个 provider。"""
        mgr = ProviderFallbackManager(chain=["a", "b"], failure_threshold=1)
        for _ in range(2):
            mgr.report_failure("a", "fail")
        assert mgr.reset_provider("a") is True
        assert mgr.get_provider_state("a").status == ProviderStatus.HEALTHY
        # b 不应被影响
        assert mgr.get_provider_state("b").status == ProviderStatus.HEALTHY

    def test_reset_unknown_provider(self):
        """重置未知 provider 应返回 False。"""
        mgr = ProviderFallbackManager()
        assert mgr.reset_provider("nonexistent") is False


class TestQueryInterfaces:
    """查询接口测试。"""

    def test_get_current_provider(self):
        """应返回当前活跃的 provider。"""
        mgr = ProviderFallbackManager(chain=["a", "b"])
        assert mgr.get_current_provider() == "a"

    def test_get_provider_state_returns_copy(self):
        """get_provider_state 应返回副本（外部修改不影响内部）。"""
        mgr = ProviderFallbackManager()
        st = mgr.get_provider_state("claude")
        st.failure_count = 999
        assert mgr.get_provider_state("claude").failure_count == 0

    def test_get_all_states_returns_all(self):
        """get_all_states 应返回所有 provider 状态。"""
        mgr = ProviderFallbackManager(chain=["a", "b", "c"])
        all_st = mgr.get_all_states()
        assert len(all_st) == 3
        assert "a" in all_st
        assert "b" in all_st
        assert "c" in all_st

    def test_get_provider_state_unknown(self):
        """查询未知 provider 返回 None。"""
        mgr = ProviderFallbackManager()
        assert mgr.get_provider_state("nonexistent") is None


class TestTotalCounters:
    """累计计数器测试。"""

    def test_total_calls_incremented(self):
        """total_calls 应在每次报告后递增。"""
        mgr = ProviderFallbackManager()
        mgr.report_success("claude")
        mgr.report_failure("claude", "fail")
        st = mgr.get_provider_state("claude")
        assert st.total_calls == 2
        assert st.total_failures == 1

    def test_total_failures_accurate(self):
        """total_failures 应准确记录失败次数的总数。"""
        mgr = ProviderFallbackManager()
        for i in range(3):
            mgr.report_failure("claude", f"fail {i}")
        mgr.report_success("claude")
        mgr.report_failure("claude", "fail")
        st = mgr.get_provider_state("claude")
        assert st.total_failures == 4
        assert st.total_calls == 5


class TestCircuitBreakerCooldown:
    """熔断器冷却测试。"""

    def test_circuit_open_cooldown_respected(self):
        """熔断器冷却时间内不应恢复。"""
        mgr = ProviderFallbackManager(
            chain=["a", "b"],
            failure_threshold=1,
            circuit_cooldown=999,  # 很长的冷却时间
        )
        for _ in range(2):
            mgr.report_failure("a", "fail")
        for _ in range(2):
            mgr.report_failure("b", "fail")
        # 仍在冷却期，应返回 ALL_EXHAUSTED
        result = mgr.decide_provider()
        assert result.action == FallbackAction.ALL_EXHAUSTED


class TestFactoryFunctions:
    """工厂函数测试。"""

    def test_create_factory_defaults(self):
        """工厂函数应使用默认值。"""
        mgr = create_provider_fallback_manager()
        assert mgr.chain == ["claude", "openai", "deepseek"]
        assert mgr.failure_threshold == 5

    def test_create_factory_custom(self):
        """工厂函数应接受自定义参数。"""
        mgr = create_provider_fallback_manager(
            chain=["openai", "deepseek"],
            failure_threshold=2,
        )
        assert mgr.chain == ["openai", "deepseek"]
        assert mgr.failure_threshold == 2

    def test_global_singleton(self):
        """get_global_fallback_manager 应返回单例。"""
        import loop_hermes.provider_fallback as pf
        pf._global_fallback_manager = None
        mgr1 = get_global_fallback_manager(chain=["a", "b"])
        mgr2 = get_global_fallback_manager()
        assert mgr1 is mgr2


class TestThreadSafety:
    """线程安全测试（基础）。"""

    def test_lock_prevents_race_in_decision(self):
        """锁应保护 decide_provider 和 report 操作。"""
        import threading
        mgr = ProviderFallbackManager(chain=["a"], failure_threshold=3)
        errors = []

        def worker(thread_id):
            try:
                for _ in range(10):
                    mgr.decide_provider()
                    mgr.report_success("a")
                    mgr.report_failure("a", f"thread {thread_id}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread errors: {errors}"
        st = mgr.get_provider_state("a")
        # 5 threads * 10 loops * 2 calls (success + failure) = 100
        assert st.total_calls == 100
