# -*- coding: utf-8 -*-
"""测试: routing.py —— P1 决策树、路由规则、convergence_counter。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.routing import (
    classify_p1,
    update_convergence_counter,
    execute_routing,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE


def _make_state(**overrides):
    """构建测试用 state 字典（深拷贝模板 + 覆盖）。"""
    state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(state.get(key), dict):
            state[key].update(val)
        else:
            state[key] = val
    return state


class TestP1DecisionTree:

    def test_design_level_root_cause_in_design(self):
        """C1: source=manual_inspection + all .md files → DESIGN_LEVEL。"""
        issue = {
            "id": "issue-001", "severity": "P1",
            "title": "Architecture mismatch",
            "source": "manual_inspection",
            "affected_files": ["artifacts/03-solution.md", "docs/design.md"],
            "description": "The module interface design causes coupling",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "DESIGN_LEVEL"

    def test_implementation_level_lint_warning(self):
        """N1: source=lint_warning → IMPLEMENTATION_LEVEL。"""
        issue = {
            "id": "issue-002", "severity": "P1",
            "title": "Unused variable",
            "source": "lint_warning",
            "affected_files": ["src/main.py"],
            "description": "Variable x is assigned but never used",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "IMPLEMENTATION_LEVEL"

    def test_design_level_cross_module(self):
        """C2: 跨 3+ 模块 → DESIGN_LEVEL。"""
        issue = {
            "id": "issue-003", "severity": "P1",
            "title": "Cross-module interface broken",
            "source": "code_review",
            "affected_files": [
                "src/api/handler.py", "src/db/models.py",
                "src/auth/middleware.py",
            ],
            "description": "API change requires changes across modules",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "DESIGN_LEVEL"

    def test_design_level_recurrence(self):
        """C3: 同问题反复出现 → DESIGN_LEVEL。"""
        issue = {
            "id": "issue-004", "severity": "P1",
            "title": "Authentication bypass",
            "source": "code_review",
            "affected_files": ["src/auth/login.py"],
            "description": "Authentication bypass via missing token check",
        }
        state = _make_state(routing_history=[
            {"cycle": 1, "from": "part_2_8", "to": "part_2_2",
             "reason": "P1: Authentication bypass (semantic match)"}
        ])
        assert classify_p1(issue, state) == "DESIGN_LEVEL"

    def test_implementation_level_build_error(self):
        """N1: source=build_error → IMPLEMENTATION_LEVEL。"""
        issue = {
            "id": "issue-005", "severity": "P1",
            "title": "Build failed",
            "source": "build_error",
            "affected_files": ["src/module.py"],
            "description": "ImportError: No module named 'missing'",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "IMPLEMENTATION_LEVEL"

    def test_implementation_level_fix_strategy_implies_code(self):
        """N3: fix_strategy 指示代码级修复 → IMPLEMENTATION_LEVEL。"""
        issue = {
            "id": "issue-006", "severity": "P1",
            "title": "Parameter mismatch",
            "source": "code_review",
            "affected_files": ["src/api/handler.py", "src/utils.py"],
            "description": "Function call uses wrong parameter order",
            "fix_strategy": "修复错误的api调用参数顺序",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "IMPLEMENTATION_LEVEL"

    def test_implementation_level_few_files_no_interface(self):
        """N4: <=2 files + no interface change → IMPLEMENTATION_LEVEL。"""
        issue = {
            "id": "issue-007", "severity": "P1",
            "title": "Null pointer check missing",
            "source": "code_review",
            "affected_files": ["src/utils.py"],
            "description": "Need to add null check before dereference",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "IMPLEMENTATION_LEVEL"

    def test_design_level_security_foundation(self):
        """C5: 安全基础架构 → DESIGN_LEVEL。"""
        issue = {
            "id": "issue-008", "severity": "P1",
            "title": "Authentication mechanism needs redesign",
            "source": "manual_inspection",
            "affected_files": ["src/auth/tokens.py", "src/auth/session.py"],
            "description": "Current token design is vulnerable to replay attacks",
        }
        state = _make_state(routing_history=[])
        assert classify_p1(issue, state) == "DESIGN_LEVEL"

    def test_design_level_blocking_multiple_tasks(self):
        """C4: 阻塞 2+ 个任务 → DESIGN_LEVEL。"""
        issue = {
            "id": "issue-009", "severity": "P1",
            "title": "Database schema mismatch",
            "source": "code_review",
            "affected_files": [
                "src/db/schema.py", "src/models/user.py",
                "src/api/handler.py",
            ],
            "description": "Schema needs migration",
        }
        state = _make_state(
            tasks=[
                {"id": "task-01", "linked_issue_ids": ["issue-009"], "dependencies": []},
                {"id": "task-02", "linked_issue_ids": ["issue-009"], "dependencies": []},
            ],
            routing_history=[],
        )
        assert classify_p1(issue, state) == "DESIGN_LEVEL"


class TestConvergenceCounter:

    def test_reset_on_new_p1(self):
        """P3: 本轮新增 P1 → 重置为 0。"""
        state = _make_state(
            progress={"convergence_counter": 2, "cycle": 3,
                       "new_issues_this_round": True,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [], "p1": [{"id": "i-1"}], "p2": []}},
        )
        result = update_convergence_counter(state)
        assert result == 0

    def test_increment_no_new_issues(self):
        """P5: 无新问题 + 全部关闭 → +1。"""
        state = _make_state(
            progress={"convergence_counter": 0, "cycle": 2,
                       "new_issues_this_round": False,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [], "p1": [], "p2": []}},
        )
        result = update_convergence_counter(state)
        assert result == 1

    def test_reset_on_new_p2(self):
        """P4: 本轮新增 P2 → 重置为 0。"""
        state = _make_state(
            progress={"convergence_counter": 1, "cycle": 2,
                       "new_issues_this_round": True,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [], "p1": [], "p2": [{"id": "i-2"}]}},
        )
        result = update_convergence_counter(state)
        assert result == 0

    def test_reset_on_active_p0(self):
        """P2: 有活跃 P0 → 重置为 0。"""
        state = _make_state(
            progress={"convergence_counter": 3, "cycle": 3,
                       "new_issues_this_round": False,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [{"id": "i-p0"}], "p1": [], "p2": []}},
        )
        result = update_convergence_counter(state)
        assert result == 0

    def test_stay_when_issues_open_but_no_new(self):
        """P1: 有旧 issue 但本轮无新问题 → 保持不变。"""
        state = _make_state(
            progress={"convergence_counter": 2, "cycle": 2,
                       "new_issues_this_round": False,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 1, "p2": 0}},
            issues={"active": {"p0": [], "p1": [{"id": "i-old"}], "p2": []}},
        )
        result = update_convergence_counter(state)
        assert result == 2


class TestRoutingExecution:

    def test_p0_issues_route_to_part_1_1(self):
        """P0 → Part 1.1（重新设计）。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 0, "convergence_counter": 0,
                       "issues_snapshot_at_round_start": {"p0": 1, "p1": 0, "p2": 0}},
            issues={"active": {
                "p0": [{"id": "i-p0", "title": "Security hole", "source": "code_review"}],
                "p1": [], "p2": [],
            }},
        )
        result = execute_routing(state)
        assert result["target"] == "part_1_1"

    def test_p2_issues_route_to_part_2_2(self):
        """P2 → Part 2.2（修复模式）。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 0, "convergence_counter": 0,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {
                "p0": [], "p1": [],
                "p2": [{"id": "i-p2", "title": "Code style", "source": "code_review",
                         "affected_files": ["src/main.py"]}],
            }},
        )
        result = execute_routing(state)
        assert result["target"] == "part_2_2"
        assert state["progress"]["repair_context"] is not None

    def test_clean_pass_routes_to_part_2_1(self):
        """无活跃 issue → 重新进入 Part 2.1。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 0, "convergence_counter": 0,
                       "new_issues_this_round": False,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [], "p1": [], "p2": []}},
        )
        result = execute_routing(state)
        assert result["target"] == "part_2_1"

    def test_convergence_marks_complete(self):
        """收敛达成 → 标记 complete。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 2, "convergence_counter": 2,
                       "new_issues_this_round": False,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {"p0": [], "p1": [], "p2": []}},
            config={"max_cycles": 5, "convergence_rounds": 2},
        )
        result = execute_routing(state)
        assert result["target"] == "complete"
        assert state["termination"]["status"] == "complete"

    def test_p1_design_level_routes_to_part_1_3(self):
        """P1 设计级 → Part 1.3。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 0, "convergence_counter": 0,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {
                "p0": [],
                "p1": [{"id": "i-p1-design", "severity": "P1", "title": "Architecture flaw",
                         "source": "manual_inspection",
                         "affected_files": ["artifacts/03-solution.md"],
                         "description": "Design needs rework"}],
                "p2": [],
            }},
        )
        result = execute_routing(state)
        assert result["target"] == "part_1_3"

    def test_p1_implementation_level_sets_repair_context(self):
        """P1 实现级 → Part 2.2 + repair_context。"""
        state = _make_state(
            progress={"phase": "routing", "cycle": 0, "convergence_counter": 0,
                       "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0}},
            issues={"active": {
                "p0": [],
                "p1": [{"id": "i-p1-impl", "severity": "P1", "title": "Unused variable",
                         "source": "lint_warning",
                         "affected_files": ["src/main.py"],
                         "description": "Variable unused"}],
                "p2": [],
            }},
        )
        result = execute_routing(state)
        assert result["target"] == "part_2_2"
        # repair_context 应被设置
        assert state["progress"]["repair_context"] is not None
        assert "i-p1-impl" in state["progress"]["repair_context"]["target_issues"]
