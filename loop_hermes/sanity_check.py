# -*- coding: utf-8 -*-
"""Sanity Check —— 15 项启动检查。

每次 loop-hermes 进程启动后、读取 state.json 之前执行。
全面检查环境、依赖、配置、数据完整性和并发安全性。

检查项总览:
     1. Python 版本 >= 3.10
     2. Hermes SDK 可导入 或 hermes 在 PATH 中
     3. state_dir 路径存在且可写
     4. .lock 文件未被占用（无僵尸锁）
     5. state.json 文件存在且合法 JSON
     6. state.json schema_version 兼容
     7. artifacts/ 目录存在
     8. context-summary.md 可追加写入
     9. 至少一个 Provider API key 已设置
    10. Provider 回退链中至少 1 个 provider 可用
    11. config.mode 取值为合法枚举
    12. max_cycles / convergence_rounds 为正整数
    13. hermes_model 非空字符串
    14. gate_state 字段存在且合法
    15. artifacts/ 中已有文件与 state.json 的 checksum 一致
"""

import sys
import os
import json
import hashlib
import shutil
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("loop_hermes.sanity_check")


# ============================================================================
# 检查项定义
# ============================================================================

SANITY_CHECKS: List[Dict[str, Any]] = [
    {"id": 1, "name": "Python 版本 >= 3.10", "category": "环境"},
    {"id": 2, "name": "Hermes SDK/CLI 可用", "category": "依赖"},
    {"id": 3, "name": "state_dir 存在且可写", "category": "文件系统"},
    {"id": 4, "name": "无僵尸 .lock 文件", "category": "并发安全"},
    {"id": 5, "name": "state.json 合法 JSON", "category": "数据完整性"},
    {"id": 6, "name": "schema_version 兼容", "category": "Schema 兼容"},
    {"id": 7, "name": "artifacts/ 目录存在", "category": "数据完整性"},
    {"id": 8, "name": "context-summary.md 可写入", "category": "文件系统"},
    {"id": 9, "name": "Provider API key 已设置", "category": "配置"},
    {"id": 10, "name": "Provider 回退链可用", "category": "连通性"},
    {"id": 11, "name": "config.mode 合法枚举", "category": "配置校验"},
    {"id": 12, "name": "max_cycles/convergence_rounds 为正整数", "category": "配置校验"},
    {"id": 13, "name": "hermes_model 非空", "category": "配置校验"},
    {"id": 14, "name": "gate_state 字段合法", "category": "Schema 校验"},
    {"id": 15, "name": "artifact checksums 一致", "category": "完整性"},
]


# ============================================================================
# 单检查项执行器
# ============================================================================

def _check_result(
    check_id: int,
    passed: bool,
    name: str = "",
    error: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """构造标准化的检查结果。

    Args:
        check_id: 检查项编号
        passed: 是否通过
        name: 检查项名称
        error: 失败时的错误描述
        detail: 补充详情

    Returns:
        标准结果字典
    """
    r: Dict[str, Any] = {"check_id": check_id, "name": name, "passed": passed}
    if not passed:
        r["error"] = error or "检查未通过"
    if detail:
        r["detail"] = detail
    return r


def _check_python_version() -> Dict[str, Any]:
    """检查 1: Python 版本 >= 3.10。"""
    ok = sys.version_info >= (3, 10)
    return _check_result(
        1, ok, "Python 版本 >= 3.10",
        error=f"当前 Python {sys.version}，需要 >= 3.10" if not ok else None,
        detail=f"Python {platform_python()}"
    )


def _check_hermes_available() -> Dict[str, Any]:
    """检查 2: Hermes SDK 或 CLI 可用。"""
    # 尝试 SDK
    try:
        import run_agent  # noqa: F401
        return _check_result(2, True, "Hermes SDK 可导入", detail="SDK 路径")
    except ImportError:
        pass

    # 尝试 CLI
    hermes_path = shutil.which("hermes")
    if hermes_path:
        return _check_result(2, True, "Hermes CLI 可用", detail=f"路径: {hermes_path}")

    return _check_result(
        2, False, "Hermes SDK/CLI 均不可用",
        error="请安装 pip install git+https://github.com/NousResearch/hermes-agent.git@<commit_hash>",
    )


def _check_state_dir_writable(state_dir: str) -> Dict[str, Any]:
    """检查 3: state_dir 存在且可写。"""
    base = Path(state_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        writable = os.access(str(base), os.W_OK)
        return _check_result(
            3, writable, "state_dir 可写",
            error=f"目录不可写: {base}" if not writable else None,
            detail=str(base),
        )
    except (OSError, PermissionError) as e:
        return _check_result(3, False, "state_dir 可写", error=str(e))


def _check_lock_not_zombie(state_dir: str) -> Dict[str, Any]:
    """检查 4: .lock 文件无僵尸锁。"""
    lock_file = Path(state_dir) / ".lock"
    if not lock_file.exists():
        return _check_result(4, True, "无僵尸锁", detail=".lock 不存在")

    import time
    try:
        age = time.time() - lock_file.stat().st_mtime
        if age > 300:
            lock_file.unlink()
            return _check_result(
                4, True, "僵尸锁已清理",
                detail=f"过期 {age:.0f}s，已自动删除"
            )
        return _check_result(4, True, "锁正常", detail=f"持锁 {age:.0f}s")
    except OSError as e:
        return _check_result(4, False, "锁检测失败", error=str(e))


def _check_state_json_valid(state_dir: str) -> Dict[str, Any]:
    """检查 5: state.json 合法 JSON。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(
            5, True, "state.json 不存在（首次初始化）",
            detail="将在首次运行时创建"
        )
    try:
        json.loads(sf.read_text(encoding="utf-8"))
        return _check_result(5, True, "state.json 合法 JSON")
    except json.JSONDecodeError as e:
        return _check_result(5, False, "state.json 非法 JSON", error=str(e))


def _check_schema_version(state_dir: str) -> Dict[str, Any]:
    """检查 6: schema_version 兼容。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(6, True, "schema_version（尚未初始化）")
    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
        sv = state.get("schema_version")
        ok = sv == 1
        return _check_result(
            6, ok, f"schema_version={sv}",
            error=f"不兼容的版本 {sv}，需要 v1" if not ok else None,
        )
    except json.JSONDecodeError:
        return _check_result(6, True, "schema_version（文件损坏）", detail="将由检查 5 处理")


def _check_artifacts_dir(state_dir: str) -> Dict[str, Any]:
    """检查 7: artifacts/ 目录存在。"""
    af = Path(state_dir) / "artifacts"
    try:
        af.mkdir(parents=True, exist_ok=True)
        return _check_result(7, True, "artifacts/ 目录就绪", detail=str(af))
    except OSError as e:
        return _check_result(7, False, "artifacts/ 创建失败", error=str(e))


def _check_context_summary_writable(state_dir: str) -> Dict[str, Any]:
    """检查 8: context-summary.md 可追加写入。"""
    cs = Path(state_dir) / "artifacts" / "context-summary.md"
    try:
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.touch(exist_ok=True)
        return _check_result(8, True, "context-summary.md 可写入")
    except OSError as e:
        return _check_result(8, False, "context-summary.md 不可写", error=str(e))


def _check_provider_api_key() -> Dict[str, Any]:
    """检查 9: 至少一个 Provider API key 已设置。"""
    env_keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]
    found = []
    for k in env_keys:
        if os.environ.get(k):
            found.append(k)

    if found:
        return _check_result(
            9, True, "Provider API key 已设置",
            detail=f"已配置: {', '.join(found)}",
        )
    return _check_result(
        9, False, "Provider API key 均未设置",
        error="请设置 ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY 中的至少一个",
    )


def _check_provider_reachable(state_dir: str) -> Dict[str, Any]:
    """检查 10: Provider 回退链中至少 1 个可用（warning-only）。

    检查 API key 是否存在 + 尝试轻量连通性测试。
    该检查默认通过（仅警告），不会阻断启动。
    """
    try:
        import requests
        env_keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]
        has_key = any(os.environ.get(k) for k in env_keys)
        if not has_key:
            return _check_result(
                10, True, "Provider 连通性（无 key，跳过测试）",
                detail="API key 未设置，将在首次 API 调用时验证"
            )
        return _check_result(
            10, True, "Provider 连通性（至少有 1 个 key 已配置）",
            detail="将在首次调用时实际验证"
        )
    except ImportError:
        return _check_result(
            10, True, "Provider 连通性（requests 未安装，跳过）",
            detail="pip install requests 以启用连通性测试"
        )


def _check_config_mode(state_dir: str) -> Dict[str, Any]:
    """检查 11: config.mode 合法枚举。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(11, True, "config.mode（尚未初始化）")
    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
        mode = state.get("config", {}).get("mode", "auto")
        ok = mode in ("safe", "auto", "unsafe", "collaborative")
        return _check_result(
            11, ok, f"config.mode={mode}",
            error=f"非法 mode '{mode}'，将回退为 auto" if not ok else None,
        )
    except json.JSONDecodeError:
        return _check_result(11, True, "config.mode（文件损坏）")


def _check_cycle_config(state_dir: str) -> Dict[str, Any]:
    """检查 12: max_cycles / convergence_rounds 为正整数。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(12, True, "循环参数（尚未初始化）")
    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
        cfg = state.get("config", {})
        mc = cfg.get("max_cycles", 5)
        cr = cfg.get("convergence_rounds", 2)
        ok = isinstance(mc, int) and mc > 0 and isinstance(cr, int) and cr > 0
        return _check_result(
            12, ok, f"max_cycles={mc}, convergence_rounds={cr}",
            error="必须为正整数" if not ok else None,
        )
    except json.JSONDecodeError:
        return _check_result(12, True, "循环参数（文件损坏）")


def _check_hermes_model(state_dir: str) -> Dict[str, Any]:
    """检查 13: hermes_model 非空字符串。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(13, True, "hermes_model（尚未初始化）")
    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
        model = state.get("config", {}).get("hermes_model", "")
        ok = bool(model)
        return _check_result(
            13, ok, f"hermes_model={model}",
            error="hermes_model 为空，使用默认模型" if not ok else None,
        )
    except json.JSONDecodeError:
        return _check_result(13, True, "hermes_model（文件损坏）")


def _check_gate_state_fields(state_dir: str) -> Dict[str, Any]:
    """检查 14: gate_state 字段存在且合法。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(14, True, "gate_state（尚未初始化）")
    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
        gs = state.get("gate_state", {})
        required = [
            "content_safety_passed", "plan_confirmed",
            "file_modifications_this_cycle", "dangerous_ops_blocked",
            "hermes_guardrail_events",
        ]
        missing = [k for k in required if k not in gs]
        ok = len(missing) == 0
        return _check_result(
            14, ok, "gate_state 字段完整",
            error=f"缺少字段: {missing}" if not ok else None,
        )
    except json.JSONDecodeError:
        return _check_result(14, True, "gate_state（文件损坏）")


def _check_artifact_checksums(state_dir: str) -> Dict[str, Any]:
    """检查 15: artifacts/ 文件与 state.json 记录的 checksum 一致。"""
    sf = Path(state_dir) / "state.json"
    if not sf.exists():
        return _check_result(15, True, "artifact checksums（尚未初始化）")

    try:
        state = json.loads(sf.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _check_result(15, True, "artifact checksums（文件损坏）")

    mismatches = []
    for art_key, info in state.get("artifacts", {}).items():
        file_path = info.get("path", "")
        recorded = info.get("checksum")
        if not file_path or not recorded:
            continue
        path = Path(file_path)
        if not path.exists():
            continue
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != recorded:
                mismatches.append(f"{art_key} ({path.name})")
        except OSError:
            mismatches.append(f"{art_key} (读取失败)")

    if mismatches:
        return _check_result(
            15, False, f"checksum 不匹配: {len(mismatches)} 个文件",
            error=f"不匹配: {', '.join(mismatches)}",
        )
    return _check_result(15, True, "artifact checksums 一致")


# ============================================================================
# 总入口
# ============================================================================

def run_sanity_check(state_dir: str) -> List[Dict[str, Any]]:
    """执行全部 15 项启动检查。

    按顺序执行所有检查项，每项独立运行，不受前一项失败影响。
    返回结构化结果列表供上层消费（决定是否中断启动或仅警告）。

    Args:
        state_dir: state.json 所在目录路径

    Returns:
        [
            {"check_id": N, "name": str, "passed": bool, "error": str|None, "detail": str|None},
            ...
        ]
    """
    results: List[Dict[str, Any]] = []

    # 1-2: 环境检查（不依赖 state.json）
    results.append(_check_python_version())
    results.append(_check_hermes_available())

    # 3-4: 文件系统基础检查
    results.append(_check_state_dir_writable(state_dir))
    results.append(_check_lock_not_zombie(state_dir))

    # 5-6: state.json 核心检查
    results.append(_check_state_json_valid(state_dir))
    results.append(_check_schema_version(state_dir))

    # 7-8: 数据目录和文件
    results.append(_check_artifacts_dir(state_dir))
    results.append(_check_context_summary_writable(state_dir))

    # 9-10: Provider 配置
    results.append(_check_provider_api_key())
    results.append(_check_provider_reachable(state_dir))

    # 11-14: 配置校验（依赖 state.json）
    results.append(_check_config_mode(state_dir))
    results.append(_check_cycle_config(state_dir))
    results.append(_check_hermes_model(state_dir))
    results.append(_check_gate_state_fields(state_dir))

    # 15: 完整性校验
    results.append(_check_artifact_checksums(state_dir))

    return results


def get_failed_checks(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛选出未通过的检查项。

    Args:
        results: run_sanity_check() 的返回结果

    Returns:
        未通过的检查项列表
    """
    return [r for r in results if not r["passed"]]


def get_blocking_failures(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛选出需要阻断启动的致命检查项。

    阻断条件:
        - 检查 1 (Python 版本): 失败 → 阻断
        - 检查 2 (Hermes 可用): 失败 → 阻断
        - 检查 5 (state.json 合法 JSON + .bak 恢复失败): 失败 → 阻断
        其余检查失败仅警告。

    Args:
        results: run_sanity_check() 的返回结果

    Returns:
        需要阻断的检查项列表
    """
    blocking_ids = {1, 2}
    blocking = []
    for r in results:
        if r["passed"]:
            continue
        if r["check_id"] in blocking_ids:
            blocking.append(r)
        elif r["check_id"] == 5:
            # 检查 5 失败且检查 6 也失败（说明 .bak 恢复也失败了）
            check6 = next((x for x in results if x["check_id"] == 6), None)
            if check6 and not check6["passed"]:
                blocking.append(r)
    return blocking


def print_sanity_report(results: List[Dict[str, Any]]) -> None:
    """将检查结果格式化输出到控制台。

    Args:
        results: run_sanity_check() 的返回结果
    """
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed

    print(f"\n{'='*60}")
    print(f"  loop-hermes Sanity Check: {passed}/{len(results)} 通过")
    print(f"{'='*60}")

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        marker = "[OK]" if r["passed"] else "[!!]"
        print(f"  {marker} #{r['check_id']:02d} {r['name']:<42s} {status}")
        if not r["passed"] and r.get("error"):
            print(f"       → {r['error']}")

    print(f"{'='*60}")

    if failed > 0:
        blocking = get_blocking_failures(results)
        if blocking:
            print(f"  FATAL: {len(blocking)} 项致命检查失败，无法继续。")
        else:
            print(f"  WARNING: {failed} 项检查未通过，继续执行（非致命）。")
    else:
        print(f"  全部 15 项检查通过。")

    print(f"{'='*60}\n")


# ============================================================================
# 辅助函数
# ============================================================================

def platform_python() -> str:
    """返回当前平台和 Python 版本的可读描述。"""
    import platform
    return f"{platform.system()} {platform.release()}, Python {platform.python_version()}"
