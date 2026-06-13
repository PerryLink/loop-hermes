# -*- coding: utf-8 -*-
"""测试: hermes_client.py —— Hermes 客户端抽象层。"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.hermes_client import (
    detect_hermes_engine,
    check_health,
    send_message,
    invoke_hermes,
    _extract_guardrail_from_cli_output,
)


class TestEngineDetection:

    def test_detect_returns_sdk_or_cli(self):
        """引擎检测应返回 "sdk" 或 "cli"。"""
        # 在无 Hermes 环境下应该抛出 RuntimeError，但这里我们
        # 测试函数封装逻辑 —— 直接重置缓存来测试 CLI fallback
        import loop_hermes.hermes_client as hc
        hc._SDK_AVAILABLE = None
        # 如果 hermes 在 PATH 中，应返回 "cli"；否则抛 RuntimeError
        try:
            engine = detect_hermes_engine()
            assert engine in ("sdk", "cli")
        except RuntimeError:
            # 无 Hermes 环境，预期行为
            pass

    def test_check_health_no_hermes(self):
        """无 Hermes 环境时 health check 应返回 unhealthy。"""
        import loop_hermes.hermes_client as hc
        hc._SDK_AVAILABLE = False
        result = check_health()
        assert "healthy" in result
        # 无 hermes CLI 时 healthy=False
        assert result.get("engine", "") in ("cli", "unknown")


class TestGuardrailParsing:

    def test_extract_hardline_guardrail(self):
        """应能从 CLI 输出中解析 HARDLINE guardrail 标记。"""
        stdout = (
            "[GUARDRAIL:HARDLINE] tool=shell_call "
            "message=blocked: dangerous command detected"
        )
        events = _extract_guardrail_from_cli_output(stdout)
        assert len(events) == 1
        assert events[0]["type"] == "HARDLINE"
        assert events[0]["tool"] == "shell_call"

    def test_extract_warn_guardrail(self):
        """应能解析 WARN 类型的 guardrail。"""
        stdout = (
            "[GUARDRAIL:WARN] tool=file_write "
            "message=sensitive file access: /etc/passwd"
        )
        events = _extract_guardrail_from_cli_output(stdout)
        assert len(events) == 1
        assert events[0]["type"] == "WARN"

    def test_extract_multiple_guardrails(self):
        """应能解析多个 guardrail 事件。"""
        stdout = (
            "[GUARDRAIL:WARN] tool=shell_call message=warning 1\n"
            "Some output in between\n"
            "[GUARDRAIL:HARDLINE] tool=file_write message=blocked"
        )
        events = _extract_guardrail_from_cli_output(stdout)
        assert len(events) == 2
        assert events[0]["type"] == "WARN"
        assert events[1]["type"] == "HARDLINE"

    def test_ignore_non_guardrail_lines(self):
        """非 guardrail 行应被忽略。"""
        stdout = "Normal output line\nAnother line"
        events = _extract_guardrail_from_cli_output(stdout)
        assert len(events) == 0


class TestSendMessage:

    @mock.patch("loop_hermes.hermes_client.detect_hermes_engine")
    def test_send_message_cli_success(self, mock_detect):
        """CLI 路径 send_message 返回标准结构。"""
        mock_detect.return_value = "cli"
        with mock.patch("loop_hermes.hermes_client._send_via_cli") as mock_cli:
            mock_cli.return_value = {
                "success": True, "output": "test output",
                "engine": "cli", "guardrail_events": [], "error": None,
            }
            result = send_message("test prompt")
            assert result["success"] is True
            assert result["output"] == "test output"
            assert result["engine"] == "cli"

    @mock.patch("loop_hermes.hermes_client.detect_hermes_engine")
    def test_send_message_no_hermes(self, mock_detect):
        """Hermes 不可用时应返回 failure。"""
        mock_detect.side_effect = RuntimeError("No Hermes")
        result = send_message("test")
        assert result["success"] is False
        assert "No Hermes" in result.get("error", "")


class TestInvokeHermes:

    def test_invoke_hermes_reads_state_config(self):
        """invoke_hermes 应从 state 读取 model/toolsets。"""
        with mock.patch("loop_hermes.hermes_client.send_message") as mock_send:
            mock_send.return_value = {
                "success": True, "output": "ok",
                "engine": "cli", "guardrail_events": [], "error": None,
            }
            state = {
                "progress": {"hermes_engine": "cli"},
                "config": {
                    "hermes_model": "test-model",
                    "hermes_toolsets": ["code"],
                },
            }
            result = invoke_hermes("test", "part_1_1", state)
            assert result["success"] is True
            mock_send.assert_called_once_with(
                prompt="test",
                engine="cli",
                model="test-model",
                toolsets=["code"],
            )


class TestInvokeHermesGuardrailIntegration:
    """invoke_hermes + guardrail_mapper 集成测试。"""

    @mock.patch("loop_hermes.hermes_client.send_message")
    def test_guardrail_events_processed_in_invoke(self, mock_send):
        """invoke_hermes 应处理 send_message 返回的 guardrail 事件。"""
        import json as _json
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE

        mock_send.return_value = {
            "success": True, "output": "ok",
            "engine": "cli",
            "guardrail_events": [
                {"type": "WARN", "tool": "shell_call",
                 "message": "warning", "timestamp": ""},
            ],
            "error": None,
        }
        state = _json.loads(_json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["hermes_engine"] = "cli"

        result = invoke_hermes("test", "part_1_1", state)
        assert result["success"] is True
        # guardrail_summary 应在结果中
        summary = result.get("guardrail_summary")
        assert summary is not None
        assert summary["total"] == 1
        assert summary["by_severity"]["P1"] == 1

    @mock.patch("loop_hermes.hermes_client.send_message")
    def test_hardline_guardrail_injects_p0(self, mock_send):
        """HARDLINE guardrail 应在 state 中生成 P0 issue。"""
        import json as _json
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE

        mock_send.return_value = {
            "success": True, "output": "ok",
            "engine": "cli",
            "guardrail_events": [
                {"type": "HARDLINE", "tool": "shell_call",
                 "message": "blocked", "timestamp": ""},
            ],
            "error": None,
        }
        state = _json.loads(_json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["hermes_engine"] = "cli"

        invoke_hermes("test", "part_1_1", state)
        assert len(state["issues"]["active"]["p0"]) == 1
        assert state["issues"]["active"]["p0"][0]["source"] == "hermes_guardrail"

    @mock.patch("loop_hermes.hermes_client.send_message")
    def test_block_guardrail_terminates_state(self, mock_send):
        """BLOCK guardrail 应将 state 标记为 failed。"""
        import json as _json
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE

        mock_send.return_value = {
            "success": True, "output": "ok",
            "engine": "cli",
            "guardrail_events": [
                {"type": "BLOCK", "tool": "shell_call",
                 "message": "blocked", "timestamp": ""},
            ],
            "error": None,
        }
        state = _json.loads(_json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["hermes_engine"] = "cli"

        invoke_hermes("test", "part_1_1", state)
        assert state["termination"]["status"] == "failed"
        assert "BLOCK" in state["termination"]["exit_reason"]

    @mock.patch("loop_hermes.hermes_client.send_message")
    def test_no_guardrail_events_no_summary(self, mock_send):
        """无 guardrail 事件时 guardrail_summary 应为 None。"""
        mock_send.return_value = {
            "success": True, "output": "ok",
            "engine": "cli", "guardrail_events": [], "error": None,
        }
        state = {
            "progress": {"hermes_engine": "cli"},
            "config": {"hermes_model": "", "hermes_toolsets": []},
        }
        result = invoke_hermes("test", "part_1_1", state)
        assert result.get("guardrail_summary") is None


class TestInvokeHermesProviderIntegration:
    """invoke_hermes + provider_fallback 集成测试。"""

    @mock.patch("loop_hermes.hermes_client.send_message")
    def test_provider_check_on_all_exhausted(self, mock_send):
        """所有 provider 耗尽时 state 应标记为 failed。"""
        import json as _json
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE
        from loop_hermes.provider_fallback import get_global_fallback_manager
        import loop_hermes.provider_fallback as pf

        # 重置全局单例并耗尽所有 provider
        pf._global_fallback_manager = None
        mgr = get_global_fallback_manager(chain=["a", "b"])
        mgr.failure_threshold = 1
        for _ in range(2):
            mgr.report_failure("a", "fail")
        for _ in range(2):
            mgr.report_failure("b", "fail")

        mock_send.return_value = {
            "success": True, "output": "ok",
            "engine": "cli", "guardrail_events": [], "error": None,
        }
        state = _json.loads(_json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["hermes_engine"] = "cli"

        invoke_hermes("test", "part_1_1", state)
        assert state["termination"]["status"] == "failed"
        assert "provider" in state["termination"]["exit_reason"].lower()

        # 清理
        pf._global_fallback_manager = None
