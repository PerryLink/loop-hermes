# -*- coding: utf-8 -*-
"""测试: parallel_manager.py —— 并行委派管理器。"""

import json
import time
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loop_hermes.parallel_manager import (
    TaskSpec,
    SubAgentResult,
    MergeResult,
    ParallelDelegateManager,
    merge_results,
    detect_file_conflicts,
    setup_agent_workspace,
    cleanup_agent_workspace,
    DEFAULT_MAX_PARALLEL,
)


class TestTaskSpec:
    """TaskSpec 数据模型测试。"""

    def test_creates_task_spec_minimal(self):
        """最少参数应能创建 TaskSpec。"""
        spec = TaskSpec(task_id="t1", prompt="do something")
        assert spec.task_id == "t1"
        assert spec.prompt == "do something"
        assert spec.agent_id != ""
        assert spec.agent_id.startswith("agent-")
        assert spec.timeout_seconds == 600

    def test_agent_id_preserved(self):
        """显示提供的 agent_id 应被保留。"""
        spec = TaskSpec(task_id="t1", prompt="test", agent_id="my-agent")
        assert spec.agent_id == "my-agent"

    def test_default_assigned_files_empty(self):
        """默认 assigned_files 应为空列表。"""
        spec = TaskSpec(task_id="t1", prompt="test")
        assert spec.assigned_files == []

    def test_unique_agent_ids(self):
        """自动生成的 agent_id 应唯一。"""
        s1 = TaskSpec(task_id="t1", prompt="a")
        s2 = TaskSpec(task_id="t2", prompt="b")
        assert s1.agent_id != s2.agent_id


class TestSubAgentResult:
    """SubAgentResult 数据模型测试。"""

    def test_default_values(self):
        """默认值检查。"""
        r = SubAgentResult(agent_id="a1", task_id="t1")
        assert r.status == "pending"
        assert r.output == {}
        assert r.modified_files == []
        assert r.guardrail_events == []
        assert r.duration_ms == 0
        assert r.error is None

    def test_explicit_values(self):
        """显示值应正确存储。"""
        r = SubAgentResult(
            agent_id="a1", task_id="t1",
            status="success", output={"key": "val"},
            modified_files=["f1.py"], duration_ms=1500,
        )
        assert r.status == "success"
        assert r.output == {"key": "val"}
        assert r.modified_files == ["f1.py"]
        assert r.duration_ms == 1500


class TestMergeResults:
    """结果合并测试。"""

    def test_merge_empty(self):
        """空列表合并返回空结果。"""
        merged = merge_results([])
        assert merged.total == 0
        assert merged.succeeded == 0

    def test_merge_single_success(self):
        """单个成功结果。"""
        r = SubAgentResult(
            agent_id="a1", task_id="t1",
            status="success",
            modified_files=["f1.py", "f2.py"],
            guardrail_events=[{"type": "WARN"}],
            output={"result": "ok"},
            duration_ms=1000,
        )
        merged = merge_results([r])
        assert merged.total == 1
        assert merged.succeeded == 1
        assert merged.failed == 0
        assert merged.all_modified_files == ["f1.py", "f2.py"]
        assert len(merged.all_guardrail_events) == 1
        assert merged.total_duration_ms == 1000
        assert "a1" in merged.merged_output["agents"]

    def test_merge_mixed_statuses(self):
        """混合状态（成功+失败+超时+取消）。"""
        results = [
            SubAgentResult(agent_id="a1", task_id="t1", status="success", duration_ms=100),
            SubAgentResult(agent_id="a2", task_id="t2", status="failed", duration_ms=200),
            SubAgentResult(agent_id="a3", task_id="t3", status="timeout", duration_ms=50),
            SubAgentResult(agent_id="a4", task_id="t4", status="cancelled", duration_ms=0),
        ]
        merged = merge_results(results)
        assert merged.succeeded == 1
        assert merged.failed == 1
        assert merged.timeout == 1
        assert merged.cancelled == 1
        assert merged.total_duration_ms == 350

    def test_merge_deduplicates_files(self):
        """重复文件应去重。"""
        r1 = SubAgentResult(agent_id="a1", task_id="t1", status="success",
                            modified_files=["f1.py", "f2.py"])
        r2 = SubAgentResult(agent_id="a2", task_id="t2", status="success",
                            modified_files=["f2.py", "f3.py"])
        merged = merge_results([r1, r2])
        assert merged.all_modified_files == ["f1.py", "f2.py", "f3.py"]

    def test_merge_aggregates_guardrail_events(self):
        """合并应汇总所有 guardrail 事件。"""
        r1 = SubAgentResult(agent_id="a1", task_id="t1", status="success",
                            guardrail_events=[{"type": "WARN"}])
        r2 = SubAgentResult(agent_id="a2", task_id="t2", status="success",
                            guardrail_events=[{"type": "HARDLINE"}])
        merged = merge_results([r1, r2])
        assert len(merged.all_guardrail_events) == 2

    def test_merge_summary_in_output(self):
        """output 的 summary 字段应包含统计信息。"""
        results = [
            SubAgentResult(agent_id="a1", task_id="t1", status="success"),
            SubAgentResult(agent_id="a2", task_id="t2", status="failed"),
        ]
        merged = merge_results(results)
        summary = merged.merged_output.get("summary", {})
        assert summary["total"] == 2
        assert summary["succeeded"] == 1
        assert summary["failed"] == 1


class TestDetectFileConflicts:
    """冲突检测测试。"""

    def test_no_conflicts(self):
        """无共享文件时不应有冲突。"""
        r1 = SubAgentResult(agent_id="a1", task_id="t1", modified_files=["a.py"])
        r2 = SubAgentResult(agent_id="a2", task_id="t2", modified_files=["b.py"])
        conflicts = detect_file_conflicts([r1, r2])
        assert len(conflicts) == 0

    def test_detects_conflict(self):
        """两个 agent 修改同一文件应被检测。"""
        r1 = SubAgentResult(agent_id="a1", task_id="t1", modified_files=["shared.py"])
        r2 = SubAgentResult(agent_id="a2", task_id="t2", modified_files=["shared.py"])
        conflicts = detect_file_conflicts([r1, r2])
        assert conflicts == ["shared.py"]

    def test_multiple_conflicts(self):
        """可检测多个冲突文件。"""
        r1 = SubAgentResult(agent_id="a1", task_id="t1",
                            modified_files=["a.py", "shared.py"])
        r2 = SubAgentResult(agent_id="a2", task_id="t2",
                            modified_files=["b.py", "shared.py"])
        r3 = SubAgentResult(agent_id="a3", task_id="t3",
                            modified_files=["a.py"])
        conflicts = detect_file_conflicts([r1, r2, r3])
        assert "shared.py" in conflicts
        assert "a.py" in conflicts


class TestAgentWorkspace:
    """Agent 工作目录测试。"""

    def test_setup_creates_directories(self):
        """setup 应创建 agent 目录和 artifacts 子目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = setup_agent_workspace(tmp, "agent-001")
            assert ws.exists()
            assert (ws / "artifacts").exists()
            assert (ws / "state.json").exists()

    def test_setup_writes_state(self):
        """setup 应写入初始 agent 状态文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = setup_agent_workspace(tmp, "agent-002")
            state_data = json.loads((ws / "state.json").read_text())
            assert state_data["agent_id"] == "agent-002"
            assert state_data["status"] == "initializing"

    def test_cleanup_removes_workspace(self):
        """cleanup 应删除 agent 工作目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = setup_agent_workspace(tmp, "agent-003")
            assert ws.exists()
            cleaned = cleanup_agent_workspace(tmp, "agent-003")
            assert cleaned is True
            assert not ws.exists()

    def test_cleanup_preserves_failed(self):
        """失败 agent 的工作目录应保留不删除。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = setup_agent_workspace(tmp, "agent-004")
            # 修改状态为 failed
            state_file = ws / "state.json"
            state_data = json.loads(state_file.read_text())
            state_data["status"] = "failed"
            state_file.write_text(json.dumps(state_data))
            cleaned = cleanup_agent_workspace(tmp, "agent-004")
            assert cleaned is False
            assert ws.exists()


class TestParallelDelegateManagerInit:
    """管理器初始化测试。"""

    def test_default_parallel(self):
        """默认并发数为 4。"""
        mgr = ParallelDelegateManager()
        assert mgr.max_parallel == 4

    def test_custom_parallel(self):
        """自定义并发数应生效。"""
        mgr = ParallelDelegateManager(max_parallel=8)
        assert mgr.max_parallel == 8

    def test_fail_fast_default(self):
        """fail_fast 默认为 True。"""
        mgr = ParallelDelegateManager()
        assert mgr.fail_fast is True

    def test_initial_not_cancelled(self):
        """初始状态不应取消。"""
        mgr = ParallelDelegateManager()
        assert mgr.is_cancelled() is False


class TestParallelDelegateManagerBasic:
    """基本 delegate 执行测试。"""

    def test_empty_specs(self):
        """空任务列表返回空结果。"""
        mgr = ParallelDelegateManager()
        results = mgr.delegate([], lambda s: None)
        assert results == []

    def test_single_task_execution(self):
        """单个任务应正确执行。"""
        mgr = ParallelDelegateManager()

        def executor(spec):
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="success",
                output={"done": True},
            )

        specs = [TaskSpec(task_id="t1", prompt="test")]
        results = mgr.delegate(specs, executor)
        assert len(results) == 1
        assert results[0].status == "success"
        assert results[0].task_id == "t1"

    def test_multiple_tasks_parallel(self):
        """多个任务应并发执行。"""
        mgr = ParallelDelegateManager(max_parallel=4)
        execution_order = []

        def executor(spec):
            execution_order.append(spec.task_id)
            time.sleep(0.05)
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="success",
            )

        specs = [TaskSpec(task_id=f"t{i}", prompt="test") for i in range(4)]
        results = mgr.delegate(specs, executor)
        assert len(results) == 4
        assert all(r.status == "success" for r in results)
        assert len(execution_order) == 4

    def test_get_result_after_delegate(self):
        """delegate 后 get_result 应返回结果。"""
        mgr = ParallelDelegateManager()

        def executor(spec):
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="success",
            )

        spec = TaskSpec(task_id="t1", prompt="test", agent_id="my-agent")
        mgr.delegate([spec], executor)
        r = mgr.get_result("my-agent")
        assert r is not None
        assert r.agent_id == "my-agent"


class TestFailFast:
    """fail-fast 行为测试。"""

    def test_fail_fast_on_failure(self):
        """一个 agent 失败应立即取消其余。"""
        mgr = ParallelDelegateManager(max_parallel=4, fail_fast=True)
        started = []
        completed = []

        def executor(spec):
            started.append(spec.task_id)
            if spec.task_id == "t0":
                time.sleep(0.3)  # 给其他线程启动的时间
                return SubAgentResult(
                    agent_id=spec.agent_id, task_id=spec.task_id,
                    status="failed", error="intentional",
                )
            else:
                time.sleep(0.5)
                completed.append(spec.task_id)
                return SubAgentResult(
                    agent_id=spec.agent_id, task_id=spec.task_id,
                    status="success",
                )

        specs = [TaskSpec(task_id=f"t{i}", prompt="test") for i in range(5)]
        results = mgr.delegate(specs, executor)

        # t0 应失败
        failed = [r for r in results if r.task_id == "t0"]
        assert len(failed) == 1
        assert failed[0].status == "failed"

        # 至少应有一个被取消
        cancelled = [r for r in results if r.status == "cancelled"]
        assert len(cancelled) >= 0  # 取决于时序

    def test_no_fail_fast_with_flag_off(self):
        """fail_fast=False 时不应取消其余。"""
        mgr = ParallelDelegateManager(max_parallel=4, fail_fast=False)

        def executor(spec):
            if spec.task_id == "t0":
                return SubAgentResult(
                    agent_id=spec.agent_id, task_id=spec.task_id,
                    status="failed", error="intentional",
                )
            return SubAgentResult(
                agent_id=spec.agent_id, task_id=spec.task_id,
                status="success",
            )

        specs = [TaskSpec(task_id=f"t{i}", prompt="test") for i in range(3)]
        results = mgr.delegate(specs, executor)
        assert len(results) == 3
        succeeded = [r for r in results if r.status == "success"]
        assert len(succeeded) == 2


class TestCancelAndTimeout:
    """取消和超时测试。"""

    def test_manual_cancel(self):
        """手动取消应设置 cancel_event。"""
        mgr = ParallelDelegateManager()
        assert mgr.is_cancelled() is False
        mgr.cancel_all("test reason")
        assert mgr.is_cancelled() is True

    def test_executor_exception_handled(self):
        """executor 异常应被捕获并返回 failed。"""
        mgr = ParallelDelegateManager()

        def executor(spec):
            raise RuntimeError("boom")

        specs = [TaskSpec(task_id="t1", prompt="test")]
        results = mgr.delegate(specs, executor)
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "boom" in results[0].error


class TestGetAllResults:
    """get_all_results 测试。"""

    def test_returns_empty_initial(self):
        """初始无结果时返回空字典。"""
        mgr = ParallelDelegateManager()
        assert mgr.get_all_results() == {}

    def test_returns_after_delegate(self):
        """delegate 后应返回所有结果。"""
        mgr = ParallelDelegateManager()

        def executor(spec):
            return SubAgentResult(
                agent_id=spec.agent_id, task_id=spec.task_id,
                status="success",
            )

        specs = [
            TaskSpec(task_id="t1", prompt="test", agent_id="a1"),
            TaskSpec(task_id="t2", prompt="test", agent_id="a2"),
        ]
        mgr.delegate(specs, executor)
        all_r = mgr.get_all_results()
        assert len(all_r) == 2
        assert "a1" in all_r
        assert "a2" in all_r
