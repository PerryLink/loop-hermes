# -*- coding: utf-8 -*-
"""pytest conftest —— loop-hermes 共享 fixtures 与 marker 注册。

提供：
  - 临时 state.json fixture（原子写入测试复用）
  - FakeArgs fixture（CLI 参数模拟复用）
  - Mock Hermes client fixture
  - 各模块通用测试辅助函数
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 将项目根目录加入 Python 路径，确保所有测试可 import loop_hermes
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Marker 注册 ─────────────────────────────────────────────────
def pytest_configure(config):
    """注册在 pyproject.toml / pytest.ini 中声明的自定义 markers。"""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")


# ── FakeArgs ────────────────────────────────────────────────────
class FakeArgs:
    """模拟 CLI 参数对象，用于 state_machine / phase_dispatch 等测试。"""

    def __init__(self, **kwargs):
        self.state_dir = kwargs.get("state_dir", "")
        self.safe = kwargs.get("safe", False)
        self.unsafe = kwargs.get("unsafe", False)
        self.interactive = kwargs.get("interactive", False)
        self.goal = kwargs.get("goal", "build a weather CLI")
        self.max_cycles = kwargs.get("max_cycles", 5)
        self.convergence_rounds = kwargs.get("convergence_rounds", 2)
        self.hermes_model = kwargs.get("hermes_model", "claude-sonnet-4-20250514")
        self.hermes_toolsets = kwargs.get("hermes_toolsets", "code,shell")
        self.provider_fallback = kwargs.get("provider_fallback", "claude,openai,deepseek")
        self.skip_testing = kwargs.get("skip_testing", False)


# ── 临时 state.json 目录 fixture ────────────────────────────────
@pytest.fixture
def temp_state_dir():
    """创建临时 state 目录，测试完成后自动清理。"""
    with tempfile.TemporaryDirectory(prefix="loop_hermes_test_") as tmpdir:
        yield tmpdir


@pytest.fixture
def temp_state_json(temp_state_dir):
    """在临时目录中创建最小 state.json 并返回路径。"""
    state_path = Path(temp_state_dir) / "state.json"
    minimal_state = {
        "version": "2.0",
        "progress": {
            "phase": "init",
            "cycle": 0,
            "convergence_counter": 0,
            "phase_history": [],
        },
        "issues": {"p0": [], "p1": [], "p2": []},
        "tasks": {"completed": [], "in_progress": [], "pending": []},
        "config": {
            "goal": "test goal",
            "mode": "L2_auto",
            "max_cycles": 5,
            "convergence_rounds": 2,
        },
    }
    state_path.write_text(json.dumps(minimal_state, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(state_path)


# ── Mock Hermes client fixture ──────────────────────────────────
@pytest.fixture
def mock_hermes_client():
    """提供一个模拟 Hermes 客户端（返回成功空响应）。"""
    class MockHermesResult:
        def __init__(self):
            self.text = ""
            self.usage = {"input_tokens": 0, "output_tokens": 0}
            self.stop_reason = "end_turn"
            self.guardrail_events = []

    with tempfile.TemporaryDirectory() as tmpdir:
        result = MockHermesResult()
        yield result


# ── 示例 state fixture 数据 ─────────────────────────────────────
@pytest.fixture
def sample_state_json():
    """返回一个完整的示例 state.json 字典（Part 2 中段状态）。"""
    return {
        "version": "2.0",
        "progress": {
            "phase": "part_2_2",
            "cycle": 1,
            "convergence_counter": 0,
            "phase_history": [
                {"phase": "init", "start_ts": 1718000000.0, "end_ts": 1718000010.0},
                {"phase": "part_1_1", "start_ts": 1718000020.0, "end_ts": 1718000100.0},
                {"phase": "part_1_2", "start_ts": 1718000110.0, "end_ts": 1718000200.0},
                {"phase": "part_1_3", "start_ts": 1718000210.0, "end_ts": 1718000300.0},
                {"phase": "part_2_1", "start_ts": 1718000310.0, "end_ts": 1718000400.0},
            ],
        },
        "issues": {
            "p0": [],
            "p1": [
                {"id": "ISSUE-001", "msg": "missing error handling in weather_api.py", "source": "part_2_2"},
            ],
            "p2": [
                {"id": "ISSUE-002", "msg": "docstring incomplete in cli.py", "source": "part_2_2"},
            ],
        },
        "tasks": {
            "completed": [{"id": "TASK-001", "name": "Set up project structure"}],
            "in_progress": [{"id": "TASK-002", "name": "Implement weather_api module"}],
            "pending": [
                {"id": "TASK-003", "name": "Add CLI integration"},
                {"id": "TASK-004", "name": "Write tests"},
            ],
        },
        "config": {
            "goal": "build a weather CLI",
            "mode": "L2_auto",
            "max_cycles": 5,
            "convergence_rounds": 2,
            "hermes_model": "claude-sonnet-4-20250514",
            "provider_fallback": "claude,openai,deepseek",
        },
        "billing": {"tokens_input": 45000, "tokens_output": 12000, "cost_usd": 0.034},
        "circuit_breaker": {"state": "CLOSED", "failures": 0, "last_failure_ts": None},
    }


# ── FakeArgs fixture (便捷版本) ─────────────────────────────────
@pytest.fixture
def fake_args():
    """返回默认 FakeArgs 实例。"""
    return FakeArgs()
