# -*- coding: utf-8 -*-
"""黄金路径集成测试 —— 3 个端到端场景，覆盖核心流程。

测试场景:
    Golden-1  正常全流程: init -> part_1_1 -> ... -> part_2_8 -> routing -> 收敛
    Golden-2  P0 回退 + Provider 降级: 路由回退 + 5 次失败触发降级
    Golden-3  并行子 agent + 收敛: 并行派发、结果合并、收敛判定
"""

import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.state_machine import (
    DEFAULT_STATE_TEMPLATE,
    load_or_init_state,
    atomic_write_state,
    is_converged,
    is_terminated,
)
from loop_hermes.phase_dispatch import dispatch_phase
from loop_hermes.provider_fallback import (
    ProviderFallbackManager,
    ProviderStatus,
    FallbackAction,
    get_global_fallback_manager,
)
from loop_hermes.parallel_manager import (
    TaskSpec,
    SubAgentResult,
    MergeResult,
    ParallelDelegateManager,
    merge_results,
    detect_file_conflicts,
)
from loop_hermes.routing import execute_routing


# ============================================================================
# 通用辅助函数与常量
# ============================================================================

def _fake_hermes_ok(output_text="Phase executed successfully."):
    """构造伪造的 Hermes 成功响应。

    Args:
        output_text: 模拟输出文本

    Returns:
        成功响应字典
    """
    return {
        "success": True,
        "output": output_text,
        "engine": "mock",
        "guardrail_events": [],
        "guardrail_summary": None,
        "error": None,
    }


def _artifacts_dir(state_dir):
    """获取 artifacts 目录 Path，不存在则创建。"""
    art = Path(state_dir) / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    return art


def _write_artifact(state_dir, filename, content="mock content"):
    """在 artifacts 目录创建仿造文件并返回其路径。

    Args:
        state_dir: state.json 所在目录
        filename: 文件名
        content: 文件内容

    Returns:
        创建文件的绝对路径字符串
    """
    fpath = _artifacts_dir(state_dir) / filename
    fpath.write_text(content, encoding="utf-8")
    return str(fpath)


# Part 2.8 硬闸门所需的 artifact 文件
_HARD_GATE_FILES = [
    "04-implementation-plan.md", "05-task-list.json",
    "05b-implementation-diff.patch", "06-code-review.md",
    "08-test-results.json", "09-issue-list.json",
]

# Phase -> artifact 文件名映射（用于确保文件存在 + 校验 checksum）
_PHASE_ART_MAP = {
    "init": "context-summary.md",
    "part_1_1": "01-requirements.md",
    "part_1_2": "02-direction.md",
    "part_1_3": "03-solution.md",
    "part_2_1": "04-implementation-plan.md",
    "part_2_2": "05b-implementation-diff.patch",
    "part_2_3": "06-code-review.md",
    "part_2_5": "07-test-plan.md",
    "part_2_6": "08-test-results.json",
    "part_2_7": "09-issue-list.json",
    "part_2_8": "10-verification.md",
}

# 全部完成的任务列表（确保硬闸门 task check 通过）
_TASKS_COMPLETED_JSON = json.dumps({
    "tasks": [
        {"id": "t-a", "title": "Implement core logic",
         "status": "completed", "assigned_files": ["src/core.py"], "module": "core"},
        {"id": "t-b", "title": "Write unit tests",
         "status": "completed", "assigned_files": ["tests/test_core.py"], "module": "tests"},
    ],
})

# 全部通过的测试结果（确保不产生 test-failure issue）
_TESTS_PASSING_JSON = json.dumps({
    "results": [
        {"id": "r1", "name": "test_happy_path", "status": "pass"},
        {"id": "r2", "name": "test_edge_case", "status": "pass"},
    ],
})

_ISSUES_EMPTY_JSON = json.dumps({"issues": []})


def _setup_all_artifacts(state_dir):
    """预创建全部 artifact 文件以支撑完整流程。

    覆盖硬闸门必需文件、各 phase 对应 artifact、以及特殊 JSON 文件。

    Args:
        state_dir: state.json 所在目录路径
    """
    d = _artifacts_dir(state_dir)
    all_names = set(_HARD_GATE_FILES)
    for v in _PHASE_ART_MAP.values():
        all_names.add(v)
    for fname in all_names:
        fpath = d / fname
        if fpath.exists():
            continue
        if fname == "05-task-list.json":
            fpath.write_text(_TASKS_COMPLETED_JSON, encoding="utf-8")
        elif fname == "08-test-results.json":
            fpath.write_text(_TESTS_PASSING_JSON, encoding="utf-8")
        elif fname == "09-issue-list.json":
            fpath.write_text(_ISSUES_EMPTY_JSON, encoding="utf-8")
        else:
            fpath.write_text(f"mock artifact content for {fname}", encoding="utf-8")


class FakeArgs:
    """CLI 参数模拟对象。"""
    state_dir = ""
    safe = False
    unsafe = False
    interactive = False
    goal = "Build a weather CLI application"
    max_cycles = 3
    convergence_rounds = 2
    hermes_model = "claude-sonnet-4-20250514"
    hermes_toolsets = "code,shell"
    provider_fallback = "claude,openai,deepseek"
    skip_testing = False
    max_part1_rounds = 5


# ============================================================================
# Golden-1: 正常全流程（需求 -> 设计 -> 实施 -> 验证）
# ============================================================================

class TestGoldenFullFlow:
    """Golden-1: 正常全流程端到端测试。

    模拟 init -> part_1_1 -> part_1_2 -> part_1_3 -> part_2_1 ->
    part_2_2 -> part_2_3 -> part_2_4 -> part_2_5 -> part_2_6 ->
    part_2_7 -> part_2_8 -> routing -> convergence 的完整链路。
    """

    def test_full_flow_init_to_convergence(self):
        """完整 13 阶段推进，验证 phase 递进、checksum 更新与终止状态。

        步骤:
            1. 从模板初始化全新 state
            2. 预创建全部 artifact 文件（确保 checksum 可更新、硬闸门可通过）
            3. Mock Hermes 为始终返回成功
            4. 循环调用 dispatch_phase 直至终止
            5. 验证关键 phase 均已到达
            6. 验证 artifact checksum 已更新
            7. 验证终止状态为 "complete"、无活跃 issue
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            sdir = str(state_dir)

            args = FakeArgs()
            args.state_dir = sdir
            state = load_or_init_state(sdir, args)
            assert state["progress"]["phase"] == "init"
            assert state["config"]["convergence_rounds"] == 2

            # 预创建全部 artifact 文件
            _setup_all_artifacts(sdir)

            visited = []
            max_iter = 80
            with mock.patch(
                'loop_hermes.hermes_client.invoke_hermes',
                return_value=_fake_hermes_ok("Phase done: artifact generated."),
            ):
                for _ in range(max_iter):
                    if is_terminated(state):
                        break
                    visited.append(state["progress"]["phase"])
                    dispatch_phase(state, sdir)

            # ---- 断言 ----
            assert len(visited) < max_iter, (
                f"流程未在 {max_iter} 次内完成 (visited={len(visited)})"
            )
            # 关键 phase 均到达
            for p in ("init", "part_1_1", "part_2_1", "part_2_8", "routing"):
                assert p in visited, f"未到达 phase [{p}]"
            assert visited[0] == "init"

            # 终止状态
            assert is_terminated(state)
            assert state["termination"]["status"] == "complete", (
                f"终止状态应为 complete，实际 {state['termination']['status']}"
            )
            assert state["termination"]["exit_reason"] is not None

            # 收敛条件: convergence_counter >= convergence_rounds
            cc = state["progress"]["convergence_counter"]
            cr = state["config"]["convergence_rounds"]
            assert cc >= cr, f"convergence_counter({cc}) < rounds({cr})"

            # 无活跃 issue
            act = state["issues"]["active"]
            total_act = len(act["p0"]) + len(act["p1"]) + len(act["p2"])
            assert total_act == 0, f"收敛后不应有活跃 issue (实际 {total_act})"

            # 关键 artifact checksum 已更新
            for ak in ("requirements", "solution", "impl_plan",
                       "test_results", "verification"):
                info = state["artifacts"].get(ak, {})
                if info.get("status") in ("generated", "updated"):
                    assert info.get("checksum") is not None, (
                        f"artifact [{ak}] status={info['status']} checksum=None"
                    )

            # 转移历史
            trans = state["progress"].get("phase_transitions", [])
            assert len(trans) > 0
            assert any(t["from"] == "init" and t["to"] == "part_1_1"
                       for t in trans), "应记录 init -> part_1_1 转移"

            # cycle >= 2
            assert state["progress"]["cycle"] >= 2, (
                f"cycle 应 >= 2，实际 {state['progress']['cycle']}"
            )

            # routing_history 有记录
            rh = state.get("routing_history", [])
            assert len(rh) > 0, "路由历史不应为空"

    def test_phase_progression_matches_schema(self):
        """init -> part_1_1 的单步推进验证。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            sdir = str(state_dir)
            state = load_or_init_state(sdir)
            _setup_all_artifacts(sdir)

            with mock.patch(
                'loop_hermes.hermes_client.invoke_hermes',
                return_value=_fake_hermes_ok("init done"),
            ):
                result = dispatch_phase(state, sdir)

            assert result["phase"] == "part_1_1", (
                f"init 之后应为 part_1_1，实际 {result['phase']}"
            )
            assert result["status"] == "ok"
            assert state["progress"]["phase"] == "part_1_1"

    def test_convergence_after_all_issues_cleared(self):
        """所有 issue 关闭 + counter 达标时 is_converged 和 routing 均返回完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            sdir = str(state_dir)
            state = load_or_init_state(sdir)

            state["issues"]["active"] = {"p0": [], "p1": [], "p2": []}
            state["progress"]["convergence_counter"] = 3
            state["config"]["convergence_rounds"] = 2
            state["progress"]["phase"] = "routing"

            assert is_converged(state)

            decision = execute_routing(state)
            assert decision.get("target") == "complete", (
                f"收敛应返回 complete，实际 {decision.get('target')}"
            )


# ============================================================================
# Golden-2: P0 回退 + Provider 降级
# ============================================================================

class TestGoldenRollbackAndFallback:
    """Golden-2: P0 回退路由 + Provider 降级链测试。

    验证:
        - routing 阶段发现 P0 issue -> 回退到 part_1_1
        - ProviderFallbackManager 5 次失败 -> DEGRADED -> 自动回退
    """

    def _make_p0_state(self):
        """构建带有 P0 issue 且 phase=routing 的 state。

        Returns:
            state 字典
        """
        s = deepcopy(DEFAULT_STATE_TEMPLATE)
        s["progress"]["phase"] = "routing"
        s["progress"]["cycle"] = 1
        s["config"]["user_request"] = "Build a secure auth system"
        s["config"]["convergence_rounds"] = 2
        s["issues"]["active"]["p0"].append({
            "id": "p0-crit-001",
            "severity": "P0",
            "title": "Critical auth vulnerability",
            "description": "Authentication bypass found in auth module",
            "source": "security_audit",
            "affected_files": ["src/auth.py", "src/session.py"],
            "status": "open",
            "fix_strategy": "Redesign authentication mechanism",
        })
        return s

    def test_p0_routing_sends_back_to_part1_1(self):
        """P0 存在时路由应回退到 part_1_1 重新设计。

        验证 target、reason、phase 更新、history 记录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / ".hermes" / "loop-hermes"
            base.mkdir(parents=True, exist_ok=True)
            sdir = str(base)
            state = self._make_p0_state()
            atomic_write_state(state, sdir)

            decision = execute_routing(state)

            assert decision["target"] == "part_1_1", (
                f"P0 应路由到 part_1_1，实际 {decision['target']}"
            )
            assert "P0" in decision["reason"]

            assert state["progress"]["phase"] == "part_1_1"

            rh = state.get("routing_history", [])
            assert len(rh) == 1
            assert rh[0]["to"] == "part_1_1"
            assert "P0" in rh[0]["reason"]

            # P0 回退不应设 repair_context
            rc = state["progress"].get("repair_context")
            assert rc is None or rc == {}, f"P0 不应设 repair_context，实际 {rc}"

            assert state["progress"]["cycle"] == 2

    def test_provider_degradation_after_5_failures(self):
        """claude 连续 5 次失败 -> DEGRADED -> decide 返回 openai。

        验证:
            1. 初始全 HEALTHY
            2. 5 次 report_failure -> DEGRADED
            3. decide_provider() 返回 openai
            4. reset 后恢复 HEALTHY
        """
        mgr = ProviderFallbackManager(
            chain=["claude", "openai", "deepseek"], failure_threshold=5,
        )

        for name in mgr.chain:
            assert mgr.get_provider_state(name).status == ProviderStatus.HEALTHY

        r1 = mgr.decide_provider()
        assert r1.provider == "claude"
        assert r1.action == FallbackAction.USE_CURRENT

        for i in range(5):
            mgr.report_failure("claude", f"fail #{i + 1}")

        cs = mgr.get_provider_state("claude")
        assert cs.status == ProviderStatus.DEGRADED, (
            f"5 次失败后应为 DEGRADED，实际 {cs.status}"
        )
        assert cs.failure_count >= 5

        r2 = mgr.decide_provider()
        assert r2.provider == "openai", (
            f"降级后应回退到 openai，实际 {r2.provider}"
        )
        assert r2.action in (FallbackAction.USE_CURRENT, FallbackAction.FALLBACK)

        assert mgr.get_provider_state("openai").status == ProviderStatus.HEALTHY
        assert mgr.all_exhausted() is False

        mgr.reset_provider("claude")
        restored = mgr.get_provider_state("claude")
        assert restored.status == ProviderStatus.HEALTHY
        assert restored.failure_count == 0

    def test_success_recovers_degraded_provider(self):
        """Provider 调用成功后自动恢复 HEALTHY。"""
        mgr = ProviderFallbackManager(
            chain=["claude", "openai"], failure_threshold=3,
        )
        for _ in range(3):
            mgr.report_failure("claude", "err")
        assert mgr.get_provider_state("claude").status == ProviderStatus.DEGRADED
        mgr.report_success("claude")
        assert mgr.get_provider_state("claude").status == ProviderStatus.HEALTHY
        assert mgr.get_provider_state("claude").failure_count == 0

    def test_chain_continues_past_second_provider(self):
        """第二个 provider 也降级后继续切换到第三个。"""
        mgr = ProviderFallbackManager(
            chain=["claude", "openai", "deepseek"], failure_threshold=3,
        )
        for _ in range(3):
            mgr.report_failure("claude", "err")
        assert mgr.states["claude"].status == ProviderStatus.DEGRADED

        r = mgr.decide_provider()
        assert r.provider == "openai"

        for _ in range(3):
            mgr.report_failure("openai", "err")
        assert mgr.states["openai"].status == ProviderStatus.DEGRADED

        r = mgr.decide_provider()
        assert r.provider == "deepseek", (
            f"openai 降级后应切到 deepseek，实际 {r.provider}"
        )

    def test_all_exhausted_when_none_healthy(self):
        """所有 provider 均非 HEALTHY 时 all_exhausted() -> True。"""
        mgr = ProviderFallbackManager(
            chain=["claude", "openai"], failure_threshold=2,
        )
        for p in ("claude", "openai"):
            for _ in range(2):
                mgr.report_failure(p, "error")
        assert mgr.all_exhausted() is True

    def test_global_fallback_manager_singleton(self):
        """全局 get_global_fallback_manager 返回单例。"""
        a = get_global_fallback_manager()
        b = get_global_fallback_manager()
        assert a is b
        c = get_global_fallback_manager(chain=["x", "y"])
        assert c is a, "已创建的单例不应被新参数覆盖"

    def test_p0_routing_creates_correct_history(self):
        """P0 路由应在 routing_history 中正确记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / ".hermes" / "loop-hermes"
            base.mkdir(parents=True, exist_ok=True)
            sdir = str(base)
            state = self._make_p0_state()

            hist_before = len(state.get("routing_history", []))
            execute_routing(state)
            hist_after = len(state.get("routing_history", []))

            assert hist_after > hist_before, "P0 路由应在 history 中添加记录"
            latest = state["routing_history"][-1]
            assert latest["to"] == "part_1_1", (
                f"最新记录应指向 part_1_1，实际 {latest['to']}"
            )


# ============================================================================
# Golden-3: 并行子 agent + 收敛
# ============================================================================

class TestGoldenParallelAndConvergence:
    """Golden-3: 并行委派、结果合并与收敛判定。

    验证:
        1. ParallelDelegateManager 并行执行 3 个 TaskSpec
        2. merge_results 正确聚合 SubAgentResult
        3. 冲突检测在共享文件场景下正确
        4. is_converged() 在各种条件下的判定
    """

    def _success_executor(self):
        """创建默认返回成功的 executor 函数。

        Returns:
            executor: (TaskSpec) -> SubAgentResult
        """
        def run(spec):
            return SubAgentResult(
                agent_id=spec.agent_id,
                task_id=spec.task_id,
                status="success",
                output={"module": spec.module, "result": f"Completed {spec.task_id}"},
                modified_files=list(spec.assigned_files),
                guardrail_events=[],
                duration_ms=100,
            )
        return run

    def test_parallel_3_tasks_all_success(self):
        """3 个 TaskSpec 并行执行全部成功，验证合并统计。

        验证:
            1. 3 个 SubAgentResult 全部 success
            2. merge_results 统计正确 (succeeded=3, failed=0)
            3. modified_files 正确去重
            4. merged_output.agents 包含 3 个 agentid
        """
        specs = [
            TaskSpec(task_id="tk-auth", prompt="Implement auth module",
                     assigned_files=["src/auth/login.py", "src/shared/utils.py"],
                     module="auth"),
            TaskSpec(task_id="tk-db", prompt="Implement database layer",
                     assigned_files=["src/db/repo.py", "src/shared/utils.py"],
                     module="database"),
            TaskSpec(task_id="tk-api", prompt="Implement API routes",
                     assigned_files=["src/api/routes.py"], module="api"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ParallelDelegateManager(
                max_parallel=3, total_timeout=30, fail_fast=True, state_dir=tmp,
            )
            results = mgr.delegate(specs, self._success_executor())

            assert len(results) == 3
            for r in results:
                assert r.status == "success", (
                    f"agent[{r.agent_id}] 应为 success，实际 {r.status}"
                )
                assert r.task_id in ("tk-auth", "tk-db", "tk-api")
                assert r.duration_ms >= 0

            merged = merge_results(results)
            assert isinstance(merged, MergeResult)
            assert merged.total == 3
            assert merged.succeeded == 3
            assert merged.failed == 0
            assert merged.cancelled == 0

            # shared/utils.py 去重
            assert "src/shared/utils.py" in merged.all_modified_files
            assert merged.all_modified_files.count("src/shared/utils.py") == 1

            agents = merged.merged_output.get("agents", {})
            assert len(agents) == 3

    def test_parallel_with_fail_fast(self):
        """失败 + fail-fast: task-2 失败应触发其余取消。"""
        def mixed_run(spec):
            if spec.task_id == "tk-2":
                return SubAgentResult(
                    agent_id=spec.agent_id, task_id=spec.task_id,
                    status="failed", error="simulated failure",
                )
            return SubAgentResult(
                agent_id=spec.agent_id, task_id=spec.task_id,
                status="success", output={"response": "done"},
                modified_files=list(spec.assigned_files),
            )

        specs = [
            TaskSpec(task_id="tk-1", prompt="T1",
                     assigned_files=["src/1.py"]),
            TaskSpec(task_id="tk-2", prompt="T2",
                     assigned_files=["src/2.py"]),
            TaskSpec(task_id="tk-3", prompt="T3",
                     assigned_files=["src/3.py"]),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ParallelDelegateManager(
                max_parallel=3, total_timeout=30, fail_fast=True, state_dir=tmp,
            )
            results = mgr.delegate(specs, mixed_run)
            merged = merge_results(results)
            assert merged.failed >= 1, f"至少 1 个失败，实际 failed={merged.failed}"

    def test_merge_result_structure(self):
        """merge_results 产出 MergeResult 的字段完整性。

        验证 total, succeeded, failed, all_guardrail_events,
        merged_output.agents, merged_output.summary。
        """
        r1 = SubAgentResult(
            agent_id="ag-x", task_id="tx", status="success",
            output={"resp": "ok"}, modified_files=["src/x.py", "src/common.py"],
            guardrail_events=[{"type": "WARN", "message": "low confidence"}],
            duration_ms=1200,
        )
        r2 = SubAgentResult(
            agent_id="ag-y", task_id="ty", status="success",
            output={"resp": "ok2"}, modified_files=["src/y.py", "src/common.py"],
            guardrail_events=[], duration_ms=800,
        )
        merged = merge_results([r1, r2])

        assert merged.total == 2
        assert merged.succeeded == 2
        assert merged.failed == 0
        assert merged.total_duration_ms == 2000

        mo = merged.merged_output
        assert "agents" in mo and "summary" in mo
        assert "ag-x" in mo["agents"] and "ag-y" in mo["agents"]
        assert mo["summary"]["total"] == 2 and mo["summary"]["succeeded"] == 2

        assert len(merged.all_guardrail_events) == 1
        assert merged.all_guardrail_events[0]["type"] == "WARN"

        # common.py 去重后应只出现 1 次
        assert len(merged.all_modified_files) == 3
        assert "src/common.py" in merged.all_modified_files

    def test_detect_file_conflicts(self):
        """两个 agent 修改同一文件时检测冲突。"""
        results = [
            SubAgentResult(agent_id="a1", task_id="t1", status="success",
                           modified_files=["src/a.py", "src/conflict.py"]),
            SubAgentResult(agent_id="a2", task_id="t2", status="success",
                           modified_files=["src/b.py", "src/conflict.py"]),
            SubAgentResult(agent_id="a3", task_id="t3", status="success",
                           modified_files=["src/c.py"]),
        ]
        conflicts = detect_file_conflicts(results)
        assert "src/conflict.py" in conflicts, (
            f"应有冲突 src/conflict.py，实际 {conflicts}"
        )
        assert "src/a.py" not in conflicts
        assert len(conflicts) == 1

    def test_no_conflicts_separate_files(self):
        """不同 agent 修改不同文件时无冲突。"""
        results = [
            SubAgentResult(agent_id="a1", task_id="t1", status="success",
                           modified_files=["src/auth/login.py"]),
            SubAgentResult(agent_id="a2", task_id="t2", status="success",
                           modified_files=["src/db/repo.py"]),
            SubAgentResult(agent_id="a3", task_id="t3", status="success",
                           modified_files=["src/api/routes.py"]),
        ]
        conflicts = detect_file_conflicts(results)
        assert len(conflicts) == 0, f"独立文件不应有冲突，实际 {len(conflicts)}"

    def test_convergence_is_converged(self):
        """is_converged() 判定: counter >= rounds 且无活跃 issue。

        场景:
            counter=1, rounds=2, 无 issue -> False
            counter=2, rounds=2, 无 issue -> True
            counter=5, rounds=2, 有 P2 -> False
            清除 P2 -> True
            counter=0 -> False
        """
        s = deepcopy(DEFAULT_STATE_TEMPLATE)
        s["config"]["convergence_rounds"] = 2

        s["progress"]["convergence_counter"] = 1
        assert is_converged(s) is False

        s["progress"]["convergence_counter"] = 2
        assert is_converged(s) is True

        s["progress"]["convergence_counter"] = 5
        s["issues"]["active"]["p2"].append({
            "id": "p2-zz", "severity": "P2", "title": "bug", "status": "open",
        })
        assert is_converged(s) is False

        s["issues"]["active"]["p2"] = []
        assert is_converged(s) is True

        s["progress"]["convergence_counter"] = 0
        assert is_converged(s) is False

    def test_convergence_routing_returns_complete(self):
        """收敛时 execute_routing 应返回 target='complete' 并设置终止状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / ".hermes" / "loop-hermes"
            base_dir.mkdir(parents=True, exist_ok=True)
            sdir = str(base_dir)
            state = load_or_init_state(sdir)

            state["issues"]["active"] = {"p0": [], "p1": [], "p2": []}
            state["progress"]["convergence_counter"] = 3
            state["config"]["convergence_rounds"] = 2
            state["progress"]["phase"] = "routing"

            decision = execute_routing(state)
            assert decision["target"] == "complete"
            assert is_converged(state)
            assert is_terminated(state)
            assert state["termination"]["status"] == "complete"

    def test_partial_failure_blocks_convergence(self):
        """有活跃 P1 issue 时 convergence 被阻塞，路由到修复路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / ".hermes" / "loop-hermes"
            base_dir.mkdir(parents=True, exist_ok=True)
            sdir = str(base_dir)
            state = load_or_init_state(sdir)
            state["progress"]["phase"] = "routing"

            state["issues"]["active"]["p1"].append({
                "id": "p1-fail-001",
                "severity": "P1",
                "title": "Config conflict from parallel execution",
                "description": "Two agents modified shared config",
                "source": "test_failure",
                "status": "open",
                "affected_files": ["src/shared/config.py"],
                "linked_task_ids": [],
                "fix_strategy": "修复边界检查",
                "discovered_in_phase": "part_2_8",
            })
            state["progress"]["convergence_counter"] = 2

            assert not is_converged(state), "活跃 P1 时不应收敛"

            decision = execute_routing(state)
            assert decision["target"] in ("part_1_3", "part_2_2"), (
                f"P1 应路由到修复路径，实际 {decision['target']}"
            )

    def test_task_spec_defaults(self):
        """TaskSpec 创建与默认值验证。"""
        s = TaskSpec(task_id="my-task", prompt="Do something",
                     assigned_files=["src/mod.py"], module="core", priority=2)
        assert s.task_id == "my-task"
        assert s.prompt == "Do something"
        assert s.assigned_files == ["src/mod.py"]
        assert s.agent_id.startswith("agent-")
        assert s.timeout_seconds == 600
        assert s.priority == 2
        assert s.module == "core"

        s2 = TaskSpec(task_id="t2", prompt="another")
        assert s.agent_id != s2.agent_id, "自动生成的 agent_id 应唯一"

    def test_sub_agent_result_defaults(self):
        """SubAgentResult 默认值验证。"""
        r = SubAgentResult(agent_id="a-1", task_id="t-1")
        assert r.status == "pending"
        assert r.output == {}
        assert r.modified_files == []
        assert r.guardrail_events == []
        assert r.error is None
        assert r.duration_ms == 0
