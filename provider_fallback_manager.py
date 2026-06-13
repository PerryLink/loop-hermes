"""
================================================================================
ProviderFallbackManager — 完整实现
================================================================================
路径: D:\100project\1008-loop-hermes\provider_fallback_manager.py

本文件实现 loop-hermes 的 Provider Fallback 管理机制，包含:
  1. 三个 LLM provider 的完整集成 (Anthropic / OpenAI / DeepSeek)
  2. 按优先级自动切换的 fallback 链
  3. 超时重试 + 指数退避
  4. Provider 健康状态追踪 + 熔断器 (circuit breaker)
  5. 线程安全

架构依据 (Creative.txt):
  - Layer 1 (第 1061-1065 行): loop-hermes 自身的 provider 回退
  - Layer 2 (第 1065 行): Hermes 内部的 provider 配置 (loop-hermes 不干预)
  - state.json.config.provider_fallback_chain (第 334 行): 默认 ["claude", "openai", "deepseek"]
  - ProviderFallbackManager 伪代码 (第 1068-1101 行)

依赖 (按需安装，非强制):
  pip install anthropic openai
  DeepSeek 使用 OpenAI SDK 通过 base_url 指向 api.deepseek.com

Python >= 3.10
================================================================================
"""

from __future__ import annotations

import os
import time
import threading
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

# ==============================================================================
# 类型定义
# ==============================================================================

logger = logging.getLogger("loop-hermes.provider_fallback")


class ProviderID(str, Enum):
    """支持的 LLM Provider 标识符。"""
    CLAUDE = "claude"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class ProviderStatus(Enum):
    """Provider 当前健康状态。"""
    HEALTHY = auto()       # 正常可用
    DEGRADED = auto()      # 可用但有延迟/错误率升高
    CIRCUIT_OPEN = auto()  # 熔断——连续失败超过阈值，暂时不可用
    UNKNOWN = auto()       # 尚未探测


@dataclass
class ProviderState:
    """单个 Provider 的运行时状态追踪。"""
    provider: ProviderID
    status: ProviderStatus = ProviderStatus.UNKNOWN

    # 调用统计
    total_calls: int = 0
    success_calls: int = 0
    failure_calls: int = 0
    consecutive_failures: int = 0

    # 性能统计 (毫秒)
    avg_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    last_error: Optional[str] = None
    last_error_time: float = 0.0

    # 熔断器状态
    circuit_opened_at: float = 0.0
    circuit_reset_after: float = 60.0   # 熔断后多少秒尝试重置
    circuit_failure_threshold: int = 3   # 连续失败多少次触发熔断

    # 时间戳
    last_call_time: float = 0.0

    def record_success(self, latency_ms: float) -> None:
        self.total_calls += 1
        self.success_calls += 1
        self.consecutive_failures = 0
        self.last_latency_ms = latency_ms
        # 指数移动平均
        alpha = 0.3
        self.avg_latency_ms = (alpha * latency_ms +
                               (1 - alpha) * self.avg_latency_ms)
        self.last_call_time = time.time()
        self.last_error = None

        if self.status == ProviderStatus.CIRCUIT_OPEN:
            logger.info(f"[{self.provider.value}] 熔断器关闭——恢复可用")
        self.status = ProviderStatus.HEALTHY

    def record_failure(self, error: str) -> None:
        self.total_calls += 1
        self.failure_calls += 1
        self.consecutive_failures += 1
        self.last_error = error
        self.last_error_time = time.time()
        self.last_call_time = time.time()

        # 检查是否触发熔断
        if self.consecutive_failures >= self.circuit_failure_threshold:
            if self.status != ProviderStatus.CIRCUIT_OPEN:
                self._open_circuit()
        elif self.consecutive_failures >= 2:
            self.status = ProviderStatus.DEGRADED
        else:
            self.status = ProviderStatus.DEGRADED  # 一次失败即标记降级

    def _open_circuit(self) -> None:
        """打开熔断器——标记 provider 暂时不可用。"""
        self.status = ProviderStatus.CIRCUIT_OPEN
        self.circuit_opened_at = time.time()
        logger.warning(
            f"[{self.provider.value}] 熔断器触发！"
            f"连续 {self.consecutive_failures} 次失败。"
            f"将在 {self.circuit_reset_after}s 后尝试恢复。"
            f"最后错误: {self.last_error}"
        )

    def should_attempt(self) -> bool:
        """判断当前是否可以尝试调用此 provider。"""
        if self.status != ProviderStatus.CIRCUIT_OPEN:
            return True
        # 检查熔断是否超时
        elapsed = time.time() - self.circuit_opened_at
        if elapsed >= self.circuit_reset_after:
            logger.info(
                f"[{self.provider.value}] 熔断超时已过 ({elapsed:.1f}s)——"
                f"尝试半开探测"
            )
            self.status = ProviderStatus.UNKNOWN  # 半开状态
            return True
        return False

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_calls / self.total_calls


@dataclass
class LLMResponse:
    """统一的 LLM 调用响应结构。"""
    text: str
    provider: ProviderID
    model: str
    latency_ms: float
    token_usage: dict = field(default_factory=dict)
    finish_reason: str = "unknown"


@dataclass
class ProviderCallConfig:
    """单次 LLM 调用的配置。"""
    model: str                          # 模型名
    prompt: str                         # 输入 prompt
    system_prompt: Optional[str] = None # 系统提示
    max_tokens: int = 4096             # 最大输出 token
    temperature: float = 0.7           # 采样温度
    timeout_seconds: int = 120         # 请求超时
    max_retries_per_provider: int = 2  # 每个 provider 的最大重试次数


# ==============================================================================
# Provider 适配器接口 (Strategy Pattern)
# ==============================================================================

class BaseProviderAdapter:
    """Provider 适配器基类——每种 LLM provider 实现此接口。"""

    provider_id: ProviderID

    def __init__(self, api_key: Optional[str] = None,
                 api_key_env_var: str = "",
                 default_model: str = ""):
        self.api_key = api_key or os.getenv(api_key_env_var, "")
        self.default_model = default_model

    def call(self, config: ProviderCallConfig) -> LLMResponse:
        """执行单次 LLM 调用。子类实现。"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """检查 provider 是否可连接（轻量级探测）。"""
        return bool(self.api_key)

    def get_model_name(self, config: ProviderCallConfig) -> str:
        return config.model or self.default_model


# ==============================================================================
# Anthropic (Claude) Provider 适配器
# ==============================================================================

class AnthropicAdapter(BaseProviderAdapter):
    """Anthropic Claude API 适配器。

    使用 anthropic Python SDK。
    需要 pip install anthropic
    环境变量: ANTHROPIC_API_KEY
    """

    provider_id = ProviderID.CLAUDE
    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    # Anthropic 特有的错误码分类
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}
    PERMANENT_STATUS_CODES = {400, 401, 403, 404}

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            api_key=api_key,
            api_key_env_var="ANTHROPIC_API_KEY",
            default_model=self.DEFAULT_MODEL,
        )
        self._client = None

    def _get_client(self):
        """懒加载 Anthropic client。"""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise ProviderNotAvailableError(
                    "anthropic",
                    "anthropic package not installed. Run: pip install anthropic"
                )
        return self._client

    def call(self, config: ProviderCallConfig) -> LLMResponse:
        client = self._get_client()
        import anthropic

        model = self.get_model_name(config)
        start_time = time.monotonic()

        try:
            # 构建消息
            messages = []
            if config.system_prompt:
                system = config.system_prompt
            else:
                system = "You are loop-hermes, an autonomous development agent."
            messages.append({"role": "user", "content": config.prompt})

            response = client.messages.create(
                model=model,
                max_tokens=config.max_tokens,
                system=system,
                messages=messages,
                temperature=config.temperature,
            )

            elapsed_ms = (time.monotonic() - start_time) * 1000

            # 提取文本输出
            text = ""
            for block in response.content:
                if block.type == "text":
                    text += block.text

            return LLMResponse(
                text=text,
                provider=ProviderID.CLAUDE,
                model=model,
                latency_ms=elapsed_ms,
                token_usage={
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                    "total": response.usage.input_tokens + response.usage.output_tokens,
                },
                finish_reason=response.stop_reason or "end_turn",
            )

        except anthropic.APIStatusError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            if e.status_code in self.RETRYABLE_STATUS_CODES:
                raise RetryableProviderError(
                    ProviderID.CLAUDE,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
            else:
                raise PermanentProviderError(
                    ProviderID.CLAUDE,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
        except (anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError) as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise RetryableProviderError(
                ProviderID.CLAUDE,
                f"{type(e).__name__}: {e}",
                latency_ms=elapsed_ms,
            )
        except anthropic.AuthenticationError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise PermanentProviderError(
                ProviderID.CLAUDE,
                f"Authentication failed: {e}",
                status_code=401,
                latency_ms=elapsed_ms,
            )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import anthropic
            return True
        except ImportError:
            return False


# ==============================================================================
# OpenAI Provider 适配器
# ==============================================================================

class OpenAIAdapter(BaseProviderAdapter):
    """OpenAI API 适配器。

    使用 openai Python SDK。
    需要 pip install openai
    环境变量: OPENAI_API_KEY
    """

    provider_id = ProviderID.OPENAI
    DEFAULT_MODEL = "gpt-4o"

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            api_key=api_key,
            api_key_env_var="OPENAI_API_KEY",
            default_model=self.DEFAULT_MODEL,
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
                self._client = openai.OpenAI(api_key=self.api_key)
            except ImportError:
                raise ProviderNotAvailableError(
                    "openai",
                    "openai package not installed. Run: pip install openai"
                )
        return self._client

    def call(self, config: ProviderCallConfig) -> LLMResponse:
        client = self._get_client()
        import openai

        model = self.get_model_name(config)
        start_time = time.monotonic()

        messages = []
        if config.system_prompt:
            messages.append({"role": "system", "content": config.system_prompt})
        messages.append({"role": "user", "content": config.prompt})

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                timeout=config.timeout_seconds,
            )

            elapsed_ms = (time.monotonic() - start_time) * 1000
            choice = response.choices[0]

            return LLMResponse(
                text=choice.message.content or "",
                provider=ProviderID.OPENAI,
                model=model,
                latency_ms=elapsed_ms,
                token_usage={
                    "input": response.usage.prompt_tokens,
                    "output": response.usage.completion_tokens,
                    "total": response.usage.total_tokens,
                },
                finish_reason=choice.finish_reason or "stop",
            )

        except openai.APIStatusError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            if e.status_code in self.RETRYABLE_STATUS_CODES:
                raise RetryableProviderError(
                    ProviderID.OPENAI,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
            else:
                raise PermanentProviderError(
                    ProviderID.OPENAI,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
        except (openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError) as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise RetryableProviderError(
                ProviderID.OPENAI,
                f"{type(e).__name__}: {e}",
                latency_ms=elapsed_ms,
            )
        except openai.AuthenticationError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise PermanentProviderError(
                ProviderID.OPENAI,
                f"Authentication failed: {e}",
                status_code=401,
                latency_ms=elapsed_ms,
            )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import openai
            return True
        except ImportError:
            return False


# ==============================================================================
# DeepSeek Provider 适配器
# ==============================================================================

class DeepSeekAdapter(BaseProviderAdapter):
    """DeepSeek API 适配器。

    DeepSeek API 兼容 OpenAI SDK 格式——通过 base_url 指向 DeepSeek 服务器。
    需要 pip install openai
    环境变量: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com (默认) 或 https://api.deepseek.com/v1
    """

    provider_id = ProviderID.DEEPSEEK
    DEFAULT_MODEL = "deepseek-chat"
    DEFAULT_BASE_URL = "https://api.deepseek.com"

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        super().__init__(
            api_key=api_key,
            api_key_env_var="DEEPSEEK_API_KEY",
            default_model=self.DEFAULT_MODEL,
        )
        self.base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL", self.DEFAULT_BASE_URL
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
                self._client = openai.OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
            except ImportError:
                raise ProviderNotAvailableError(
                    "deepseek",
                    "openai package not installed. Run: pip install openai"
                )
        return self._client

    def call(self, config: ProviderCallConfig) -> LLMResponse:
        client = self._get_client()
        import openai

        model = self.get_model_name(config)
        start_time = time.monotonic()

        messages = []
        if config.system_prompt:
            messages.append({"role": "system", "content": config.system_prompt})
        messages.append({"role": "user", "content": config.prompt})

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                timeout=config.timeout_seconds,
            )

            elapsed_ms = (time.monotonic() - start_time) * 1000
            choice = response.choices[0]

            return LLMResponse(
                text=choice.message.content or "",
                provider=ProviderID.DEEPSEEK,
                model=model,
                latency_ms=elapsed_ms,
                token_usage={
                    "input": response.usage.prompt_tokens if response.usage else 0,
                    "output": response.usage.completion_tokens if response.usage else 0,
                    "total": response.usage.total_tokens if response.usage else 0,
                },
                finish_reason=choice.finish_reason or "stop",
            )

        except openai.APIStatusError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            if e.status_code in self.RETRYABLE_STATUS_CODES:
                raise RetryableProviderError(
                    ProviderID.DEEPSEEK,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
            else:
                raise PermanentProviderError(
                    ProviderID.DEEPSEEK,
                    f"HTTP {e.status_code}: {e.message}",
                    status_code=e.status_code,
                    latency_ms=elapsed_ms,
                )
        except (openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError) as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise RetryableProviderError(
                ProviderID.DEEPSEEK,
                f"{type(e).__name__}: {e}",
                latency_ms=elapsed_ms,
            )
        except openai.AuthenticationError as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            raise PermanentProviderError(
                ProviderID.DEEPSEEK,
                f"Authentication failed: {e}",
                status_code=401,
                latency_ms=elapsed_ms,
            )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import openai
            return True
        except ImportError:
            return False


# ==============================================================================
# 异常层次结构
# ==============================================================================

class ProviderError(Exception):
    """所有 Provider 相关异常的基础类。"""
    def __init__(self, provider: ProviderID, message: str,
                 status_code: Optional[int] = None,
                 latency_ms: float = 0.0):
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.latency_ms = latency_ms
        super().__init__(f"[{provider.value}] {message}")


class RetryableProviderError(ProviderError):
    """可重试的 Provider 错误——超时、限流、服务器错误。"""
    pass


class PermanentProviderError(ProviderError):
    """不可重试的 Provider 错误——认证失败、权限不足、参数错误。"""
    pass


class ProviderNotAvailableError(ProviderError):
    """Provider SDK 未安装或 API key 未配置。"""
    def __init__(self, provider_name: str, detail: str):
        self.provider_name = provider_name
        self.detail = detail
        # 不使用 ProviderID enum 因为此时可能还不知道 provider_id
        super().__init__(ProviderID.CLAUDE, f"{provider_name}: {detail}")


class AllProvidersExhaustedError(Exception):
    """所有 Provider 回退链均已耗尽——无法完成 LLM 调用。"""
    def __init__(self, errors: list[tuple[ProviderID, str]]):
        self.errors = errors
        detail = "; ".join(f"[{p.value}] {e}" for p, e in errors)
        super().__init__(
            f"All providers exhausted. Errors: {detail}"
        )


# ==============================================================================
# ProviderFallbackManager — 核心类
# ==============================================================================

class ProviderFallbackManager:
    """管理 loop-hermes 自身 LLM 调用的 provider 回退链。

    功能:
      - 按优先级链依次尝试 (Claude -> OpenAI -> DeepSeek)
      - 每个 provider 有独立的超时重试 + 指数退避
      - 熔断器：连续失败 N 次自动暂时排除
      - 线程安全
      - 健康状态追踪和统计上报

    用法:
        manager = ProviderFallbackManager(fallback_chain=[
            ProviderID.CLAUDE, ProviderID.OPENAI, ProviderID.DEEPSEEK
        ])
        response = manager.call_with_fallback(prompt="Write hello world")
        print(response.text)

    配置:
        - 通过 state.json.config.provider_fallback_chain 控制回退顺序
        - API keys 通过环境变量配置:
          ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
    """

    def __init__(
        self,
        fallback_chain: Optional[list[ProviderID | str]] = None,
        # 每 provider 超时重试配置
        max_retries_per_provider: int = 2,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 30.0,
        retry_backoff_multiplier: float = 2.0,
        # 熔断器配置
        circuit_failure_threshold: int = 3,
        circuit_reset_seconds: float = 60.0,
        # 请求超时
        default_timeout_seconds: int = 120,
        # Provider 适配器（可注入自定义实现）
        adapters: Optional[dict[ProviderID, BaseProviderAdapter]] = None,
    ):
        # 回退链——默认 Claude -> OpenAI -> DeepSeek
        chain_raw = fallback_chain or [
            ProviderID.CLAUDE, ProviderID.OPENAI, ProviderID.DEEPSEEK
        ]
        self.fallback_chain: list[ProviderID] = [
            ProviderID(p) if isinstance(p, str) else p for p in chain_raw
        ]

        # 重试配置
        self.max_retries_per_provider = max_retries_per_provider
        self.retry_base_delay = retry_base_delay_seconds
        self.retry_max_delay = retry_max_delay_seconds
        self.retry_backoff_multiplier = retry_backoff_multiplier
        self.default_timeout = default_timeout_seconds

        # Provider 适配器（注入或自动创建）
        if adapters:
            self._adapters = adapters
        else:
            self._adapters = self._create_default_adapters()

        # Provider 状态追踪
        self._states: dict[ProviderID, ProviderState] = {
            pid: ProviderState(provider=pid,
                              circuit_failure_threshold=circuit_failure_threshold,
                              circuit_reset_after=circuit_reset_seconds)
            for pid in self.fallback_chain
        }

        # 线程安全锁
        self._lock = threading.RLock()

        # 初始化后立即做一次健康探测
        self._initial_health_check()

        logger.info(
            f"ProviderFallbackManager 初始化完成。"
            f"回退链: {[p.value for p in self.fallback_chain]}"
        )

    def _create_default_adapters(self) -> dict[ProviderID, BaseProviderAdapter]:
        """自动创建默认的 Provider 适配器（从环境变量读取 API key）。"""
        adapters: dict[ProviderID, BaseProviderAdapter] = {}

        # Anthropic
        try:
            anthro = AnthropicAdapter()
            if anthro.is_available():
                adapters[ProviderID.CLAUDE] = anthro
            else:
                logger.warning(
                    "Anthropic adapter: ANTHROPIC_API_KEY not set or "
                    "anthropic package not installed"
                )
        except Exception as e:
            logger.warning(f"Anthropic adapter init failed: {e}")

        # OpenAI
        try:
            oai = OpenAIAdapter()
            if oai.is_available():
                adapters[ProviderID.OPENAI] = oai
            else:
                logger.warning(
                    "OpenAI adapter: OPENAI_API_KEY not set or "
                    "openai package not installed"
                )
        except Exception as e:
            logger.warning(f"OpenAI adapter init failed: {e}")

        # DeepSeek
        try:
            ds = DeepSeekAdapter()
            if ds.is_available():
                adapters[ProviderID.DEEPSEEK] = ds
            else:
                logger.warning(
                    "DeepSeek adapter: DEEPSEEK_API_KEY not set or "
                    "openai package not installed"
                )
        except Exception as e:
            logger.warning(f"DeepSeek adapter init failed: {e}")

        return adapters

    def _initial_health_check(self) -> None:
        """初始化时对所有 provider 做一次快速可用性检查。"""
        available = []
        unavailable = []
        for pid in self.fallback_chain:
            adapter = self._adapters.get(pid)
            if adapter and adapter.is_available():
                available.append(pid.value)
                self._states[pid].status = ProviderStatus.HEALTHY
            else:
                unavailable.append(pid.value)
                self._states[pid].status = ProviderStatus.UNKNOWN
        logger.info(
            f"Provider 可用: {available}; 不可用: {unavailable}"
        )

    # ── 公共 API ─────────────────────────────────────────────────────────

    def call_with_fallback(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout_seconds: Optional[int] = None,
    ) -> LLMResponse:
        """按回退链依次尝试调用 LLM。

        流程:
          1. 遍历回退链中的每个 provider
          2. 跳过不可用/熔断的 provider
          3. 每个 provider 有最多 max_retries_per_provider 次重试 (指数退避)
          4. 一个 provider 永久失败 -> 切换到下一个
          5. 所有 provider 耗尽 -> 抛出 AllProvidersExhaustedError

        返回:
            首个成功调用的 LLMResponse

        异常:
            AllProvidersExhaustedError — 所有 provider 均失败
            ValueError — 回退链中没有任何可用的 provider
        """
        config = ProviderCallConfig(
            model=model or "",
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds or self.default_timeout,
            max_retries_per_provider=self.max_retries_per_provider,
        )

        errors: list[tuple[ProviderID, str]] = []
        tried_providers: list[ProviderID] = []

        for pid in self.fallback_chain:
            with self._lock:
                state = self._states[pid]
                adapter = self._adapters.get(pid)

            # 检查 adapter 是否存在
            if adapter is None:
                errors.append((pid, "Adapter not available (SDK not installed or API key not set)"))
                continue

            # 检查熔断器
            if not state.should_attempt():
                errors.append((
                    pid,
                    f"Circuit breaker open. "
                    f"Consecutive failures: {state.consecutive_failures}. "
                    f"Will retry after {state.circuit_reset_after}s from "
                    f"{time.strftime('%H:%M:%S', time.localtime(state.circuit_opened_at))}"
                ))
                continue

            tried_providers.append(pid)

            # 尝试调用 (含 per-provider 重试)
            for attempt in range(self.max_retries_per_provider):
                try:
                    logger.debug(
                        f"[{pid.value}] 尝试 #{attempt+1}/{self.max_retries_per_provider}..."
                    )

                    response = adapter.call(config)

                    # 成功——记录统计
                    with self._lock:
                        state.record_success(response.latency_ms)

                    logger.info(
                        f"[{pid.value}] 调用成功 "
                        f"({response.latency_ms:.0f}ms, "
                        f"{response.token_usage.get('total', '?')} tokens)"
                    )
                    return response

                except PermanentProviderError as e:
                    # 永久错误——不重试，直接切换 provider
                    with self._lock:
                        state.record_failure(str(e))
                    errors.append((pid, str(e)))
                    logger.error(f"[{pid.value}] 永久错误: {e}")
                    break  # 跳出重试循环，切换下一个 provider

                except RetryableProviderError as e:
                    # 可重试错误——指数退避后重试
                    with self._lock:
                        state.record_failure(str(e))

                    if attempt < self.max_retries_per_provider - 1:
                        delay = min(
                            self.retry_base_delay *
                            (self.retry_backoff_multiplier ** attempt),
                            self.retry_max_delay,
                        )
                        logger.warning(
                            f"[{pid.value}] 可重试错误 (尝试 {attempt+1}/"
                            f"{self.max_retries_per_provider}): {e}. "
                            f"{delay:.1f}s 后重试..."
                        )
                        time.sleep(delay)
                    else:
                        errors.append((pid, f"Retries exhausted: {e}"))
                        logger.error(
                            f"[{pid.value}] 重试耗尽 ("
                            f"{self.max_retries_per_provider}次): {e}"
                        )

                except ProviderNotAvailableError as e:
                    errors.append((pid, str(e)))
                    with self._lock:
                        state.record_failure(str(e))
                    break

                except Exception as e:
                    # 未预期的异常
                    with self._lock:
                        state.record_failure(str(e))
                    errors.append((pid, f"Unexpected error: {e}"))
                    logger.exception(f"[{pid.value}] 未预期的错误")
                    break

        # 所有 provider 均失败
        raise AllProvidersExhaustedError(errors)

    # ── 状态查询 ─────────────────────────────────────────────────────────

    def get_provider_state(self, provider: ProviderID) -> Optional[ProviderState]:
        """获取指定 provider 的当前状态。"""
        with self._lock:
            return self._states.get(provider)

    def get_all_states(self) -> dict[ProviderID, ProviderState]:
        """获取所有 provider 的状态快照。"""
        with self._lock:
            return dict(self._states)

    def get_health_summary(self) -> dict:
        """获取健康状态摘要（用于 logging/monitoring）。"""
        with self._lock:
            summary = {}
            for pid, state in self._states.items():
                summary[pid.value] = {
                    "status": state.status.name,
                    "success_rate": f"{state.success_rate:.1%}",
                    "avg_latency_ms": f"{state.avg_latency_ms:.0f}",
                    "total_calls": state.total_calls,
                    "consecutive_failures": state.consecutive_failures,
                    "circuit_open": state.status == ProviderStatus.CIRCUIT_OPEN,
                    "last_error": state.last_error,
                }
            return summary

    def reset_provider(self, provider: ProviderID) -> None:
        """手动重置 provider 状态（清除熔断器）。"""
        with self._lock:
            if provider in self._states:
                old_state = self._states[provider]
                self._states[provider] = ProviderState(
                    provider=provider,
                    status=ProviderStatus.UNKNOWN,
                    circuit_failure_threshold=old_state.circuit_failure_threshold,
                    circuit_reset_after=old_state.circuit_reset_after,
                )
                logger.info(f"[{provider.value}] 状态已手动重置")

    def reset_all(self) -> None:
        """重置所有 provider 状态。"""
        with self._lock:
            for pid in list(self._states.keys()):
                old = self._states[pid]
                self._states[pid] = ProviderState(
                    provider=pid,
                    circuit_failure_threshold=old.circuit_failure_threshold,
                    circuit_reset_after=old.circuit_reset_after,
                )
        logger.info("所有 provider 状态已重置")

    # ── 统计上报 ─────────────────────────────────────────────────────────

    def get_stats_snapshot(self) -> dict:
        """获取完整的统计快照（可序列化为 JSON 存入 state.json）。"""
        with self._lock:
            stats = {
                "fallback_chain": [p.value for p in self.fallback_chain],
                "providers": {},
                "total_calls": 0,
                "total_successes": 0,
                "total_failures": 0,
            }
            for pid, state in self._states.items():
                stats["providers"][pid.value] = {
                    "status": state.status.name,
                    "total_calls": state.total_calls,
                    "success_calls": state.success_calls,
                    "failure_calls": state.failure_calls,
                    "success_rate": round(state.success_rate, 4),
                    "avg_latency_ms": round(state.avg_latency_ms, 1),
                    "last_latency_ms": round(state.last_latency_ms, 1),
                    "consecutive_failures": state.consecutive_failures,
                    "circuit_open": state.status == ProviderStatus.CIRCUIT_OPEN,
                    "circuit_opened_at": (
                        state.circuit_opened_at if state.circuit_opened_at > 0
                        else None
                    ),
                    "last_error": state.last_error,
                }
                stats["total_calls"] += state.total_calls
                stats["total_successes"] += state.success_calls
                stats["total_failures"] += state.failure_calls
            return stats


# ==============================================================================
# 便捷工厂函数
# ==============================================================================

def create_provider_fallback_manager(
    fallback_chain: Optional[list[str]] = None,
    hermes_model: str = "claude-sonnet-4-20250514",
) -> ProviderFallbackManager:
    """从 state.json config 创建 ProviderFallbackManager 的便捷函数。

    参数:
        fallback_chain: Provider 回退链，如 ["claude", "openai", "deepseek"]
                        默认: 从环境变量 LOOP_HERMES_FALLBACK 读取，否则使用默认链
        hermes_model: Hermes 使用的模型名（用于日志/关联）

    返回:
        配置完成的 ProviderFallbackManager 实例
    """
    # 从环境变量读取回退链配置（逗号分隔）
    if fallback_chain is None:
        env_chain = os.getenv("LOOP_HERMES_FALLBACK", "")
        if env_chain:
            fallback_chain = [s.strip() for s in env_chain.split(",") if s.strip()]
        else:
            fallback_chain = ["claude", "openai", "deepseek"]

    logger.info(
        f"创建 ProviderFallbackManager: "
        f"chain={fallback_chain}, model={hermes_model}"
    )

    return ProviderFallbackManager(
        fallback_chain=fallback_chain,  # type: ignore[arg-type]
    )


# ==============================================================================
# Layer 2 — Hermes 内部 Provider 配置传递 (Creative.txt 第 1065 行)
# ==============================================================================

def get_hermes_provider_hint(state: dict) -> dict:
    """从 state.json 提取 Hermes provider 配置。
    注意：此函数返回"建议"——Hermes 有最终决定权。loop-hermes 不干预。

    Creative.txt 第 1065 行：
      "仅通过 AIAgent(provider_fallback=...) 参数传递建议，Hermes 有最终决定权"
    """
    config = state.get("config", {})
    return {
        "provider_fallback": config.get("provider_fallback_chain",
                                         ["claude", "openai", "deepseek"]),
        "model": config.get("hermes_model", "claude-sonnet-4-20250514"),
    }


# ==============================================================================
# 自测
# ==============================================================================

if __name__ == "__main__":
    import json

    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("  ProviderFallbackManager — 自测")
    print("=" * 60)

    # 1. 创建 manager
    print("\n[1] 初始化 ProviderFallbackManager...")
    manager = create_provider_fallback_manager()
    print(f"    回退链: {[p.value for p in manager.fallback_chain]}")

    # 2. 健康状态
    print("\n[2] Provider 健康状态:")
    for pid, state in manager.get_all_states().items():
        print(f"    {pid.value:10s} -> "
              f"status={state.status.name:12s} "
              f"success_rate={state.success_rate:.1%}")

    # 3. 尝试调用 (如果没有 API key 则优雅报错)
    print("\n[3] 尝试最小 LLM 调用...")
    try:
        response = manager.call_with_fallback(
            prompt="Reply with exactly: OK",
            max_tokens=10,
        )
        print(f"    成功! provider={response.provider.value} "
              f"model={response.model} latency={response.latency_ms:.0f}ms"
              f"\n    output: {response.text[:100]}")
    except AllProvidersExhaustedError as e:
        print(f"    预期失败 (无 API key 配置): {e}")
        print(f"    各 Provider 错误详情:")
        for pid, err in e.errors:
            print(f"      [{pid.value}] {err}")
    except Exception as e:
        print(f"    非预期错误: {type(e).__name__}: {e}")

    # 4. 统计快照
    print("\n[4] 统计快照:")
    print(json.dumps(manager.get_stats_snapshot(), indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("  自测完成")
    print("  文件: provider_fallback_manager.py")
    print("=" * 60)
