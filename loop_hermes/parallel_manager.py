# -*- coding: utf-8 -*-
"""并行委派管理器。

管理 Hermes sub-agent 的并行派发、监控、超时和结果合并。

核心功能:
    - delegate(task_specs): 并行派发多个 sub-agent 执行任务
    - merge(results): 合并多个 sub-agent 的产出结果
    - 子 agent 生命周期: spawn → monitor → timeout → cleanup
    - 最大并发数限制 + 排队机制
    - fail-fast 模式: 任一 agent 失败立即终止其余

设计意图:
    在 Part 2.2（实现阶段）和 Part 2.6（测试阶段）中，
    可将独立任务并行分派给多个 Hermes sub-agent 同时执行，
    提升整体吞吐量。本模块管理并发、超时和结果聚合。

Sub-agent 产出格式:
    每个 sub-agent 返回:
    {
        "agent_id": str,
        "task_id": str,
        "status": "success" | "failed" | "timeout" | "cancelled",
        "output": dict,  # artifact 变更记录
        "modified_files": [str],
        "guardrail_events": [dict],
        "duration_ms": int,
        "error": str | None,
    }
"""

import os
import json
import time
import uuid
import queue
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable

logger = logging.getLogger("loop_hermes.parallel_manager")

# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class TaskSpec:
    """单个 sub-agent 任务规格。

    Attributes:
        task_id: 任务唯一 ID
        agent_id: sub-agent 唯一 ID（自动生成）
        prompt: 发送给 sub-agent 的 prompt
        assigned_files: 分配给此 agent 的文件列表
        module: 所属模块名
        timeout_seconds: 超时时间（秒），默认 600
        priority: 优先级（越小越优先），默认 1
        metadata: 附加元数据
    """
    task_id: str
    prompt: str
    agent_id: str = ""
    assigned_files: List[str] = field(default_factory=list)
    module: str = ""
    timeout_seconds: int = 600
    priority: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.agent_id:
            self.agent_id = f"agent-{uuid.uuid4().hex[:8]}"


@dataclass
class SubAgentResult:
    """Sub-agent 执行结果。

    Attributes:
        agent_id: sub-agent ID
        task_id: 关联任务 ID
        status: 执行状态
        output: 产出内容
        modified_files: 修改的文件列表
        guardrail_events: guardrail 事件列表
        duration_ms: 执行耗时（毫秒）
        error: 错误描述
        started_at: 启动时间戳
        finished_at: 完成时间戳
    """
    agent_id: str
    task_id: str
    status: str = "pending"
    output: Dict[str, Any] = field(default_factory=dict)
    modified_files: List[str] = field(default_factory=list)
    guardrail_events: List[dict] = field(default_factory=list)
    duration_ms: int = 0
    error: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class MergeResult:
    """合并后的结果摘要。

    Attributes:
        total: 总任务数
        succeeded: 成功数
        failed: 失败数
        timeout: 超时数
        cancelled: 被取消数
        merged_output: 合并后的产出
        all_modified_files: 所有修改的文件合集
        all_guardrail_events: 所有 guardrail 事件合集
        total_duration_ms: 总耗时（毫秒）
    """
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    timeout: int = 0
    cancelled: int = 0
    merged_output: Dict[str, Any] = field(default_factory=dict)
    all_modified_files: List[str] = field(default_factory=list)
    all_guardrail_events: List[dict] = field(default_factory=list)
    total_duration_ms: int = 0


# ============================================================================
# 默认配置
# ============================================================================

DEFAULT_MAX_PARALLEL = 4
DEFAULT_TOTAL_TIMEOUT = 3600  # 1 小时
DEFAULT_PER_AGENT_TIMEOUT = 600  # 10 分钟


# ============================================================================
# ParallelDelegateManager
# ============================================================================


class ParallelDelegateManager:
    """并行委派管理器。

    管理多个 Hermes sub-agent 的并行执行，支持:
        - 最大并发数限制（semaphore）
        - 单 agent 超时
        - 总体超时
        - fail-fast 模式（任一 agent 失败立即取消其余）
        - 结果合并

    使用示例:
        >>> mgr = ParallelDelegateManager(max_parallel=4)
        >>> specs = [TaskSpec(task_id="t1", prompt="..."), ...]
        >>> results = mgr.delegate(specs, executor_fn=my_executor)
        >>> merged = merge_results(results)
    """

    def __init__(
        self,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        total_timeout: int = DEFAULT_TOTAL_TIMEOUT,
        fail_fast: bool = True,
        state_dir: Optional[str] = None,
    ):
        """初始化并行委派管理器。

        Args:
            max_parallel: 最大并发 sub-agent 数
            total_timeout: 总体超时时间（秒）
            fail_fast: 是否启用 fail-fast 模式
            state_dir: state 目录（用于 sub-agent 工作目录）
        """
        self.max_parallel = max_parallel
        self.total_timeout = total_timeout
        self.fail_fast = fail_fast
        self.state_dir = state_dir

        # 并发控制
        self._semaphore = threading.BoundedSemaphore(max_parallel)

        # 运行时追踪
        self._results: Dict[str, SubAgentResult] = {}
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._start_time: float = 0.0

        logger.info(
            "ParallelDelegateManager 初始化: max_parallel=%d, "
            "total_timeout=%ds, fail_fast=%s",
            max_parallel, total_timeout, fail_fast,
        )

    # ------------------------------------------------------------------
    # 主入口：并行派发
    # ------------------------------------------------------------------

    def delegate(
        self,
        task_specs: List[TaskSpec],
        executor_fn: Callable[[TaskSpec], SubAgentResult],
    ) -> List[SubAgentResult]:
        """并行派发 sub-agent 执行任务。

        每个 TaskSpec 在一个独立线程中执行，受 max_parallel 限制。
        使用 ThreadPool 模式（通过 threading + semaphore 实现）。

        Args:
            task_specs: TaskSpec 列表，每个描述一个 sub-agent 任务
            executor_fn: 执行函数，签名为 (TaskSpec) -> SubAgentResult

        Returns:
            SubAgentResult 列表，与输入 task_specs 顺序一致
        """
        if not task_specs:
            logger.info("无任务需要派发")
            return []

        self._start_time = time.time()
        self._cancel_event.clear()

        # 结果收集
        results: List[Optional[SubAgentResult]] = [None] * len(task_specs)
        threads: List[threading.Thread] = []

        def _worker(idx: int, spec: TaskSpec):
            """线程 worker：获取信号量 → 执行 → 释放信号量。"""
            acquired = self._semaphore.acquire(timeout=self.total_timeout)
            if not acquired:
                logger.error("获取信号量超时: %s", spec.task_id)
                result = SubAgentResult(
                    agent_id=spec.agent_id,
                    task_id=spec.task_id,
                    status="timeout",
                    error="获取并发槽位超时",
                )
                with self._lock:
                    results[idx] = result
                    self._results[spec.agent_id] = result
                return

            try:
                # 检查是否已被取消
                if self._cancel_event.is_set():
                    result = SubAgentResult(
                        agent_id=spec.agent_id,
                        task_id=spec.task_id,
                        status="cancelled",
                        error="Fail-fast: 其他 agent 已失败",
                    )
                    with self._lock:
                        results[idx] = result
                        self._results[spec.agent_id] = result
                    return

                # 记录开始
                now_iso = datetime.now(timezone.utc).isoformat()
                logger.info("启动 sub-agent [%s] for task [%s]", spec.agent_id, spec.task_id)

                # 执行
                t_start = time.time()
                try:
                    result = executor_fn(spec)
                except Exception as e:
                    logger.error("Sub-agent [%s] 执行异常: %s", spec.agent_id, e)
                    result = SubAgentResult(
                        agent_id=spec.agent_id,
                        task_id=spec.task_id,
                        status="failed",
                        error=str(e),
                    )

                t_end = time.time()
                result.duration_ms = int((t_end - t_start) * 1000)
                result.started_at = now_iso
                result.finished_at = datetime.now(timezone.utc).isoformat()
                if not result.agent_id:
                    result.agent_id = spec.agent_id
                if not result.task_id:
                    result.task_id = spec.task_id

                with self._lock:
                    results[idx] = result
                    self._results[spec.agent_id] = result

                # fail-fast 检查
                if result.status in ("failed", "timeout") and self.fail_fast:
                    logger.warning(
                        "Fail-fast: agent [%s] 状态=%s，取消其余 agent",
                        spec.agent_id, result.status,
                    )
                    self._cancel_event.set()

                logger.info(
                    "Sub-agent [%s] 完成: status=%s, duration=%dms",
                    spec.agent_id, result.status, result.duration_ms,
                )

            finally:
                self._semaphore.release()

        # 启动所有 worker 线程
        for i, spec in enumerate(task_specs):
            t = threading.Thread(
                target=_worker,
                args=(i, spec),
                name=f"hermes-sub-{spec.agent_id}",
                daemon=True,
            )
            threads.append(t)
            t.start()

        # 等待所有线程完成（或总体超时）
        deadline = self._start_time + self.total_timeout
        for t in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                logger.error("总体超时 (%ds)，取消剩余 agent", self.total_timeout)
                self._cancel_event.set()
                break
            t.join(timeout=remaining)

        # 标记未完成的为 timeout
        with self._lock:
            for i, r in enumerate(results):
                if r is None:
                    spec = task_specs[i]
                    results[i] = SubAgentResult(
                        agent_id=spec.agent_id,
                        task_id=spec.task_id,
                        status="timeout",
                        error="总体超时或线程未完成",
                        started_at=datetime.now(timezone.utc).isoformat(),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )

        elapsed = time.time() - self._start_time
        logger.info(
            "Parallel delegate 完成: %d tasks, %.1fs elapsed",
            len(task_specs), elapsed,
        )
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_result(self, agent_id: str) -> Optional[SubAgentResult]:
        """获取指定 sub-agent 的执行结果。

        Args:
            agent_id: sub-agent ID

        Returns:
            SubAgentResult 或 None
        """
        with self._lock:
            return self._results.get(agent_id)

    def get_all_results(self) -> Dict[str, SubAgentResult]:
        """获取所有 sub-agent 的执行结果。

        Returns:
            agent_id → SubAgentResult 映射
        """
        with self._lock:
            return dict(self._results)

    def is_cancelled(self) -> bool:
        """检查是否已触发取消信号（fail-fast）。

        Returns:
            True 如果已取消
        """
        return self._cancel_event.is_set()

    def cancel_all(self, reason: str = "") -> None:
        """手动取消所有正在执行的 sub-agent。

        Args:
            reason: 取消原因
        """
        logger.warning("手动取消所有 sub-agent: %s", reason)
        self._cancel_event.set()


# ============================================================================
# 结果合并
# ============================================================================


def merge_results(results: List[SubAgentResult]) -> MergeResult:
    """合并多个 sub-agent 的执行结果。

    合并策略:
        - modified_files: 去重合并
        - guardrail_events: 全部保留
        - output: 按 agent_id 为 key 合并为字典
        - 按 status 统计成功/失败/超时/取消数量

    Args:
        results: SubAgentResult 列表

    Returns:
        MergeResult 合并摘要
    """
    merged = MergeResult()
    merged.total = len(results)

    files_set = set()
    guardrail_list: List[dict] = []
    output_map: Dict[str, Any] = {}

    for r in results:
        if r.status == "success":
            merged.succeeded += 1
        elif r.status == "failed":
            merged.failed += 1
        elif r.status == "timeout":
            merged.timeout += 1
        elif r.status == "cancelled":
            merged.cancelled += 1

        # 合并文件
        for f in r.modified_files:
            files_set.add(f)

        # 合并 guardrail 事件
        guardrail_list.extend(r.guardrail_events)

        # 合并 output（以 agent_id 为 key）
        if r.output:
            output_map[r.agent_id] = r.output

        merged.total_duration_ms += r.duration_ms

    merged.all_modified_files = sorted(files_set)
    merged.all_guardrail_events = guardrail_list
    merged.merged_output = {
        "agents": output_map,
        "summary": {
            "total": merged.total,
            "succeeded": merged.succeeded,
            "failed": merged.failed,
            "timeout": merged.timeout,
            "cancelled": merged.cancelled,
        },
    }

    logger.info(
        "结果合并完成: total=%d, success=%d, failed=%d, timeout=%d, cancelled=%d, "
        "modified_files=%d, guardrail_events=%d",
        merged.total, merged.succeeded, merged.failed,
        merged.timeout, merged.cancelled,
        len(merged.all_modified_files), len(merged.all_guardrail_events),
    )
    return merged


# ============================================================================
# Sub-agent 工作目录管理
# ============================================================================


def setup_agent_workspace(
    state_dir: str,
    agent_id: str,
) -> Path:
    """为 sub-agent 创建隔离的工作目录。

    目录结构:
        {state_dir}/parallel/agents/{agent_id}/
            ├── artifacts/    # 产出目录
            └── state.json    # agent 自身状态

    Args:
        state_dir: 主 state 目录
        agent_id: sub-agent ID

    Returns:
        agent 工作目录 Path
    """
    ws = Path(state_dir) / "parallel" / "agents" / agent_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "artifacts").mkdir(exist_ok=True)

    # 写入基本 agent 状态
    agent_state = {
        "agent_id": agent_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "initializing",
        "artifacts": {},
    }
    (ws / "state.json").write_text(
        json.dumps(agent_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug("Agent workspace 创建: %s", ws)
    return ws


def cleanup_agent_workspace(state_dir: str, agent_id: str) -> bool:
    """清理 sub-agent 的工作目录。

    仅在 agent 正常结束后调用；失败时保留现场用于调试。

    Args:
        state_dir: 主 state 目录
        agent_id: sub-agent ID

    Returns:
        True 如果成功清理
    """
    ws = Path(state_dir) / "parallel" / "agents" / agent_id
    if not ws.exists():
        return True

    # 读取状态判断是否成功
    state_file = ws / "state.json"
    should_clean = True
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("status") == "failed":
                should_clean = False
                logger.info("Agent [%s] 失败，保留 workspace: %s", agent_id, ws)
        except (json.JSONDecodeError, OSError):
            pass

    if should_clean:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)
        logger.debug("Agent workspace 已清理: %s", ws)
    return should_clean


# ============================================================================
# 冲突检测
# ============================================================================


def detect_file_conflicts(results: List[SubAgentResult]) -> List[str]:
    """检测多个 sub-agent 是否修改了相同文件（潜在冲突）。

    Args:
        results: SubAgentResult 列表

    Returns:
        被多个 agent 修改的文件路径列表
    """
    from collections import Counter
    file_counts = Counter()
    for r in results:
        for f in r.modified_files:
            file_counts[f] += 1

    conflicts = [f for f, cnt in file_counts.items() if cnt > 1]
    if conflicts:
        logger.warning("检测到 %d 个文件被多个 agent 修改: %s",
                       len(conflicts), conflicts)
    return sorted(conflicts)


