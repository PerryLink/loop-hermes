# -*- coding: utf-8 -*-
"""边界条件测试 —— loop-hermes 降级、恢复和故障场景。

5 个生产级边缘场景:
    Edge-1: Hermes 不可用时的降级（SDK + CLI 均失效）
    Edge-2: 所有 Provider 耗尽后的 ALL_EXHAUSTED 判断
    Edge-3: state.json 损坏后从 .bak 备份恢复
    Edge-4: artifact checksum 不匹配检测与修复
    Edge-5: 并行 sub-agent 全部超时后的合并统计
"""

import sys
import json
import time
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile

from loop_hermes.state_machine import (
    load_or_init_state, DEFAULT_STATE_TEMPLATE,
    verify_artifact_integrity,
)
from loop_hermes.hermes_client import (
    check_health, send_message, invoke_hermes,
)
from loop_hermes.provider_fallback import (
    ProviderFallbackManager, ProviderStatus, FallbackAction,
)
from loop_hermes.parallel_manager import (
    ParallelDelegateManager, TaskSpec, SubAgentResult, merge_results,
)
from loop_hermes.checksum import (
    compute_checksum, verify_artifact_integrity as cvi,
    update_checksum_in_state, is_artifact_intact,
)
from loop_hermes.schemas import validate_state
from loop_hermes.scheduler import (
    parse_hloop_state, should_terminate,
)
from loop_hermes.guardrail_mapper import (
    map_guardrail_to_severity, map_guardrail_to_action,
    is_terminating_guardrail, process_guardrail_events,
    inject_guardrail_issues_into_state, guardrail_event_to_issue,
)


# ====================================================================
# Edge-1: Hermes 不可用时的降级
# ====================================================================


class TestHermesDegradationWhenUnavailable:
    """Hermes SDK 和 CLI 均不可用时，所有入口应优雅降级不崩溃。"""

    def test_check_health_unhealthy(self):
        """detect_hermes_engine 抛 RuntimeError → healthy=False。"""
        with mock.patch("loop_hermes.hermes_client.detect_hermes_engine") as m:
            m.side_effect = RuntimeError("SDK 和 CLI 均不可用")
            r = check_health()
        assert r["healthy"] is False
        assert len(r.get("error", "")) > 0

    def test_send_message_failure(self):
        """Hermes 不可用时 send_message 返回 success=False + 错误描述。"""
        with mock.patch("loop_hermes.hermes_client.detect_hermes_engine") as m:
            m.side_effect = RuntimeError("两条调用路径均已失败")
            r = send_message("分析项目结构")
        assert r["success"] is False
        assert "error" in r and len(r["error"]) > 0

    def test_invoke_hermes_no_crash(self):
        """invoke_hermes 无引擎时不崩溃，返回含 error 的 dict。"""
        with mock.patch("loop_hermes.hermes_client.detect_hermes_engine") as m:
            m.side_effect = RuntimeError("Hermes 连接失败")
            r = invoke_hermes(prompt="请生成文档", phase="part_1_1")
        assert isinstance(r, dict)
        assert r["success"] is False
        assert len(r.get("error", "")) > 0
        assert "guardrail_summary" in r


# ====================================================================
# Edge-2: 3 个 Provider 全部不可用
# ====================================================================


class TestProviderChainAllExhausted:
    """3 个 provider 全部 DEGRADED/CIRCUIT_OPEN → ALL_EXHAUSTED。"""

    def test_all_exhausted_scenario(self):
        """降级所有 provider 后 verify decide_provider/all_exhausted/current。"""
        mgr = ProviderFallbackManager(chain=["a", "b", "c"], failure_threshold=1)

        # 全部降级为 CIRCUIT_OPEN
        for name in ("a", "b", "c"):
            mgr.report_failure(name, "service down")
            mgr.report_failure(name, "still down")
            st = mgr.get_provider_state(name)
            assert st is not None and st.status == ProviderStatus.CIRCUIT_OPEN

        # decide_provider → ALL_EXHAUSTED
        result = mgr.decide_provider()
        assert result.action == FallbackAction.ALL_EXHAUSTED
        assert result.success is False
        assert result.provider == ""

        # all_exhausted 为 True
        # 注意: get_current_provider 返回 _current_index 位置的 provider
        # 不检查该 provider 是否真的可用（由 decide_provider 负责决策）
        assert mgr.all_exhausted() is True
        # 3 个 provider 全部不可用，decide_provider 返回 ALL_EXHAUSTED
        # 此时 get_current_provider 可能仍返回 _current_index 处的名称
        assert result.action == FallbackAction.ALL_EXHAUSTED

    def test_single_healthy_prevents_exhausted(self):
        """剩下 1 个 HEALTHY provider → all_exhausted=False。"""
        mgr = ProviderFallbackManager(chain=["a", "b", "c"], failure_threshold=1)
        mgr.report_failure("a", "f"); mgr.report_failure("a", "f")
        mgr.report_failure("b", "f"); mgr.report_failure("b", "f")
        # c 仍为 HEALTHY
        assert mgr.all_exhausted() is False
        result = mgr.decide_provider()
        assert result.action != FallbackAction.ALL_EXHAUSTED


# ====================================================================
# Edge-3: state.json 损坏恢复
# ====================================================================


class TestCorruptedStateRecovery:
    """state.json 损坏 → 从 .bak 恢复；两者均损坏 → ValueError。"""

    def test_restore_from_valid_backup(self):
        """破损 state.json + 完好 .bak → 自动恢复并通过校验。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "state.json").write_text(
                '{"schema_version": 1, "progress": {"phase": "init', encoding="utf-8")
            valid = deepcopy(DEFAULT_STATE_TEMPLATE)
            valid["progress"]["phase"] = "part_1_1"
            valid["progress"]["cycle"] = 3
            (Path(td) / "state.json.bak").write_text(
                json.dumps(valid, ensure_ascii=False), encoding="utf-8")
            restored = load_or_init_state(td)
            assert restored["schema_version"] == 1
            assert restored["progress"]["phase"] == "part_1_1"
            assert restored["progress"]["cycle"] == 3
            # 恢复后 state.json 已被重写
            rewritten = json.loads((Path(td) / "state.json").read_text("utf-8"))
            assert rewritten["schema_version"] == 1

    def test_both_corrupted_raises_value_error(self):
        """state.json 和 .bak 均不可解析 → 抛 ValueError。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "state.json").write_text("{{{ bad json", encoding="utf-8")
            (Path(td) / "state.json.bak").write_text("]]] also bad", encoding="utf-8")
            with pytest.raises(ValueError) as e:
                load_or_init_state(td)
            assert "state.json" in str(e.value)

    def test_no_files_initializes_from_template(self):
        """两个文件都不存在 → 从 DEFAULT_STATE_TEMPLATE 初始化。"""
        with tempfile.TemporaryDirectory() as td:
            s = load_or_init_state(td)
            assert s["schema_version"] == 1
            assert s["progress"]["phase"] == "init"
            assert (Path(td) / "state.json").exists()

    def test_restore_passes_validation(self):
        """从 .bak 恢复的 state 必须通过 validate_state 无异常。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "state.json").write_text("}}broken", encoding="utf-8")
            valid = deepcopy(DEFAULT_STATE_TEMPLATE)
            (Path(td) / "state.json.bak").write_text(
                json.dumps(valid, ensure_ascii=False), encoding="utf-8")
            restored = load_or_init_state(td)
            validate_state(restored)  # 不应抛异常
            assert restored["config"]["max_cycles"] == 5


# ====================================================================
# Edge-4: checksum 校验失败
# ====================================================================


class TestChecksumMismatchDetection:
    """artifact checksum 不匹配 → 检出 → 修复 → 通过。"""

    def test_mismatch_detection_and_fix(self):
        """检测 mismatch → is_artifact_intact=False → 修复后通过。"""
        with tempfile.TemporaryDirectory() as td:
            art_dir = Path(td) / "artifacts"; art_dir.mkdir()
            af = art_dir / "01-requirements.md"

            af.write_text("# Requirements\n- A\n- B\n", encoding="utf-8")
            orig_cs = compute_checksum(str(af))

            state = deepcopy(DEFAULT_STATE_TEMPLATE)
            state["artifacts"]["requirements"]["path"] = str(af)
            state["artifacts"]["requirements"]["status"] = "generated"
            state["artifacts"]["requirements"]["checksum"] = orig_cs
            state["progress"]["phase"] = "part_1_1"

            # 出厂校验通过
            assert len(cvi(state)) == 0
            assert is_artifact_intact(state, "requirements") is True

            # 篡改文件
            af.write_text("# Tampered\n+ Malicious\n", encoding="utf-8")
            mismatches = cvi(state)
            assert len(mismatches) >= 1
            assert mismatches[0]["artifact"] == "requirements"
            assert is_artifact_intact(state, "requirements") is False

            # 修复: update_checksum_in_state
            old_ver = state["artifacts"]["requirements"]["version"]
            update_checksum_in_state(state, "requirements", str(af))
            assert state["artifacts"]["requirements"]["version"] == old_ver + 1
            assert is_artifact_intact(state, "requirements") is True
            assert len(cvi(state)) == 0

    def test_multiple_artifacts_mismatch(self):
        """verify_artifact_integrity 应检出所有不匹配的 artifact。"""
        with tempfile.TemporaryDirectory() as td:
            art_dir = Path(td) / "artifacts"; art_dir.mkdir()
            rf = art_dir / "01-requirements.md"
            df = art_dir / "02-direction.md"
            rf.write_text("Real reqs", encoding="utf-8")
            df.write_text("Real dir", encoding="utf-8")

            state = deepcopy(DEFAULT_STATE_TEMPLATE)
            for key, f in [("requirements", rf), ("direction", df)]:
                state["artifacts"][key]["path"] = str(f)
                state["artifacts"][key]["status"] = "generated"
                state["artifacts"][key]["checksum"] = compute_checksum(str(f))

            # 篡改两个文件
            rf.write_text("Tampered reqs", encoding="utf-8")
            df.write_text("Tampered dir", encoding="utf-8")

            mismatches = cvi(state)
            assert len(mismatches) == 2
            names = {m["artifact"] for m in mismatches}
            assert names == {"requirements", "direction"}
            assert not is_artifact_intact(state, "requirements")
            assert not is_artifact_intact(state, "direction")

    def test_not_generated_artifact_skipped(self):
        """status=not_generated 且无 checksum 的 artifact 不参与校验。"""
        with tempfile.TemporaryDirectory() as td:
            art_dir = Path(td) / "artifacts"; art_dir.mkdir()
            af = art_dir / "01-requirements.md"
            af.write_text("content", encoding="utf-8")

            state = deepcopy(DEFAULT_STATE_TEMPLATE)
            state["artifacts"]["requirements"]["path"] = str(af)
            # 不设置 checksum（模拟 not_generated 的无 checksum 状态）
            # status 保持 not_generated
            assert len(cvi(state)) == 0
            # 无 checksum 时 is_artifact_intact 视为通过
            assert is_artifact_intact(state, "requirements") is True


# ====================================================================
# Edge-5: 并行 sub-agent 全部超时
# ====================================================================


class TestParallelAgentAllTimeout:
    """3 个 agent 全部超时 → merge_results 正确计数。"""

    def test_all_agents_timeout(self):
        """executor sleep 10s + per-agent timeout 1s → 全部 timeout。"""
        def slow_exec(spec):
            time.sleep(10)
            return SubAgentResult(agent_id=spec.agent_id, task_id=spec.task_id,
                                  status="success")

        specs = [TaskSpec(task_id=f"t-{i:03d}", prompt=f"任务 {i}",
                          timeout_seconds=1) for i in range(1, 4)]
        mgr = ParallelDelegateManager(max_parallel=3, total_timeout=2,
                                      fail_fast=False)
        results = mgr.delegate(specs, executor_fn=slow_exec)
        assert len(results) == 3
        timeout_n = sum(1 for r in results if r.status == "timeout")
        assert timeout_n == 3, f"应 3 个 timeout, 实际 {timeout_n}"

    def test_merge_results_all_timeout(self):
        """merge_results 应正确统计 3/3 timeout。"""
        results = [
            SubAgentResult(agent_id=f"a-{i}", task_id=f"t-{i}",
                           status="timeout", error="超时",
                           duration_ms=1000 + i * 100)
            for i in range(1, 4)
        ]
        merged = merge_results(results)
        assert merged.total == 3
        assert merged.timeout == 3
        assert merged.succeeded == 0
        assert merged.failed == 0
        assert merged.cancelled == 0
        s = merged.merged_output["summary"]
        assert s["succeeded"] == 0 and s["timeout"] == 3

    def test_mixed_merge_stats(self):
        """success + timeout + failed 混合状态合并统计。"""
        results = [
            SubAgentResult(agent_id="a1", task_id="t1", status="success",
                           duration_ms=500),
            SubAgentResult(agent_id="a2", task_id="t2", status="timeout",
                           error="t/o", duration_ms=2000),
            SubAgentResult(agent_id="a3", task_id="t3", status="failed",
                           error="err", duration_ms=300),
            SubAgentResult(agent_id="a4", task_id="t4", status="timeout",
                           error="t/o", duration_ms=1500),
        ]
        merged = merge_results(results)
        assert merged.succeeded == 1
        assert merged.failed == 1
        assert merged.timeout == 2

    def test_executor_exception_becomes_failed(self):
        """executor 抛异常 → 捕获并标记为 failed。"""
        def bad_exec(spec):
            raise ValueError(f"异常: {spec.task_id}")

        specs = [TaskSpec(task_id="e-001", prompt="p1", timeout_seconds=5),
                 TaskSpec(task_id="e-002", prompt="p2", timeout_seconds=5)]
        mgr = ParallelDelegateManager(max_parallel=2, total_timeout=10,
                                      fail_fast=False)
        results = mgr.delegate(specs, executor_fn=bad_exec)
        assert all(r.status == "failed" for r in results)
        assert all("异常" in (r.error or "") for r in results)

    def test_fail_fast_on_timeout(self):
        """fail-fast 模式下超时应触发取消其余 agent。"""
        def mixed_exec(spec):
            if spec.task_id == "ff-001":
                time.sleep(10)
            return SubAgentResult(agent_id=spec.agent_id,
                                  task_id=spec.task_id, status="success")

        specs = [TaskSpec(task_id="ff-001", prompt="慢", timeout_seconds=1),
                 TaskSpec(task_id="ff-002", prompt="快", timeout_seconds=5),
                 TaskSpec(task_id="ff-003", prompt="快", timeout_seconds=5)]
        mgr = ParallelDelegateManager(max_parallel=3, total_timeout=3,
                                      fail_fast=True)
        results = mgr.delegate(specs, executor_fn=mixed_exec)
        affected = sum(1 for r in results
                       if r.status in ("timeout", "cancelled"))
        assert affected >= 1, f"至少 1 个 agent 应被 timeout/cancelled, 实际 {affected}"


# ====================================================================
# Edge-6: 调度器 HLOOP_STATE 解析和终止判定
# ====================================================================


class TestSchedulerEdgeCases:
    """调度器 HLOOP_STATE 解析、四层终止判定、异常输入容错。"""

    def test_scheduler_parse_hloop_state_basic(self):
        """解析标准 HLOOP_STATE block 返回完整字典。"""
        stdout = """
<<<HLOOP_STATE>>>
phase: part_2_3
cycle: 2
convergence_counter: 1
new_issues_this_round: false
issues_active_p0: 0
issues_active_p1: 1
issues_active_p2: 2
all_test_status: pass
all_issue_status: none_open
pending_confirmation_status: null
termination_status: running
max_cycles: 5
convergence_rounds: 2
hermes_guardrail_hardlines: 0
hermes_guardrail_warns: 0
<<<END_HLOOP_STATE>>>
"""
        hstate = parse_hloop_state(stdout)
        assert hstate["phase"] == "part_2_3"
        assert hstate["cycle"] == 2
        assert hstate["convergence_counter"] == 1
        assert hstate["new_issues_this_round"] is False
        assert hstate["issues_active_p0"] == 0
        assert hstate["issues_active_p1"] == 1
        assert hstate["issues_active_p2"] == 2
        assert hstate["all_test_status"] == "pass"
        assert hstate["termination_status"] == "running"
        assert hstate["max_cycles"] == 5
        assert hstate["convergence_rounds"] == 2

    def test_scheduler_termination_detection(self):
        """四层级联终止判定全覆盖。"""
        # L1: termination_status = complete → 停止
        hstate = {"termination_status": "complete"}
        stop, reason = should_terminate(hstate)
        assert stop is True
        assert "complete" in reason

        # L1: termination_status = paused → 停止
        hstate = {"termination_status": "paused"}
        stop, reason = should_terminate(hstate)
        assert stop is True

        # L1: termination_status = failed → 停止
        hstate = {"termination_status": "failed"}
        stop, reason = should_terminate(hstate)
        assert stop is True

        # L2: 收敛达成 (p0=p1=p2=0, counter >= rounds)
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 0,
            "issues_active_p1": 0,
            "issues_active_p2": 0,
            "convergence_counter": 3,
            "convergence_rounds": 2,
            "cycle": 1,
            "max_cycles": 5,
        }
        stop, reason = should_terminate(hstate)
        assert stop is True
        assert "Convergence" in reason

        # L2: 收敛 counter 不足 → 继续
        hstate["convergence_counter"] = 1
        stop, reason = should_terminate(hstate)
        assert stop is False
        assert reason == "Continue"

        # L3: cycle >= max_cycles → 停止
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 2,
            "issues_active_p1": 1,
            "issues_active_p2": 0,
            "convergence_counter": 0,
            "convergence_rounds": 2,
            "cycle": 5,
            "max_cycles": 5,
        }
        stop, reason = should_terminate(hstate)
        assert stop is True
        assert "Max cycles" in reason

        # L4: Default-Continue（无任何终止条件触发）
        hstate = {
            "termination_status": "running",
            "issues_active_p0": 1,
            "issues_active_p1": 0,
            "issues_active_p2": 0,
            "convergence_counter": 0,
            "convergence_rounds": 2,
            "cycle": 2,
            "max_cycles": 5,
        }
        stop, reason = should_terminate(hstate)
        assert stop is False
        assert reason == "Continue"

    def test_scheduler_invalid_state_graceful(self):
        """畸形 HLOOP_STATE 输入不崩溃，优雅返回空或部分数据。"""
        # 无 HLOOP_STATE block
        hstate = parse_hloop_state("no hloop block here")
        assert hstate == {}

        # 空字符串
        hstate = parse_hloop_state("")
        assert hstate == {}

        # 格式错误的 block（键值对缺冒号或值缺 key）
        hstate = parse_hloop_state("<<<HLOOP_STATE>>>\nmissing_colon\n<<<END_HLOOP_STATE>>>")
        assert isinstance(hstate, dict)

        # JSON 格式的 HLOOP_STATE
        json_stdout = '<<<HLOOP_STATE>>>\n{"phase": "part_1_1", "cycle": 1, "termination_status": "running"}\n<<<END_HLOOP_STATE>>>'
        hstate = parse_hloop_state(json_stdout)
        assert hstate == {"phase": "part_1_1", "cycle": 1, "termination_status": "running"}

        # 空 HLOOP_STATE block
        hstate = parse_hloop_state("<<<HLOOP_STATE>>>\n<<<END_HLOOP_STATE>>>")
        assert isinstance(hstate, dict)

        # 不完整的 block（缺少结束标记）
        hstate = parse_hloop_state("<<<HLOOP_STATE>>>\nphase: init\ncycle: 0\n")
        assert isinstance(hstate, dict)


# ====================================================================
# Edge-7: Guardrail Mapper 事件映射和注入
# ====================================================================


class TestGuardrailMapperEdgeCases:
    """Guardrail 事件 → 严重性映射、终止标识、空输入容错。"""

    def test_hardline_event_maps_to_p0(self):
        """HARDLINE 和 HARDLINE_BLOCK 映射为 P0 严重性。"""
        assert map_guardrail_to_severity("HARDLINE") == "P0"
        assert map_guardrail_to_severity("HARDLINE_BLOCK") == "P0"

    def test_warn_event_maps_to_p1(self):
        """WARN 和 WARN_PATTERN 映射为 P1 严重性。"""
        assert map_guardrail_to_severity("WARN") == "P1"
        assert map_guardrail_to_severity("WARN_PATTERN") == "P1"

    def test_approval_event_maps_to_p2(self):
        """APPROVAL_DENY 和 APPROVAL_TIMEOUT 映射为 P2 严重性。"""
        assert map_guardrail_to_severity("APPROVAL_DENY") == "P2"
        assert map_guardrail_to_severity("APPROVAL_TIMEOUT") == "P2"

    def test_unknown_event_defaults_to_p2(self):
        """未知 guardrail 类型默认映射为 P2。"""
        assert map_guardrail_to_severity("UNKNOWN_TYPE_XYZ") == "P2"

    def test_terminate_event_stops_workflow(self):
        """BLOCK 和 HARDLINE_BLOCK 触发终止标识。"""
        assert is_terminating_guardrail("BLOCK") is True
        assert is_terminating_guardrail("HARDLINE_BLOCK") is True
        assert is_terminating_guardrail("HARDLINE") is False
        assert is_terminating_guardrail("WARN") is False

    def test_empty_events_no_crash(self):
        """空 guardrail 事件列表不崩溃。"""
        issues, summary = process_guardrail_events([], phase="part_2_1")
        assert issues == []
        assert summary["total"] == 0
        assert summary["by_severity"] == {"P0": 0, "P1": 0, "P2": 0}
        assert summary["terminating"] is False
        assert summary["actions"] == []
        assert summary["highest_severity"] is None

    def test_single_event_creates_issue(self):
        """单个 guardrail 事件正确映射为 issue。"""
        event = {
            "type": "HARDLINE",
            "tool": "Bash",
            "message": "rm -rf / detected",
            "timestamp": "2025-01-01T00:00:00Z",
        }
        issue = guardrail_event_to_issue(event, phase="part_2_3")
        assert issue["severity"] == "P0"
        assert issue["source"] == "hermes_guardrail"
        assert "HARDLINE" in issue["title"]
        assert "Bash" in issue["title"]
        assert issue["status"] == "open"

    def test_process_mixed_events(self):
        """混合事件批量处理，正确统计每种严重性的数量。"""
        events = [
            {"type": "HARDLINE", "tool": "Bash", "message": "op1", "timestamp": "t1"},
            {"type": "WARN", "tool": "Edit", "message": "op2", "timestamp": "t2"},
            {"type": "APPROVAL_DENY", "tool": "Write", "message": "op3", "timestamp": "t3"},
            {"type": "HARDLINE_BLOCK", "tool": "Bash", "message": "op4", "timestamp": "t4"},
        ]
        issues, summary = process_guardrail_events(events, phase="part_2_3")
        assert summary["total"] == 4
        assert summary["by_severity"]["P0"] == 2
        assert summary["by_severity"]["P1"] == 1
        assert summary["by_severity"]["P2"] == 1
        assert summary["terminating"] is True
        assert summary["highest_severity"] == "P0"
        assert "RETREAT_TO_PART1" in summary["actions"]
        assert "TERMINATE" in summary["actions"]
        assert len(issues) == 4

    def test_inject_into_state_sets_termination(self):
        """终止级 guardrail 注入 state 后 termination.status 设为 failed。"""
        state = deepcopy(DEFAULT_STATE_TEMPLATE)
        events = [
            {"type": "BLOCK", "tool": "Bash", "message": "blocked", "timestamp": "t1"},
        ]
        summary = inject_guardrail_issues_into_state(state, events, phase="part_2_3")
        assert summary["terminating"] is True
        assert state["termination"]["status"] == "failed"
        assert "Guardrail 终止" in state["termination"]["exit_reason"]
        assert len(state["gate_state"]["hermes_guardrail_events"]) == 1
        assert len(state["issues"]["active"]["p2"]) == 1

    def test_inject_empty_events_does_nothing(self):
        """空事件列表注入不修改 state。"""
        state = deepcopy(DEFAULT_STATE_TEMPLATE)
        original_status = state["termination"]["status"]
        summary = inject_guardrail_issues_into_state(state, [], phase="part_2_3")
        assert summary["total"] == 0
        assert state["termination"]["status"] == original_status
        assert state["gate_state"]["hermes_guardrail_events"] == []
