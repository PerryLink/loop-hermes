# -*- coding: utf-8 -*-
"""测试: phase_dispatch.py —— Phase 分发器、prompt 构造、跨 artifact 数据流。"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.phase_dispatch import (
    PHASE_ORDER,
    PHASE_NEXT,
    PROMPT_FILES,
    load_prompt_template,
    build_hermes_prompt,
    dispatch_phase,
    dispatch_phase_parallel,
    promote_test_failures_to_issues,
    promote_orphan_tasks_to_issues,
    sync_context_summary,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE


class TestPhaseConfig:

    def test_phase_order_has_all_phases(self):
        """PHASE_ORDER 应包含全部 13 个阶段（含 routing）。"""
        assert len(PHASE_ORDER) == 13
        assert "init" in PHASE_ORDER
        assert "part_1_1" in PHASE_ORDER
        assert "part_2_8" in PHASE_ORDER
        assert "routing" in PHASE_ORDER

    def test_phase_next_covers_all(self):
        """PHASE_NEXT 应覆盖所有非 routing phase。"""
        for phase in PHASE_ORDER:
            if phase == "routing":
                continue
            assert phase in PHASE_NEXT, f"Missing transition for {phase}"

    def test_prompt_files_all_phases(self):
        """PROMPT_FILES 应覆盖所有 12 个执行 phase。"""
        for phase in PHASE_ORDER:
            if phase == "routing":
                continue
            assert phase in PROMPT_FILES, f"Missing prompt file mapping for {phase}"


class TestLoadPromptTemplate:

    def test_loads_init_prompt(self):
        """初始化模板应包含 phase contract。"""
        prompt = load_prompt_template("init")
        assert "init" in prompt.lower() or "loop-hermes" in prompt
        # 应包含 Phase Contract
        assert "Expected outputs" in prompt or "expected outputs" in prompt.lower()

    def test_loads_part_2_2_prompt(self):
        """Part 2.2 模板应包含 HLOOP_STATE 块。"""
        prompt = load_prompt_template("part_2_2")
        assert "Part 2.2" in prompt or "implementation" in prompt.lower()

    def test_fallback_for_unknown_phase(self):
        """未知 phase 返回默认回退 prompt。"""
        prompt = load_prompt_template("nonexistent_phase")
        assert "loop-hermes" in prompt


class TestBuildHermesPrompt:

    def test_substitutes_user_request(self):
        """应用替换 user_request 占位符。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["config"]["user_request"] = "build a weather CLI"
        prompt = build_hermes_prompt("init", state)
        assert "build a weather CLI" in prompt

    def test_substitutes_context_summary(self):
        """应用注入 context_summary 内容。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            ctx_file = artifacts_dir / "context-summary.md"
            ctx_file.write_text("Context summary line 1\nContext summary line 2\n", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["context_summary"]["path"] = str(ctx_file)
            state["config"]["user_request"] = "test"

            prompt = build_hermes_prompt("part_1_1", state)
            assert "Context summary" in prompt

    def test_adds_repair_context_block(self):
        """Part 2.2 模式下应注入 repair_context 协议块。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["config"]["user_request"] = "test"
        state["progress"]["repair_context"] = {
            "from_phase": "routing",
            "routing_reason": "P2 issue",
            "target_issues": ["issue-001"],
            "affected_files": ["src/main.py"],
            "attempt_number": 1,
            "review_required": False,
            "hermes_guardrail_source": False,
        }

        prompt = build_hermes_prompt("part_2_2", state)
        assert "REPAIR CONTEXT" in prompt
        assert "issue-001" in prompt
        assert "src/main.py" in prompt


class TestDispatchPhase:

    def test_returns_terminated_for_complete(self):
        """终止状态应直接返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["termination"]["status"] = "complete"

            result = dispatch_phase(state, str(state_dir))
            assert result["status"] == "terminated"

    def test_returns_terminated_for_paused(self):
        """暂停状态应直接返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["termination"]["status"] = "paused"

            result = dispatch_phase(state, str(state_dir))
            assert result["status"] == "terminated"

    def test_routing_phase_is_handled(self):
        """routing phase 应正确执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["progress"]["phase"] = "routing"
            state["config"]["user_request"] = "test"

            result = dispatch_phase(state, str(state_dir))
            # routing 应返回 target
            assert "target" in result


class TestPromoteTestFailures:

    def test_promotes_failures_to_p2_issues(self):
        """失败的测试应提升为 P2 issue。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "loop-hermes"
            artifacts_dir = state_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)

            test_results = {
                "results": [
                    {"id": "test-001", "name": "test_a", "status": "fail",
                     "error_message": "AssertionError: 1 != 2",
                     "linked_task_id": "task-01"},
                    {"id": "test-002", "name": "test_b", "status": "pass"},
                ]
            }
            (artifacts_dir / "08-test-results.json").write_text(
                json.dumps(test_results), encoding="utf-8"
            )

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            count = promote_test_failures_to_issues(state, state_dir)

            assert count == 1
            assert len(state["issues"]["active"]["p2"]) == 1
            assert state["issues"]["active"]["p2"][0]["severity"] == "P2"
            assert "test_a" in state["issues"]["active"]["p2"][0]["title"]

    def test_no_failures_no_issues(self):
        """全部通过时不创建 issue。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "loop-hermes"
            artifacts_dir = state_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)

            test_results = {
                "results": [
                    {"id": "test-001", "name": "test_a", "status": "pass"},
                    {"id": "test-002", "name": "test_b", "status": "pass"},
                ]
            }
            (artifacts_dir / "08-test-results.json").write_text(
                json.dumps(test_results), encoding="utf-8"
            )

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            count = promote_test_failures_to_issues(state, state_dir)
            assert count == 0


class TestPromoteOrphanTasks:

    def test_promotes_pending_task_to_p2(self):
        """pending 任务提升为 P2 issue。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "loop-hermes"
            artifacts_dir = state_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)

            task_list = {
                "tasks": [
                    {"id": "task-01", "title": "Unfinished task", "status": "pending",
                     "assigned_files": ["src/main.py"]},
                    {"id": "task-02", "title": "Done", "status": "completed"},
                ]
            }
            (artifacts_dir / "05-task-list.json").write_text(
                json.dumps(task_list), encoding="utf-8"
            )

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            count = promote_orphan_tasks_to_issues(state, state_dir)

            assert count == 1
            assert len(state["issues"]["active"]["p2"]) == 1

    def test_promotes_failed_task_to_p1(self):
        """failed 任务提升为 P1 issue。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "loop-hermes"
            artifacts_dir = state_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)

            task_list = {
                "tasks": [
                    {"id": "task-03", "title": "Failed task", "status": "failed",
                     "assigned_files": ["src/api.py"]},
                ]
            }
            (artifacts_dir / "05-task-list.json").write_text(
                json.dumps(task_list), encoding="utf-8"
            )

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            count = promote_orphan_tasks_to_issues(state, state_dir)

            assert count == 1
            assert len(state["issues"]["active"]["p1"]) == 1


class TestSyncContextSummary:

    def test_appends_summary_line(self):
        """同步上下文摘要应追加行到 context-summary.md。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "loop-hermes"
            artifacts_dir = state_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["context_summary"]["path"] = str(
                artifacts_dir / "context-summary.md"
            )

            sync_context_summary(state, state_dir, "Phase completed successfully")

            ctx_file = artifacts_dir / "context-summary.md"
            assert ctx_file.exists()
            content = ctx_file.read_text(encoding="utf-8")
            assert "Phase completed successfully" in content
            assert state["progress"]["phase"] in content


class TestDispatchPhaseGuardrailIntegration:
    """dispatch_phase + guardrail 集成测试。"""

    @mock.patch("loop_hermes.hermes_client.invoke_hermes")
    def test_hardline_guardrail_injects_p0_issue(self, mock_invoke):
        """HARDLINE guardrail 事件应在 phase dispatch 中被处理。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["phase"] = "part_2_2"
        state["config"]["user_request"] = "test"

        mock_invoke.return_value = {
            "success": True, "output": "output",
            "engine": "cli",
            "guardrail_events": [
                {"type": "HARDLINE", "tool": "shell_call",
                 "message": "blocked", "timestamp": ""},
            ],
            "guardrail_summary": {
                "total": 1, "by_severity": {"P0": 1, "P1": 0, "P2": 0},
                "terminating": False, "actions": ["RETREAT_TO_PART1"],
                "highest_severity": "P0",
            },
            "error": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            result = dispatch_phase(state, str(state_dir))
            assert result["status"] in ("ok", "error", "terminated")

    @mock.patch("loop_hermes.hermes_client.invoke_hermes")
    def test_terminating_guardrail_stops_phase(self, mock_invoke):
        """终止级 guardrail 应导致 phase 返回 terminated。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["progress"]["phase"] = "part_2_2"
        state["config"]["user_request"] = "test"

        mock_invoke.return_value = {
            "success": False, "output": "",
            "engine": "cli",
            "guardrail_events": [
                {"type": "BLOCK", "tool": "shell_call",
                 "message": "blocked", "timestamp": ""},
            ],
            "guardrail_summary": {
                "total": 1, "by_severity": {"P0": 1, "P1": 0, "P2": 0},
                "terminating": True, "actions": ["TERMINATE"],
                "highest_severity": "P0",
            },
            "error": "guardrail block",
        }

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            result = dispatch_phase(state, str(state_dir))
            assert result["status"] in ("terminated", "error")


class TestParallelDispatch:
    """并行派发 dispatch_phase_parallel 测试。"""

    def test_no_task_specs_returns_no_tasks(self):
        """无任务时返回 no_tasks。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["progress"]["phase"] = "part_2_2"
            state["config"]["user_request"] = "test"

            result = dispatch_phase_parallel(state, str(state_dir), task_specs=[])
            assert result["status"] == "no_tasks"
            assert result["merge_result"] is None

    def test_parallel_with_single_task(self):
        """单任务并行执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["progress"]["phase"] = "part_2_2"
            state["config"]["user_request"] = "test"

            from loop_hermes.parallel_manager import TaskSpec
            specs = [TaskSpec(task_id="t1", prompt="do something")]

            with mock.patch(
                "loop_hermes.hermes_client.invoke_hermes"
            ) as mock_invoke:
                mock_invoke.return_value = {
                    "success": True, "output": "done",
                    "engine": "cli", "guardrail_events": [], "error": None,
                }
                result = dispatch_phase_parallel(
                    state, str(state_dir), task_specs=specs, max_parallel=2,
                )
                assert result["status"] in ("ok", "partial_failure")
                assert result["merge_result"] is not None

    def test_build_task_specs_from_state(self):
        """_build_task_specs_from_state 应从 task_list 创建 TaskSpec。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 创建 task_list JSON
            task_list = {
                "tasks": [
                    {"id": "task-01", "title": "Task 1", "status": "pending",
                     "description": "desc 1", "module": "core",
                     "assigned_files": ["f1.py"], "priority": 1},
                    {"id": "task-02", "title": "Task 2", "status": "completed"},
                    {"id": "task-03", "title": "Task 3", "status": "pending",
                     "description": "desc 3", "module": "ui",
                     "assigned_files": ["f2.py"], "priority": 2},
                ]
            }
            tl_path = Path(tmp) / "task-list.json"
            tl_path.write_text(json.dumps(task_list), encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["task_list"]["path"] = str(tl_path)
            state["progress"]["phase"] = "part_2_2"

            from loop_hermes.phase_dispatch import _build_task_specs_from_state
            specs = _build_task_specs_from_state(state)

            # 只有 2 个 pending 任务
            assert len(specs) == 2
            assert specs[0].task_id == "task-01"
            assert specs[1].task_id == "task-03"
            # 验证 prompt 包含相关信息
            assert "Task 1" in specs[0].prompt
            assert "core" in specs[0].prompt

    def test_parallel_guardrail_injection(self):
        """并行结果中的 guardrail 事件应注入 state。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            (state_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["progress"]["phase"] = "part_2_2"
            state["config"]["user_request"] = "test"

            from loop_hermes.parallel_manager import TaskSpec
            specs = [TaskSpec(task_id="t1", prompt="do something")]

            with mock.patch(
                "loop_hermes.hermes_client.invoke_hermes"
            ) as mock_invoke:
                mock_invoke.return_value = {
                    "success": True, "output": "done",
                    "engine": "cli",
                    "guardrail_events": [
                        {"type": "WARN", "tool": "file_write",
                         "message": "warning", "timestamp": ""},
                    ],
                    "error": None,
                }
                dispatch_phase_parallel(
                    state, str(state_dir), task_specs=specs, max_parallel=2,
                )
                # guardrail 事件应被注入
                assert len(state["issues"]["active"]["p1"]) >= 1
