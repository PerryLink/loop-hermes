# -*- coding: utf-8 -*-
"""配置管理模块。

管理 loop-hermes 运行时的全局配置，包括:
    - Hermes CLI 路径探测
    - Model provider 优先级链（anthropic → openai → deepseek）
    - API keys 管理
    - 四种运行模式（safe / auto / unsafe / collaborative）
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================================
# 运行模式定义
# ============================================================================

class RunMode:
    """运行模式枚举。

    四种模式对应不同信任级别:
        - L1: safe（安全模式）——全部闸门激活，方案确认暂停等待用户
        - L2: auto（标准模式，默认）——方案自动通过，危险操作超阈值暂停
        - L3: unsafe（无限制模式）——仅保留内容安全+灾难性操作硬拦截
        - L1+: collaborative（协作模式）——Part 1 决策点等待确认，超时 30min 自动降级
    """
    SAFE = "safe"
    AUTO = "auto"
    UNSAFE = "unsafe"
    COLLABORATIVE = "collaborative"

    VALID_MODES = {SAFE, AUTO, UNSAFE, COLLABORATIVE}

    @classmethod
    def is_valid(cls, mode: str) -> bool:
        """校验 mode 是否为合法枚举值。

        Args:
            mode: 运行模式字符串。

        Returns:
            True 如果 mode 是 safe/auto/unsafe/collaborative 之一。
        """
        return mode in cls.VALID_MODES

    @classmethod
    def default(cls) -> str:
        """返回默认运行模式。

        Returns:
            默认模式字符串 "auto"。
        """
        return cls.AUTO


# ============================================================================
# Provider 配置
# ============================================================================

# 默认 provider 回退链：按优先级从高到低排列
DEFAULT_PROVIDER_FALLBACK_CHAIN = ["claude", "openai", "deepseek"]

# Provider → 环境变量映射
PROVIDER_ENV_KEY_MAP = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Provider → SDK 类型映射（用于 ProviderFallbackManager）
PROVIDER_SDK_TYPE = {
    "claude": "anthropic",
    "openai": "openai",
    "deepseek": "openai_compatible",
}

# Provider → 默认模型映射
PROVIDER_DEFAULT_MODEL = {
    "claude": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "deepseek": "deepseek-chat",
}

# Provider → API 基础 URL 映射
PROVIDER_BASE_URL = {
    "claude": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}


# ============================================================================
# 配置数据类
# ============================================================================

@dataclass
class LoopHermesConfig:
    """loop-hermes 全局配置数据容器。

    从 CLI 参数和环境变量汇聚而来，传递给所有模块使用。

    Attributes:
        mode: 运行模式（safe / auto / unsafe / collaborative）
        user_request: 用户的需求描述（目标语句）
        state_dir: state.json 所在目录路径
        max_cycles: 最大循环轮次上限
        convergence_rounds: 收敛所需轮次数
        hermes_model: Hermes Agent 使用的模型 ID
        hermes_toolsets: Hermes Agent 启用的工具集列表
        hermes_commit_pin: Hermes Agent Git commit hash（pin 到特定版本）
        provider_fallback_chain: loop-hermes 自身 LLM 调用的 provider 回退链
        skip_testing: 是否跳过测试阶段
        max_part1_rounds: Part 1 设计气泡最大轮次
        route_repeat_max: 同路由点最大重复次数
    """
    mode: str = "auto"
    user_request: str = ""
    state_dir: str = ".hermes/loop-hermes"

    # 循环控制
    max_cycles: int = 5
    convergence_rounds: int = 2
    max_part1_rounds: int = 5
    route_repeat_max: int = 3

    # Hermes 配置
    hermes_model: str = "claude-sonnet-4-20250514"
    hermes_toolsets: List[str] = field(default_factory=lambda: ["code", "shell"])
    hermes_commit_pin: str = ""

    # Provider 回退链（loop-hermes 自身调用）
    provider_fallback_chain: List[str] = field(
        default_factory=lambda: DEFAULT_PROVIDER_FALLBACK_CHAIN.copy()
    )

    # 开关
    skip_testing: bool = False

    # 闸门阈值（按模式分级）
    gate_file_count_threshold: dict = field(default_factory=lambda: {
        "safe": 3,
        "auto": 10,
        "unsafe": 999,
    })
    gate_irreversible_ops_blocked_in: List[str] = field(
        default_factory=lambda: ["safe", "auto"]
    )


def get_env_api_key(provider: str) -> Optional[str]:
    """获取指定 provider 的环境变量 API key。

    Args:
        provider: Provider 名称（claude / openai / deepseek）

    Returns:
        环境变量中对应的 API key 字符串；未设置则返回 None
    """
    env_var = PROVIDER_ENV_KEY_MAP.get(provider, "")
    if env_var:
        return os.environ.get(env_var)
    return None


def get_available_providers() -> List[str]:
    """检测当前环境中哪些 provider 的 API key 已设置。

    Returns:
        已配置 API key 的 provider 名称列表，按默认优先级排序
    """
    available = []
    for provider in DEFAULT_PROVIDER_FALLBACK_CHAIN:
        if get_env_api_key(provider):
            available.append(provider)
    return available


def has_any_provider_key() -> bool:
    """检测是否至少有一个 provider 的 API key 已设置。

    Returns:
        True 如果至少有一个 API key 可用
    """
    return len(get_available_providers()) > 0
