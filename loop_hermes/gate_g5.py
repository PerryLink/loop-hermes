# -*- coding: utf-8 -*-
"""G5 文件变更门。

监控 Hermes Agent 在本 cycle 内创建、修改、删除的文件数量。
按运行模式设定不同阈值，超阈值时触发拦截/警告。

闸门等级: L2（safe/auto 模式超阈值暂停，unsafe 仅记录）
触发时机: 每个 cycle 结束时（Part 2.8 hard_gate 之前）
处置动作: 超阈值注入 P1 issue，要求用户审查变更。

设计意图:
    防止 Hermes Agent 在自动驾驶过程中进行大规模不可控的文件变更。
    小步快跑、逐个 area 迭代是安全目标。单 cycle 内变更过多
    意味着 Agent 可能失去控制或产生了意外副作用。

阈值分级:
    - safe 模式: 最多 3 个文件/cycle
    - auto 模式: 最多 10 个文件/cycle
    - collaborative 模式: 最多 10 个文件/cycle
    - unsafe 模式: 仅记录，不拦截（999 上限为软限制）
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set, Tuple

logger = logging.getLogger("loop_hermes.gate_g5")

# ============================================================================
# G5 闸门常量
# ============================================================================

GATE_ID = "G5"

# 各模式文件变更阈值
FILE_COUNT_THRESHOLDS = {
    "safe": 3,
    "auto": 10,
    "collaborative": 10,
    "unsafe": 999,
}

# 不计入变更的文件扩展名（文档、配置、锁文件等）
IGNORED_EXTENSIONS: Set[str] = {
    ".md", ".txt", ".log", ".json", ".lock",
    ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".gitignore", ".dockerignore", ".editorconfig",
}

# 不计入变更的路径模式
IGNORED_PATH_PATTERNS: List[str] = [
    ".hermes/",           # loop-hermes 自身 state 目录
    ".git/",              # Git 内部文件
    "__pycache__/",       # Python 缓存
    "node_modules/",       # Node 依赖
    ".venv/",             # Python 虚拟环境
    "venv/",
    ".env",               # 环境变量文件
    "*.pyc",              # Python 字节码
    ".pytest_cache/",     # pytest 缓存
    ".benchmarks/",       # 性能基准测试
    ".coverage",          # 覆盖率文件
    "dist/",              # 构建产物
    "build/",             # 构建产物
    "*.egg-info/",        # Python 包信息
]

# 受保护的关键文件/目录（deleted 时触发 HIGH 告警）
PROTECTED_PATHS: List[str] = [
    "setup.py", "setup.cfg", "pyproject.toml",
    "Makefile", "Dockerfile", "docker-compose.yml",
    "requirements.txt", "requirements*.txt",
    "package.json", "package-lock.json",
    "src/", "tests/", "loop_hermes/",
]


# ============================================================================
# 文件 Snapshot
# ============================================================================


class FileSnapshot:
    """文件系统快照。

    记录某个时间点的文件状态（路径 + mtime + size），
    用于跨 cycle 对比变更。

    Attributes:
        files: {relative_path: (mtime, size)} 映射
        snapshot_at: 快照时间戳
    """

    def __init__(self, root_dir: str):
        """初始化快照。

        Args:
            root_dir: 项目根目录
        """
        self.root_dir = Path(root_dir).resolve()
        self.files: Dict[str, Tuple[float, int]] = {}
        self.snapshot_at: str = ""

    def capture(self, extensions: Optional[Set[str]] = None) -> int:
        """捕获当前文件系统状态。

        遍历 root_dir 下的所有文件（排除 IGNORED_PATH_PATTERNS），
        记录每个文件的 mtime 和 size。

        Args:
            extensions: 关注的文件扩展名集合。
                        为 None 时记录所有不忽略的文件。

        Returns:
            捕获的文件数量
        """
        self.files.clear()
        self.snapshot_at = datetime.now(timezone.utc).isoformat()

        for root, dirs, files in os.walk(str(self.root_dir)):
            # 过滤不需要遍历的目录
            dirs_to_skip = set()
            for d in dirs:
                d_full = os.path.join(root, d)
                if _is_ignored_path(d_full, self.root_dir):
                    dirs_to_skip.add(d)
            # 原地修改 dirs 列表以阻止 os.walk 深入
            dirs[:] = [d for d in dirs if d not in dirs_to_skip]

            for f in files:
                fpath = os.path.join(root, f)
                if _is_ignored_path(fpath, self.root_dir):
                    continue
                if extensions:
                    _, ext = os.path.splitext(f)
                    if ext.lower() not in extensions:
                        continue
                try:
                    stat = os.stat(fpath)
                    rel = os.path.relpath(fpath, str(self.root_dir))
                    self.files[rel] = (stat.st_mtime, stat.st_size)
                except OSError:
                    pass

        logger.debug("快照捕获 %d 个文件 @ %s", len(self.files), self.snapshot_at)
        return len(self.files)

    def diff(self, other: "FileSnapshot") -> Dict[str, Any]:
        """计算与另一个快照之间的变更。

        对比两个快照的文件列表，分类为:
            - added: 新增文件
            - deleted: 删除文件
            - modified: 修改文件（mtime 或 size 变化）

        Args:
            other: 另一个 FileSnapshot 实例

        Returns:
            {
                "added": [str],
                "deleted": [str],
                "modified": [str],
                "total": int,
            }
        """
        self_paths = set(self.files.keys())
        other_paths = set(other.files.keys())

        added = sorted(other_paths - self_paths)
        deleted = sorted(self_paths - other_paths)
        modified: List[str] = []

        for p in self_paths & other_paths:
            if self.files[p] != other.files[p]:
                modified.append(p)
        modified.sort()

        return {
            "added": added,
            "deleted": deleted,
            "modified": modified,
            "total": len(added) + len(deleted) + len(modified),
        }


def _is_ignored_path(filepath: str, root_dir: Path) -> bool:
    """判断文件路径是否应被忽略（不计入变更统计）。

    Args:
        filepath: 绝对文件路径
        root_dir: 项目根目录

    Returns:
        True 如果应被忽略
    """
    try:
        rel = os.path.relpath(filepath, str(root_dir))
    except ValueError:
        return False

    # 统一使用正斜杠
    rel_norm = rel.replace("\\", "/")

    # 检查忽略路径模式
    for pattern in IGNORED_PATH_PATTERNS:
        if "*" in pattern:
            # 通配符匹配（文件名级别）
            pat = pattern.replace(".", r"\.").replace("*", ".*")
            import re
            if re.search(pat, rel_norm):
                return True
        elif pattern.endswith("/"):
            if rel_norm.startswith(pattern) or rel_norm == pattern.rstrip("/"):
                return True
        elif rel_norm == pattern or rel_norm.startswith(pattern):
            return True

    # 检查忽略扩展名
    _, ext = os.path.splitext(rel_norm)
    if ext.lower() in IGNORED_EXTENSIONS:
        return True

    return False


# ============================================================================
# 受保护文件检测
# ============================================================================


def check_protected_files(change_list: Dict[str, Any]) -> List[Dict[str, str]]:
    """检测变更中是否涉及受保护的关键文件/目录。

    Args:
        change_list: FileSnapshot.diff() 返回的变更字典

    Returns:
        受保护文件变更告警列表
    """
    alerts: List[Dict[str, str]] = []

    for deleted in change_list.get("deleted", []):
        for protected in PROTECTED_PATHS:
            if deleted.startswith(protected):
                alerts.append({
                    "file": deleted,
                    "change_type": "deleted",
                    "protected_pattern": protected,
                    "severity": "HIGH",
                })

    for modified in change_list.get("modified", []):
        for protected in PROTECTED_PATHS:
            if modified.startswith(protected):
                alerts.append({
                    "file": modified,
                    "change_type": "modified",
                    "protected_pattern": protected,
                    "severity": "MEDIUM",
                })

    return alerts


# ============================================================================
# G5 核心逻辑
# ============================================================================


def audit_file_changes(
    snapshot_before: Optional[FileSnapshot],
    snapshot_after: FileSnapshot,
    mode: str = "auto",
) -> Dict[str, Any]:
    """审计文件变更是否超过模式阈值。

    Args:
        snapshot_before: 本 cycle 开始前的快照。为 None 时仅记录当前状态
        snapshot_after: 本 cycle 结束后的快照
        mode: 运行模式

    Returns:
        {
            "gate_id": "G5",
            "passed": bool,
            "blocked": bool,
            "threshold": int,
            "changes": {
                "added": [str],
                "deleted": [str],
                "modified": [str],
                "total": int,
            },
            "protected_alerts": [dict],
            "timestamp": str,
        }
    """
    threshold = FILE_COUNT_THRESHOLDS.get(mode, 10)

    if snapshot_before is None:
        # 无对比基准 → 默认通过
        logger.debug("G5 无快照对比基准，默认通过")
        return {
            "gate_id": GATE_ID,
            "passed": True,
            "blocked": False,
            "threshold": threshold,
            "changes": {"added": [], "deleted": [], "modified": [], "total": 0},
            "protected_alerts": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    changes = snapshot_before.diff(snapshot_after)
    protected_alerts = check_protected_files(changes)

    # 判断是否超过阈值
    exceeded = changes["total"] > threshold
    has_protected_alerts = len(protected_alerts) > 0

    # safe/auto/collaborative 模式超阈值则拦截
    blocked = exceeded and mode != "unsafe"
    passed = not blocked

    if not passed:
        logger.warning(
            "G5 文件变更超阈值: %d > %d（mode=%s）",
            changes["total"], threshold, mode,
        )
        for alert in protected_alerts:
            logger.warning(
                "  G5 受保护文件告警: [%s] %s (%s)",
                alert["severity"], alert["file"], alert["change_type"],
            )

    return {
        "gate_id": GATE_ID,
        "passed": passed,
        "blocked": blocked,
        "threshold": threshold,
        "changes": changes,
        "protected_alerts": protected_alerts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Issue 注入
# ============================================================================


def inject_g5_issues_into_state(
    state: dict,
    audit_result: Dict[str, Any],
) -> int:
    """将 G5 审计发现注入 state 的 issue 列表。

    Args:
        state: state 字典（原地修改）
        audit_result: audit_file_changes() 返回结果

    Returns:
        注入的 issue 数量
    """
    import uuid

    changes = audit_result.get("changes", {})
    protected_alerts = audit_result.get("protected_alerts", [])
    count = 0
    phase = state["progress"].get("phase", "unknown")

    # 超阈值 issue
    if audit_result.get("blocked"):
        total = changes.get("total", 0)
        threshold = audit_result.get("threshold", 0)
        issue = {
            "id": f"g5-{uuid.uuid4().hex[:8]}",
            "severity": "P1",
            "title": f"G5 文件变更超阈值: {total} > {threshold}",
            "description": (
                f"本 cycle 修改 {total} 个文件，超过"
                f" {state['config'].get('mode', 'auto')} 模式"
                f"阈值 {threshold}。\n"
                f"新增: {len(changes.get('added', []))}\n"
                f"删除: {len(changes.get('deleted', []))}\n"
                f"修改: {len(changes.get('modified', []))}\n"
                f"需要人工审查变更范围后重新执行。"
            )[:500],
            "source": "hermes_guardrail",
            "source_ref": "gate_g5@threshold_exceeded",
            "discovered_in_phase": phase,
            "status": "open",
            "affected_files": list(
                set(changes.get("added", [])[:20] +
                    changes.get("deleted", [])[:20] +
                    changes.get("modified", [])[:20])
            ),
            "linked_task_ids": [],
            "fix_strategy": "拆分变更为多个小批量 cycle，每批控制在阈值内。",
        }
        state["issues"]["active"]["p1"].append(issue)
        state["issues"]["all_time"]["p1_total"] += 1
        count += 1

    # 受保护文件告警 issue
    for alert in protected_alerts:
        sev = "P0" if alert["severity"] == "HIGH" else "P1"
        issue = {
            "id": f"g5-{uuid.uuid4().hex[:8]}",
            "severity": sev,
            "title": f"G5 受保护文件变更: {alert['file']} ({alert['change_type']})",
            "description": (
                f"受保护文件被{alert['change_type']}。\n"
                f"文件: {alert['file']}\n"
                f"保护规则: {alert['protected_pattern']}\n"
                f"需要人工审查此变更是否合理。"
            ),
            "source": "hermes_guardrail",
            "source_ref": f"gate_g5@protected_file@{alert['file']}",
            "discovered_in_phase": phase,
            "status": "open",
            "affected_files": [alert["file"]],
            "linked_task_ids": [],
            "fix_strategy": "人工审查受保护文件变更的合法性。",
        }
        sev_key = sev.lower()
        state["issues"]["active"][sev_key].append(issue)
        state["issues"]["all_time"][f"{sev_key}_total"] += 1
        count += 1

    if count > 0:
        state["progress"]["new_issues_this_round"] = True

    logger.info("G5 注入 %d 个 issue 到 state", count)
    return count


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g5(
    state: dict,
    root_dir: str,
    snapshot_before: Optional[FileSnapshot] = None,
) -> Dict[str, Any]:
    """运行 G5 文件变更门完整流程。

    1. 创建当前文件系统快照
    2. 与 cycle 开始前快照对比
    3. 检查是否超过模式阈值
    4. 超阈值时注入 issue

    Args:
        state: state 字典（原地修改）
        root_dir: 项目根目录
        snapshot_before: cycle 开始前的快照（可选）

    Returns:
        完整审计结果字典
    """
    mode = state.get("config", {}).get("mode", "auto")
    snapshot_after = FileSnapshot(root_dir)
    snapshot_after.capture()

    audit_result = audit_file_changes(snapshot_before, snapshot_after, mode)

    if audit_result["blocked"]:
        inject_g5_issues_into_state(state, audit_result)

    # 更新 gate_state 中的文件修改变更计数
    gate = state.setdefault("gate_state", {})
    gate["file_modifications_this_cycle"] = audit_result["changes"]["total"]

    return audit_result


def create_snapshot(root_dir: str) -> FileSnapshot:
    """创建项目文件系统快照（用于 cycle 开始前）。

    Args:
        root_dir: 项目根目录

    Returns:
        FileSnapshot 实例（已捕获当前状态）
    """
    snap = FileSnapshot(root_dir)
    snap.capture()
    return snap
