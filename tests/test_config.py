# -*- coding: utf-8 -*-
"""测试: config.py —— 配置管理。"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.config import (
    RunMode, LoopHermesConfig, DEFAULT_PROVIDER_FALLBACK_CHAIN,
    PROVIDER_ENV_KEY_MAP, get_env_api_key, get_available_providers,
    has_any_provider_key,
)


class TestRunMode:

    def test_valid_modes(self):
        """四种模式都应判定为合法。"""
        assert RunMode.is_valid("safe")
        assert RunMode.is_valid("auto")
        assert RunMode.is_valid("unsafe")
        assert RunMode.is_valid("collaborative")

    def test_invalid_mode(self):
        """非法模式应返回 False。"""
        assert not RunMode.is_valid("dangerous")
        assert not RunMode.is_valid("")
        assert not RunMode.is_valid("SAFE")

    def test_default_is_auto(self):
        """默认模式应为 auto。"""
        assert RunMode.default() == "auto"


class TestLoopHermesConfig:

    def test_default_values(self):
        """默认配置应有合理值。"""
        cfg = LoopHermesConfig()
        assert cfg.mode == "auto"
        assert cfg.max_cycles == 5
        assert cfg.convergence_rounds == 2
        assert cfg.hermes_model == "claude-sonnet-4-20250514"
        assert cfg.hermes_toolsets == ["code", "shell"]

    def test_custom_values(self):
        """自定义配置应正确存储。"""
        cfg = LoopHermesConfig(
            mode="safe",
            user_request="build a CLI",
            max_cycles=10,
            provider_fallback_chain=["openai"],
        )
        assert cfg.mode == "safe"
        assert cfg.user_request == "build a CLI"
        assert cfg.max_cycles == 10
        assert cfg.provider_fallback_chain == ["openai"]


class TestProviderConfig:

    def test_provider_env_key_map(self):
        """Provider 到环境变量的映射应正确。"""
        assert PROVIDER_ENV_KEY_MAP["claude"] == "ANTHROPIC_API_KEY"
        assert PROVIDER_ENV_KEY_MAP["openai"] == "OPENAI_API_KEY"
        assert PROVIDER_ENV_KEY_MAP["deepseek"] == "DEEPSEEK_API_KEY"

    def test_default_fallback_chain(self):
        """默认回退链应为 claude → openai → deepseek。"""
        assert DEFAULT_PROVIDER_FALLBACK_CHAIN == ["claude", "openai", "deepseek"]

    def test_get_env_api_key_returns_none_when_not_set(self):
        """未设置环境变量时应返回 None。"""
        key = get_env_api_key("claude")
        if "ANTHROPIC_API_KEY" in os.environ:
            assert key is not None
        else:
            assert key is None

    def test_has_any_provider_key(self):
        """检测是否有任何 provider key。"""
        result = has_any_provider_key()
        # 取决于环境，至少不抛异常
        assert isinstance(result, bool)

    def test_get_available_providers_returns_list(self):
        """获取可用 providers 列表。"""
        available = get_available_providers()
        assert isinstance(available, list)
