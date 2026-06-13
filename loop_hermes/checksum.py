# -*- coding: utf-8 -*-
r"""Checksum 协议模块 —— SHA-256 三层校验。

三层校验体系:
    第 1 层：文件内容 → SHA-256 哈希 → state.json artifacts.<key>.checksum
    第 2 层：version 单调递增计数器（每次 artifact 更新时 +1）
    第 3 层：Sanity Check #15 启动时全量校验（state.json 记录 vs 文件实际哈希）

变更检测:
    每次 phase 产出 artifact 后调用 update_artifact_meta() 更新三层校验数据。
    Sanity Check 启动时调用 verify_artifact_integrity() 检测篡改。

用途:
    - 跨 phase 数据完整性保证
    - 篡改检测（恶意修改 artifact 文件）
    - 变更追踪（通过 version 计数器）
"""

import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

logger = logging.getLogger("loop_hermes.checksum")

# ============================================================================
# 第 1 层：SHA-256 哈希计算
# ============================================================================


def compute_checksum(file_path: str) -> str:
    """计算文件的 SHA-256 哈希值。

    第 1 层校验：文件内容 → 十六进制哈希字符串。

    Args:
        file_path: 文件路径

    Returns:
        64 位十六进制 SHA-256 哈希字符串

    Raises:
        FileNotFoundError: 文件不存在
        OSError: 文件读取失败
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_checksum_from_content(content: str) -> str:
    """从字符串内容计算 SHA-256 哈希值。

    用于尚未写入文件的 artifact 内容预计算。

    Args:
        content: 文本内容

    Returns:
        64 位十六进制 SHA-256 哈希字符串
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================================
# 第 2 层：version 计数器 + 元数据更新
# ============================================================================


def update_checksum_in_state(
    state: dict,
    art_key: str,
    file_path: Optional[str] = None,
) -> None:
    """更新 state.json 中 artifact 的三层校验数据。

    第 1 层：计算文件 SHA-256 → 写入 checksum 字段
    第 2 层：递增 version 计数器
    第 3 层：更新 status / generated_at / generated_in_phase（供 Sanity Check 使用）

    Args:
        state: state 字典（原地修改 artifacts 字段）
        art_key: artifact 键名（如 "requirements", "task_list" 等）
        file_path: artifact 文件路径。为 None 时使用 state 中已有路径

    Raises:
        KeyError: art_key 不在 state["artifacts"] 中
    """
    info = state.get("artifacts", {}).get(art_key)
    if info is None:
        raise KeyError(f"未知的 artifact 键: {art_key}")

    target_path = file_path or info.get("path", "")
    path = Path(target_path) if target_path else None

    if path and path.exists():
        # 第 1 层：checksum
        info["checksum"] = compute_checksum(str(path))
        # 第 2 层：version 递增
        info["version"] = info.get("version", 0) + 1
        # 第 3 层：status + 时间戳
        prev_status = info.get("status", "not_generated")
        info["status"] = "updated" if prev_status in ("generated", "updated") else "generated"
        info["generated_at"] = datetime.now(timezone.utc).isoformat()
        info["generated_in_phase"] = state["progress"]["phase"]
        logger.debug(
            "artifact [%s] checksum 已更新: version=%d, status=%s",
            art_key, info["version"], info["status"],
        )
    else:
        logger.warning("artifact 文件不存在或路径为空，跳过 checksum: key=%s, path=%s", art_key, target_path)


def update_all_artifacts_in_state(state: dict) -> List[str]:
    """批量更新 state 中所有已存在 artifact 文件的 checksum。

    用于 Sanity Check 后一次性同步所有 artifact 的校验数据。

    Args:
        state: state 字典（原地修改）

    Returns:
        成功更新的 artifact 键名列表
    """
    updated = []
    for art_key in state.get("artifacts", {}):
        info = state["artifacts"][art_key]
        file_path = info.get("path", "")
        if file_path and Path(file_path).exists():
            try:
                update_checksum_in_state(state, art_key, file_path)
                updated.append(art_key)
            except (OSError, KeyError) as e:
                logger.warning("批量更新 [%s] 失败: %s", art_key, e)
    return updated


# ============================================================================
# 第 3 层：完整性校验（Sanity Check 集成）
# ============================================================================


def verify_artifact_integrity(state: dict) -> List[Dict[str, Any]]:
    """校验所有已生成 artifact 的 checksum 完整性。

    第 3 层校验：对比 state.json 中记录的 checksum 与文件实际 SHA-256。
    用于 Sanity Check #15 和启动时全量验证。

    校验规则:
        - 仅校验 status 为 generated/updated 的 artifact
        - 仅校验有 checksum 记录的 artifact
        - 文件不存在 → 跳过（由其他检查项处理）

    Args:
        state: state 字典

    Returns:
        checksum 不匹配的详情列表。空列表表示全部通过。
        每条记录: {artifact, file, recorded, actual}
    """
    mismatches = []
    for art_key, info in state.get("artifacts", {}).items():
        file_path = info.get("path", "")
        recorded_checksum = info.get("checksum")
        status = info.get("status", "not_generated")

        # 仅校验已生成的 artifact
        if status not in ("generated", "updated"):
            continue
        if not file_path or not recorded_checksum:
            continue

        path = Path(file_path)
        if not path.exists():
            # 文件丢失 —— 不在此处报错（由 Sanity Check #7 处理）
            continue

        try:
            actual = compute_checksum(str(path))
        except OSError as e:
            mismatches.append({
                "artifact": art_key,
                "file": file_path,
                "recorded": recorded_checksum,
                "actual": "READ_ERROR",
                "error": str(e),
            })
            continue

        if actual != recorded_checksum:
            mismatches.append({
                "artifact": art_key,
                "file": file_path,
                "recorded": recorded_checksum[:16] + "...",
                "actual": actual[:16] + "...",
                "full_recorded": recorded_checksum,
                "full_actual": actual,
            })
            logger.warning(
                "checksum 不匹配 [%s]: recorded=%s..., actual=%s...",
                art_key, recorded_checksum[:16], actual[:16],
            )

    if not mismatches:
        logger.debug("所有 artifact checksum 校验通过")
    return mismatches


# ============================================================================
# 便捷 API
# ============================================================================


def is_artifact_intact(state: dict, art_key: str) -> bool:
    """快速检查单个 artifact 是否完整（checksum 匹配）。

    Args:
        state: state 字典
        art_key: artifact 键名

    Returns:
        True 如果 artifact 校验通过或未生成
    """
    info = state.get("artifacts", {}).get(art_key)
    if not info:
        return True
    file_path = info.get("path", "")
    recorded = info.get("checksum")
    if not file_path or not recorded:
        return True  # 未生成，视为通过
    path = Path(file_path)
    if not path.exists():
        return False
    try:
        return compute_checksum(str(path)) == recorded
    except OSError:
        return False
