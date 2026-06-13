# -*- coding: utf-8 -*-
r"""Hermes 客户端抽象层。

实现 Hermes Agent 的双路径调用:
    - SDK 路径（优先）：Python `AIAgent(quiet_mode=True)` 直接调用
    - CLI 路径（降级）：subprocess `hermes chat -q` 调用

路径选择逻辑:
    启动时尝试 `import run_agent`:
        → 成功: 使用 SDK 路径（更低延迟，programmatic guardrail 事件回调）
        → 失败: 降级为 CLI 路径（PyInstaller 二进制场景的默认路径）
        → CLI 路径检测 `hermes` 命令是否在 PATH 中 → 不可用则报错退出

注意事项:
    - `hermes -z` 标志不存在于 Hermes Agent 源码中
    - 使用 `hermes chat -q`（quiet mode）或 `AIAgent(quiet_mode=True)`
    - hermes chat -q 后面跟 --message "prompt" 传递内容
"""

import os
import sys
import json
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("loop_hermes.hermes_client")

# ============================================================================
# SDK 导入检测（模块级缓存）
# ============================================================================

_SDK_AVAILABLE: Optional[bool] = None
_AIAgent = None


def _probe_sdk_import() -> bool:
    """探测试图导入 Hermes Agent SDK 的 AIAgent 类。

    使用模块级缓存，避免重复 import。

    Returns:
        True 如果 SDK 可导入
    """
    global _SDK_AVAILABLE, _AIAgent
    if _SDK_AVAILABLE is not None:
        return _SDK_AVAILABLE

    try:
        from run_agent import AIAgent  # noqa: F401
        _AIAgent = AIAgent
        _SDK_AVAILABLE = True
        logger.info("Hermes SDK 导入成功（run_agent.AIAgent）")
        return True
    except ImportError:
        _SDK_AVAILABLE = False
        logger.info("Hermes SDK 不可导入，将使用 CLI 降级路径")
        return False


# ============================================================================
# 引擎检测
# ============================================================================

def detect_hermes_engine() -> str:
    """检测当前可用的 Hermes 调用路径。

    检测顺序:
        1. Python SDK（import run_agent）
        2. CLI 命令（hermes 在 PATH 中）

    Returns:
        "sdk" 或 "cli"

    Raises:
        RuntimeError: 两条路径均不可用
    """
    # 优先 SDK
    if _probe_sdk_import():
        return "sdk"

    # 降级 CLI
    hermes_path = shutil.which("hermes")
    if hermes_path:
        logger.info("检测到 Hermes CLI: %s", hermes_path)
        return "cli"

    raise RuntimeError(
        "未检测到 Hermes Agent。请先安装:\n"
        "  pip install git+https://github.com/NousResearch/hermes-agent.git@<commit_hash>\n"
        "或确保 'hermes' 命令在 PATH 中可用。"
    )


# ============================================================================
# 健康检查
# ============================================================================

def check_health(engine: Optional[str] = None) -> Dict[str, Any]:
    """检测 Hermes 是否可用。

    Args:
        engine: 指定引擎类型（"sdk" 或 "cli"）。
                为 None 时自动检测。

    Returns:
        {
            "healthy": bool,
            "engine": str,      # "sdk" 或 "cli"
            "version": str,     # 版本号（CLI 路径可用）
            "error": str|None,  # 不可用时的错误描述
        }
    """
    if engine is None:
        try:
            engine = detect_hermes_engine()
        except RuntimeError as e:
            return {"healthy": False, "engine": "unknown", "version": "", "error": str(e)}

    if engine == "sdk":
        if _probe_sdk_import():
            return {"healthy": True, "engine": "sdk", "version": "sdk", "error": None}
        return {"healthy": False, "engine": "sdk", "version": "", "error": "SDK 导入失败"}

    if engine == "cli":
        hermes_path = shutil.which("hermes")
        if hermes_path:
            try:
                result = subprocess.run(
                    ["hermes", "--version"],
                    capture_output=True, text=True, timeout=10,
                )
                version = result.stdout.strip() or result.stderr.strip()
            except Exception:
                version = "unknown"
            return {"healthy": True, "engine": "cli", "version": version, "error": None}
        return {"healthy": False, "engine": "cli", "version": "", "error": "hermes 不在 PATH 中"}

    return {"healthy": False, "engine": engine, "version": "", "error": f"未知引擎: {engine}"}


# ============================================================================
# 消息发送
# ============================================================================

def send_message(
    prompt: str,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    toolsets: Optional[list] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    """向 Hermes Agent 发送 prompt 并获取响应。

    根据 engine 自动选择 SDK 或 CLI 路径。

    Args:
        prompt: 发送给 Hermes Agent 的 prompt 文本
        engine: 调用路径（"sdk" / "cli"）。为 None 时自动检测
        model: 模型 ID（仅 CLI 路径使用）
        toolsets: 工具集列表（仅 CLI 路径使用）
        timeout: 超时时间（秒），默认 600（10 分钟）

    Returns:
        {
            "success": bool,
            "output": str,          # Hermes 响应文本
            "engine": str,          # 实际使用的引擎
            "guardrail_events": [], # guardrail 事件列表
            "error": str|None,
        }
    """
    if engine is None:
        try:
            engine = detect_hermes_engine()
        except RuntimeError as e:
            return {
                "success": False, "output": "", "engine": "unknown",
                "guardrail_events": [], "error": str(e),
            }

    if engine == "sdk":
        return _send_via_sdk(prompt, timeout)
    return _send_via_cli(prompt, model, toolsets, timeout)


# ============================================================================
# SDK 路径
# ============================================================================

def _send_via_sdk(prompt: str, timeout: int) -> Dict[str, Any]:
    """通过 AIAgent Python SDK 发送 prompt。

    Args:
        prompt: prompt 文本
        timeout: 超时秒数

    Returns:
        标准结果字典
    """
    if not _probe_sdk_import():
        return {
            "success": False, "output": "", "engine": "sdk",
            "guardrail_events": [], "error": "SDK 不可用",
        }

    try:
        agent = _AIAgent(quiet_mode=True)
        result = agent.run(prompt)

        # 解析 guardrail 事件
        guardrail_events = _extract_guardrail_from_sdk_result(result)

        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return {
            "success": True, "output": output, "engine": "sdk",
            "guardrail_events": guardrail_events, "error": None,
        }
    except Exception as e:
        logger.error("SDK 调用失败: %s", e, exc_info=True)
        return {
            "success": False, "output": "", "engine": "sdk",
            "guardrail_events": [], "error": str(e),
        }


def _extract_guardrail_from_sdk_result(result) -> list:
    """从 SDK 返回结果中提取 guardrail 事件。

    解析 AIAgent.run() 返回的 dict 中的 guardrail_log 字段，
    提取 type/tool/message/timestamp 四个字段。

    Args:
        result: AIAgent.run() 的返回结果（dict 或 str）。

    Returns:
        guardrail 事件列表，每个事件为 {type, tool, message, timestamp} 字典。
        若 result 为非 dict 或无 guardrail_log 则返回空列表。
    """
    events = []
    if isinstance(result, dict):
        raw = result.get("guardrail_log", [])
        for item in raw:
            if isinstance(item, dict):
                events.append({
                    "type": item.get("type", "UNKNOWN"),
                    "tool": item.get("tool", ""),
                    "message": item.get("message", ""),
                    "timestamp": item.get("timestamp", ""),
                })
    return events


# ============================================================================
# CLI 路径
# ============================================================================

_KNOWN_GUARDRAIL_TAGS = {
    "HARDLINE", "HARDLINE_BLOCK", "WARN", "WARN_PATTERN",
    "APPROVAL_DENY", "APPROVAL_TIMEOUT",
}


def _send_via_cli(
    prompt: str,
    model: Optional[str] = None,
    toolsets: Optional[list] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    """通过 subprocess 调用 hermes chat -q 发送 prompt。

    CLI 命令格式:
        hermes chat -q --message "<prompt>" [--model <model>] [--toolsets <toolsets>]

    Args:
        prompt: prompt 文本
        model: 模型 ID
        toolsets: 工具集列表
        timeout: 超时秒数

    Returns:
        标准结果字典
    """
    hermes_path = shutil.which("hermes")
    if not hermes_path:
        return {
            "success": False, "output": "", "engine": "cli",
            "guardrail_events": [], "error": "hermes 命令不在 PATH 中",
        }

    cmd = [hermes_path, "chat", "-q", "--message", prompt]
    if model:
        cmd.extend(["--model", model])
    if toolsets:
        if isinstance(toolsets, str):
            toolsets = toolsets.split(",")
        cmd.extend(["--toolsets", ",".join(toolsets)])

    logger.debug("CLI 调用: %s", " ".join(cmd[:4]) + " ...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd(),
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # 从输出中提取 guardrail 标记
        guardrail_events = _extract_guardrail_from_cli_output(stdout)

        if result.returncode != 0:
            return {
                "success": False,
                "output": stdout,
                "engine": "cli",
                "guardrail_events": guardrail_events,
                "error": f"hermes 退出码 {result.returncode}: {stderr[:500]}",
            }

        return {
            "success": True,
            "output": stdout,
            "engine": "cli",
            "guardrail_events": guardrail_events,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "output": "", "engine": "cli",
            "guardrail_events": [],
            "error": f"Hermes CLI 调用超时（{timeout}s）",
        }
    except Exception as e:
        return {
            "success": False, "output": "", "engine": "cli",
            "guardrail_events": [], "error": f"CLI 调用异常: {e}",
        }


def _extract_guardrail_from_cli_output(stdout: str) -> list:
    """从 CLI stdout 中解析 [GUARDRAIL:*] 标记。

    Hermes CLI 在触发 guardrail 时输出带标记的行:
        [GUARDRAIL:HARDLINE] tool=shell_call message=blocked: ...

    Args:
        stdout: CLI 标准输出

    Returns:
        解析出的 guardrail 事件列表
    """
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("[GUARDRAIL:"):
            continue

        # 解析 [GUARDRAIL:TYPE] 标记
        bracket_end = line.find("]")
        if bracket_end == -1:
            continue
        tag = line[1:bracket_end]
        gtype = tag.split(":", 1)[-1] if ":" in tag else "UNKNOWN"
        if gtype not in _KNOWN_GUARDRAIL_TAGS:
            continue

        event = {"type": gtype, "tool": "", "message": "", "timestamp": ""}
        rest = line[bracket_end + 1:].strip()
        # 解析 key=value 对
        for part in rest.split():
            if "=" in part:
                k, _, v = part.partition("=")
                k = k.strip().lower()
                if k in ("tool", "message", "timestamp"):
                    event[k] = v
        event.setdefault("message", rest[:200])
        events.append(event)

    return events


# ============================================================================
# 高层封装：invoke_hermes
# ============================================================================

def invoke_hermes(
    prompt: str,
    phase: str = "",
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    """Hermes Agent 调用的统一入口。

    供 phase_dispatch 使用的高层封装，自动从 state 中读取
    engine、model、toolsets 配置。

    集成功能:
        - ProviderFallbackManager: 检查 provider 可用性状态
        - GuardrailMapper: 处理返回的 guardrail 事件并注入 state

    Args:
        prompt: 发送给 Hermes 的完整 prompt
        phase: 当前 phase 名称（用于日志）
        state: state 字典（可选，用于读取配置）

    Returns:
        {
            "success": bool,
            "output": str,
            "engine": str,
            "guardrail_events": list,
            "guardrail_summary": dict|None,  # guardrail 处理摘要
            "error": str|None,
        }
    """
    engine = None
    model = None
    toolsets = None

    if state is not None:
        engine = state["progress"].get("hermes_engine")
        model = state["config"].get("hermes_model")
        toolsets = state["config"].get("hermes_toolsets")

        # Provider 可用性预检查
        _check_provider_availability(state)

    result = send_message(
        prompt=prompt,
        engine=engine,
        model=model,
        toolsets=toolsets,
    )

    # Guardrail 事件处理
    guardrail_summary = None
    if state is not None and result.get("guardrail_events"):
        guardrail_summary = _process_invoke_guardrails(
            state=state,
            result=result,
            phase=phase,
        )

    result["guardrail_summary"] = guardrail_summary
    return result


def _check_provider_availability(state: dict) -> None:
    """检查 provider 回退管理器的可用性状态。

    调用 ProviderFallbackManager.all_exhausted() 检查是否所有
    LLM provider 均已不可用。若已全部耗尽则设置 termination.status = "failed"
    并记录退出原因。

    Args:
        state: state 字典（原地修改 termination 字段）。

    Raises:
        无显式抛出——ImportError 和通用异常均被静默捕获。
    """
    try:
        from .provider_fallback import get_global_fallback_manager
        mgr = get_global_fallback_manager()
        if mgr.all_exhausted():
            logger.error("所有 provider 均已不可用，工作流终止")
            state["termination"]["status"] = "failed"
            state["termination"]["exit_reason"] = (
                "所有 LLM provider 均已不可用（HEALTHY=0）"
            )
    except ImportError:
        logger.debug("provider_fallback 不可导入，跳过可用性检查")
    except Exception as e:
        logger.warning("Provider 可用性检查失败: %s", e)


def _process_invoke_guardrails(
    state: dict,
    result: Dict[str, Any],
    phase: str,
) -> Optional[Dict[str, Any]]:
    """处理 invoke_hermes 返回的 guardrail 事件。

    将 Hermes Agent 返回的 guardrail 事件通过 guardrail_mapper 映射为标准 issue
    并注入到 state["issues"]["active"] 中。若 guardrail_mapper 不可用则跳过注入。

    Args:
        state:  state 字典（原地修改 issues.active）。
        result: Hermes 调用结果字典（含 guardrail_events 字段）。
        phase:  当前 phase 名称（用于 issue 溯源标记）。

    Returns:
        guardrail 处理摘要字典（含 total/injected/skipped 计数），
        若无事件则返回 None。
    """
    events = result.get("guardrail_events", [])
    if not events:
        return None

    try:
        from .guardrail_mapper import inject_guardrail_issues_into_state
        summary = inject_guardrail_issues_into_state(state, events, phase)
        logger.info("Guardrail 注入完成: %d 个事件 → state", summary["total"])
        return summary
    except ImportError:
        logger.warning("guardrail_mapper 不可导入，跳过 guardrail 注入")
        return None
    except Exception as e:
        logger.error("Guardrail 注入失败: %s", e, exc_info=True)
        return None
