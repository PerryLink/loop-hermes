# -*- coding: utf-8 -*-
"""测试: cli.py —— CLI 入口。"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.cli import (
    build_parser, validate_args, resolve_goal, resolve_mode,
    print_hloop_state, main,
)


class TestArgParse:

    def test_parse_goal_positional(self):
        """位置参数 goal 应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["build a weather CLI"])
        assert args.goal == "build a weather CLI"

    def test_parse_safe_flag(self):
        """--safe 标志应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["--safe", "test"])
        assert args.safe is True

    def test_parse_mode_option(self):
        """--mode 选项应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["--mode", "safe", "test"])
        assert args.mode == "safe"

    def test_parse_state_dir(self):
        """--state-dir 选项应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["--state-dir", "/custom/path", "test"])
        assert args.state_dir == "/custom/path"

    def test_parse_init_flag(self):
        """--init 标志应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["--init"])
        assert args.init is True

    def test_parse_check_flag(self):
        """--check 标志应正确解析。"""
        parser = build_parser()
        args = parser.parse_args(["--check"])
        assert args.check is True

    def test_safe_and_unsafe_mutually_exclusive(self):
        """--safe 和 --unsafe 应互斥。"""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--safe", "--unsafe", "test"])


class TestValidateArgs:

    def test_no_errors_for_valid_args(self):
        """合法参数不应有错误。"""
        parser = build_parser()
        args = parser.parse_args(["test goal"])
        errors = validate_args(args)
        assert len(errors) == 0

    def test_init_and_check_mutually_exclusive(self):
        """--init 与 --check 应互斥。"""
        parser = build_parser()
        args = parser.parse_args(["--init", "--check"])
        errors = validate_args(args)
        assert len(errors) > 0

    def test_invalid_max_cycles(self):
        """max_cycles < 1 应报错。"""
        parser = build_parser()
        args = parser.parse_args(["--max-cycles", "0", "test"])
        errors = validate_args(args)
        assert any("max-cycles" in e for e in errors)


class TestGoalResolution:

    def test_goal_from_positional(self):
        """goal 应优先选自位置参数。"""
        parser = build_parser()
        args = parser.parse_args(["positional goal", "--requirement", "requirement goal"])
        assert resolve_goal(args) == "positional goal"

    def test_goal_from_requirement_only(self):
        """无位置参数时应使用 --requirement。"""
        parser = build_parser()
        args = parser.parse_args(["--requirement", "requirement goal"])
        assert resolve_goal(args) == "requirement goal"

    def test_goal_none_when_omitted(self):
        """全缺省时 goal 应为 None。"""
        parser = build_parser()
        args = parser.parse_args([])
        assert resolve_goal(args) is None


class TestModeResolution:

    def test_mode_from_flag_safe(self):
        """--safe 应映射到 safe。"""
        parser = build_parser()
        args = parser.parse_args(["--safe", "test"])
        assert resolve_mode(args) == "safe"

    def test_mode_default_auto(self):
        """默认模式应为 auto。"""
        parser = build_parser()
        args = parser.parse_args(["test"])
        assert resolve_mode(args) == "auto"

    def test_mode_from_option_overrides_flag(self):
        """--mode 应优先于 --safe。"""
        parser = build_parser()
        args = parser.parse_args(["--mode", "unsafe", "--safe", "test"])
        assert resolve_mode(args) == "unsafe"


class TestHloopState:

    def test_print_hloop_state_no_error(self):
        """HLOOP_STATE 输出不应抛异常。"""
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE
        import json
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        try:
            print_hloop_state(state)
        except Exception as e:
            pytest.fail(f"print_hloop_state raised: {e}")

    def test_print_hloop_state_json_mode(self):
        """JSON 模式输出不应抛异常。"""
        from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE
        import json as j
        state = j.loads(j.dumps(DEFAULT_STATE_TEMPLATE))
        try:
            print_hloop_state(state, json_mode=True)
        except Exception as e:
            pytest.fail(f"print_hloop_state(json_mode=True) raised: {e}")
