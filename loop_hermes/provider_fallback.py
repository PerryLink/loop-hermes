# -*- coding: utf-8 -*-
"""Provider 回退管理器（loop-hermes 内部 LLM 调用）。

管理 loop-hermes 自身调用 LLM 时的 provider 优先级链和容错逻辑。

核心功能:
    - 优先级链: Anthropic → OpenAI → DeepSeek（可配置）
    - 每 provider 独立 failure_counter，5 次失败自动降级
    - 恢复探测: 每 5 分钟尝试恢复上一级 provider
    - 所有 provider 均不可用时触发致命错误退出
    - 线程安全 (threading.Lock)

设计意图:
    本模块不同于项目根目录的 provider_fallback_manager.py:
        - 根目录版本: 提供完整的 LLM 适配器（AnthropicAdapter 等）
        - 本模块: 专注于 loop-hermes 流程内的 provider 容错逻辑
          不直接调用 LLM API，而是管理 provider 可用性状态机，
          供 hermes_client 和 phase_dispatch 调用。

Provider 状态机:
    HEALTHY → (5次失败) → DEGRADED → (等待恢复探测) → HEALTHY
    DEGRADED → (继续失败) → CIRCUIT_OPEN → (恢复探测成功) → HEALTHY
"""

import time
import logging
import threading
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable

logger = logging.getLogger("loop_hermes.provider_fallback")

# ============================================================================
# 状态枚举
# ============================================================================


class ProviderStatus(str, Enum):
    """Provider 健康状态枚举。"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


class FallbackAction(str, Enum):
    """回退动作枚举。"""
    USE_CURRENT = "use_current"
    FALLBACK = "fallback"
    PROBE_RECOVERY = "probe_recovery"
    ALL_EXHAUSTED = "all_exhausted"


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class ProviderState:
    """单个 provider 的运行时状态。

    Attributes:
        name: provider 标识（如 "claude"）
        status: 当前健康状态
        failure_count: 连续失败次数
        last_failure_time: 最近一次失败的时间戳（epoch float）
        last_probe_time: 最近一次恢复探测的时间戳
        total_calls: 累计调用次数
        total_failures: 累计失败次数
        degrated_at: 进入 DEGRADED 状态的时间戳
    """
    name: str
    status: ProviderStatus = ProviderStatus.UNKNOWN
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_probe_time: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    degraded_at: float = 0.0


@dataclass
class FallbackResult:
    """回退调用结果。

    Attributes:
        success: 调用是否成功
        provider: 实际使用的 provider 名称
        action: 执行的回退动作
        message: 结果描述
        data: 可选的附加数据
    """
    success: bool
    provider: str
    action: FallbackAction
    message: str
    data: Optional[Any] = None


# ============================================================================
# 默认常量
# ============================================================================

# 默认回退链（优先级从高到低）
DEFAULT_FALLBACK_CHAIN = ["claude", "openai", "deepseek"]

# 连续失败次数阈值（达到后降级到下一 provider）
DEFAULT_FAILURE_THRESHOLD = 5

# 恢复探测间隔（秒），默认 5 分钟
DEFAULT_PROBE_INTERVAL_SECONDS = 300

# 熔断器打开后等待时间（秒），默认 10 分钟
DEFAULT_CIRCUIT_OPEN_COOLDOWN = 600


# ============================================================================
# ProviderFallbackManager
# ============================================================================


class ProviderFallbackManager:
    """Provider 回退管理器。

    管理多个 LLM provider 的可用性状态，支持自动降级、恢复探测
    和熔断器机制。不直接调用 LLM API，而是为上层调用者提供
    "当前应使用哪个 provider" 的决策。

    使用示例:
        >>> mgr = ProviderFallbackManager()
        >>> result = mgr.decide_provider()
        >>> if result.action == FallbackAction.ALL_EXHAUSTED:
        ...     raise SystemExit("所有 provider 不可用")
        ... # 使用 result.provider 进行 LLM 调用
        >>> mgr.report_success("claude")
        >>> mgr.report_failure("claude", "timeout")

    Attributes:
        chain: provider 优先级链（list of str）
        states: provider → ProviderState 映射
        failure_threshold: 连续失败阈值（默认 5）
        probe_interval: 恢复探测间隔秒数（默认 300）
        circuit_cooldown: 熔断器冷却秒数（默认 600）
        _lock: 线程安全锁
        _current_index: 当前活跃 provider 在 chain 中的索引
    """

    def __init__(
        self,
        chain: Optional[List[str]] = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        probe_interval: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        circuit_cooldown: float = DEFAULT_CIRCUIT_OPEN_COOLDOWN,
    ):
        """初始化 ProviderFallbackManager。

        Args:
            chain: provider 优先级链，默认 ["claude", "openai", "deepseek"]
            failure_threshold: 连续失败次数触发降级的阈值
            probe_interval: 恢复探测间隔（秒）
            circuit_cooldown: 熔断器冷却时间（秒）
        """
        self.chain = chain or DEFAULT_FALLBACK_CHAIN.copy()
        self.failure_threshold = failure_threshold
        self.probe_interval = probe_interval
        self.circuit_cooldown = circuit_cooldown
        self._lock = threading.Lock()
        self._current_index = 0

        # 初始化所有 provider 状态
        self.states: Dict[str, ProviderState] = {}
        for name in self.chain:
            self.states[name] = ProviderState(
                name=name,
                status=ProviderStatus.HEALTHY,
            )
        logger.info(
            "ProviderFallbackManager 初始化: chain=%s, threshold=%d",
            self.chain, self.failure_threshold,
        )

    # ------------------------------------------------------------------
    # 核心决策: 决定当前应使用的 provider
    # ------------------------------------------------------------------

    def decide_provider(self) -> FallbackResult:
        """决定当前应使用哪个 provider。

        决策优先级:
            1. 当前 provider HEALTHY → 继续使用
            2. 当前 provider DEGRADED + 到达探测间隔 → 探测恢复
            3. 当前 provider 不可用 → 降级到下一个
            4. 所有 provider 都不行 → ALL_EXHAUSTED

        Returns:
            FallbackResult 包含 provider 名称和动作
        """
        with self._lock:
            now = time.time()

            # 从当前位置开始遍历
            for offset in range(len(self.chain)):
                idx = (self._current_index + offset) % len(self.chain)
                name = self.chain[idx]
                st = self.states[name]

                if st.status == ProviderStatus.HEALTHY:
                    self._current_index = idx
                    return FallbackResult(
                        success=True,
                        provider=name,
                        action=FallbackAction.USE_CURRENT,
                        message=f"使用 {name}（HEALTHY）",
                    )

                # DEGRADED 且到达探测间隔 → 尝试恢复
                if (st.status == ProviderStatus.DEGRADED
                        and (now - st.last_failure_time) >= self.probe_interval):
                    self._current_index = idx
                    return FallbackResult(
                        success=True,
                        provider=name,
                        action=FallbackAction.PROBE_RECOVERY,
                        message=f"探测恢复 {name}（DEGRADED, 已等待 {now - st.last_failure_time:.0f}s）",
                    )

                # CIRCUIT_OPEN 且冷却时间已过 → 重置为 DEGRADED 并探测
                if (st.status == ProviderStatus.CIRCUIT_OPEN
                        and (now - st.degraded_at) >= self.circuit_cooldown):
                    st.status = ProviderStatus.DEGRADED
                    st.failure_count = self.failure_threshold // 2
                    st.last_probe_time = now
                    self._current_index = idx
                    logger.info("%s 熔断器冷却完成，进入 DEGRADED 探测", name)
                    return FallbackResult(
                        success=True,
                        provider=name,
                        action=FallbackAction.PROBE_RECOVERY,
                        message=f"熔断器冷却完成，探测恢复 {name}",
                    )

                logger.debug("跳过 %s（status=%s）", name, st.status.value)

            # 所有 provider 均不可用
            return FallbackResult(
                success=False,
                provider="",
                action=FallbackAction.ALL_EXHAUSTED,
                message="所有 provider 均不可用（HEALTHY/DEGRADED/CIRCUIT_OPEN）",
            )

    # ------------------------------------------------------------------
    # 结果报告
    # ------------------------------------------------------------------

    def report_success(self, provider: str) -> None:
        """报告 provider 调用成功。

        成功会重置该 provider 的 failure_count，如果之前是 DEGRADED
        则恢复到 HEALTHY。

        Args:
            provider: provider 名称
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return
            was_degraded = st.status == ProviderStatus.DEGRADED
            st.failure_count = 0
            st.total_calls += 1
            if st.status in (ProviderStatus.DEGRADED, ProviderStatus.CIRCUIT_OPEN):
                st.status = ProviderStatus.HEALTHY
                logger.info("%s 恢复为 HEALTHY（调用成功）", provider)
            elif was_degraded:
                st.status = ProviderStatus.HEALTHY
                logger.info("%s 恢复为 HEALTHY（探测成功）", provider)

    def report_failure(self, provider: str, error_message: str = "") -> None:
        """报告 provider 调用失败。

        失败会递增 failure_count，达到阈值后自动降级。

        Args:
            provider: provider 名称
            error_message: 失败描述（用于日志）
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return
            now = time.time()
            st.failure_count += 1
            st.total_failures += 1
            st.total_calls += 1
            st.last_failure_time = now

            logger.warning(
                "%s 调用失败 (%d/%d): %s",
                provider, st.failure_count, self.failure_threshold, error_message,
            )

            if st.failure_count >= self.failure_threshold:
                if st.status == ProviderStatus.HEALTHY:
                    st.status = ProviderStatus.DEGRADED
                    st.degraded_at = now
                    logger.warning("%s 降级为 DEGRADED（%d 次连续失败）",
                                   provider, st.failure_count)
                elif st.status == ProviderStatus.DEGRADED:
                    st.status = ProviderStatus.CIRCUIT_OPEN
                    st.degraded_at = now
                    logger.error("%s 降级为 CIRCUIT_OPEN（继续失败）", provider)

    # ------------------------------------------------------------------
    # 探测与恢复检查
    # ------------------------------------------------------------------

    def is_probe_due(self, provider: str) -> bool:
        """检查指定 provider 是否到恢复探测时间。

        Args:
            provider: provider 名称

        Returns:
            True 如果 provider 处于 DEGRADED 且已过探测间隔
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return False
            if st.status != ProviderStatus.DEGRADED:
                return False
            return (time.time() - st.last_failure_time) >= self.probe_interval

    def probe_recovery(self, provider: str) -> bool:
        """执行恢复探测：如果成功则重置为 HEALTHY。

        调用者应先使用 is_probe_due() 检查是否到达探测时间。

        Args:
            provider: provider 名称

        Returns:
            True 如果探测期间状态允许恢复
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return False
            st.last_probe_time = time.time()
            if st.status == ProviderStatus.DEGRADED:
                return True
            return False

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_current_provider(self) -> Optional[str]:
        """获取当前活跃的 provider 名称。

        Returns:
            当前 provider 名称，无可用时返回 None
        """
        with self._lock:
            if 0 <= self._current_index < len(self.chain):
                return self.chain[self._current_index]
            return None

    def get_provider_state(self, provider: str) -> Optional[ProviderState]:
        """获取指定 provider 的完整状态。

        Args:
            provider: provider 名称

        Returns:
            ProviderState 或 None（未知 provider）
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return None
            # 返回副本避免外部修改
            return ProviderState(
                name=st.name,
                status=st.status,
                failure_count=st.failure_count,
                last_failure_time=st.last_failure_time,
                last_probe_time=st.last_probe_time,
                total_calls=st.total_calls,
                total_failures=st.total_failures,
                degraded_at=st.degraded_at,
            )

    def get_all_states(self) -> Dict[str, ProviderState]:
        """获取所有 provider 的状态摘要。

        Returns:
            provider 名称 → ProviderState 映射
        """
        with self._lock:
            return {
                name: ProviderState(
                    name=st.name,
                    status=st.status,
                    failure_count=st.failure_count,
                    last_failure_time=st.last_failure_time,
                    last_probe_time=st.last_probe_time,
                    total_calls=st.total_calls,
                    total_failures=st.total_failures,
                    degraded_at=st.degraded_at,
                )
                for name, st in self.states.items()
            }

    def all_exhausted(self) -> bool:
        """检查是否所有 provider 均已不可用。

        Returns:
            True 如果所有 provider 都不是 HEALTHY
        """
        with self._lock:
            return all(
                st.status != ProviderStatus.HEALTHY
                for st in self.states.values()
            )

    def reset(self) -> None:
        """重置所有 provider 状态为 HEALTHY。"""
        with self._lock:
            self._current_index = 0
            for st in self.states.values():
                st.status = ProviderStatus.HEALTHY
                st.failure_count = 0
                st.last_failure_time = 0.0
                st.last_probe_time = 0.0
                st.degraded_at = 0.0
            logger.info("所有 provider 状态已重置")

    def reset_provider(self, provider: str) -> bool:
        """重置单个 provider 为 HEALTHY。

        Args:
            provider: provider 名称

        Returns:
            True 如果成功重置
        """
        with self._lock:
            st = self._get_state(provider)
            if st is None:
                return False
            st.status = ProviderStatus.HEALTHY
            st.failure_count = 0
            st.last_failure_time = 0.0
            st.degraded_at = 0.0
            logger.info("%s 状态已重置为 HEALTHY", provider)
            return True

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_state(self, provider: str) -> Optional[ProviderState]:
        """获取 provider 状态（内部无锁访问）。

        Args:
            provider: provider 名称

        Returns:
            ProviderState 或 None
        """
        return self.states.get(provider)


# ============================================================================
# 便捷工厂函数
# ============================================================================


def create_provider_fallback_manager(
    chain: Optional[List[str]] = None,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> ProviderFallbackManager:
    """创建 ProviderFallbackManager 的便捷工厂。

    Args:
        chain: provider 优先级链
        failure_threshold: 连续失败阈值

    Returns:
        配置好的 ProviderFallbackManager 实例
    """
    return ProviderFallbackManager(
        chain=chain,
        failure_threshold=failure_threshold,
    )


# ============================================================================
# 全局单例
# ============================================================================

_global_fallback_manager: Optional[ProviderFallbackManager] = None
_global_lock = threading.Lock()


def get_global_fallback_manager(
    chain: Optional[List[str]] = None,
) -> ProviderFallbackManager:
    """获取全局 ProviderFallbackManager 单例。

    首次调用时创建，后续调用返回同一实例。

    Args:
        chain: provider 优先级链（仅首次创建时使用）

    Returns:
        全局 ProviderFallbackManager 实例
    """
    global _global_fallback_manager
    if _global_fallback_manager is None:
        with _global_lock:
            if _global_fallback_manager is None:
                _global_fallback_manager = create_provider_fallback_manager(chain)
    return _global_fallback_manager

