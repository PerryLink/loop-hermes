# -*- coding: utf-8 -*-
"""平台配置模块。

管理跨平台适配，包括:
    - OS 检测（Windows / Linux / macOS）
    - 路径标准化（分平台路径规范化）
    - 权限检测（文件可读/可写/可执行）
    - 默认调度器推荐（cron / Task Scheduler / sleep loop）
    - Hermes 调用路径探测（SDK vs CLI）
"""

import os
import sys
import platform
import shutil
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("loop_hermes.platform_config")


# ============================================================================
# OS 检测
# ============================================================================

def detect_os() -> str:
    """检测当前操作系统。

    Returns:
        "linux" / "macos" / "windows" / "unknown"
    """
    s = sys.platform
    if s.startswith("linux"):
        return "linux"
    if s == "darwin":
        return "macos"
    if s in ("win32", "cygwin"):
        return "windows"
    return "unknown"


def get_os_info() -> Dict[str, str]:
    """获取详细的 OS 信息。

    Returns:
        {
            "os": str,              # linux / macos / windows
            "platform": str,        # sys.platform 原始值
            "release": str,         # platform.release()
            "machine": str,         # platform.machine()
            "python_version": str,  # Python 版本
            "python_executable": str, # Python 解释器路径
        }
    """
    return {
        "os": detect_os(),
        "platform": sys.platform,
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
    }


# ============================================================================
# 路径标准化
# ============================================================================

def normalize_path(path: str) -> str:
    """跨平台路径标准化。

    - Windows: 统一使用反斜杠 → 正斜杠，移除多余分隔符
    - Linux/macOS: 解析 ~ 和相对路径

    Args:
        path: 原始路径字符串

    Returns:
        标准化的绝对路径字符串
    """
    return str(Path(path).expanduser().resolve())


def get_state_dir(custom_dir: Optional[str] = None) -> Path:
    """获取 state_dir 的标准路径。

    优先级:
        1. 用户指定的 custom_dir
        2. 当前工作目录下的 .hermes/loop-hermes/

    Args:
        custom_dir: 用户指定的自定义目录（可选）

    Returns:
        标准化的 state_dir Path 对象
    """
    if custom_dir:
        return Path(custom_dir).expanduser().resolve()
    return Path.cwd() / ".hermes" / "loop-hermes"


def resolve_runtime_paths(state_dir: str) -> Dict[str, Path]:
    """解析 state_dir 下的所有运行时文件路径。

    Args:
        state_dir: state.json 所在目录路径

    Returns:
        {
            "state_file": Path,     # state.json
            "lock_file": Path,      # .lock
            "bak_file": Path,       # state.json.bak
            "tmp_file": Path,       # state.json.tmp
            "artifacts_dir": Path,  # artifacts/
            "runs_log": Path,       # runs.log
            "parallel_dir": Path,   # parallel/agents/
        }
    """
    base = Path(state_dir).resolve()
    return {
        "state_file": base / "state.json",
        "lock_file": base / ".lock",
        "bak_file": base / "state.json.bak",
        "tmp_file": base / "state.json.tmp",
        "artifacts_dir": base / "artifacts",
        "runs_log": base / "runs.log",
        "parallel_dir": base / "parallel" / "agents",
    }


# ============================================================================
# 权限检测
# ============================================================================

def check_path_permissions(path: str) -> Dict[str, bool]:
    """检测路径的读写执行权限。

    Args:
        path: 要检测的路径

    Returns:
        {
            "exists": bool,
            "readable": bool,
            "writable": bool,
            "executable": bool,
        }
    """
    p = Path(path)
    exists = p.exists()
    result = {
        "exists": exists,
        "readable": os.access(str(p), os.R_OK) if exists else False,
        "writable": os.access(str(p), os.W_OK) if exists else False,
        "executable": os.access(str(p), os.X_OK) if exists else False,
    }

    # 对于不存在的路径，检查父目录是否可写
    if not exists:
        parent = p.parent
        if parent.exists():
            result["writable"] = os.access(str(parent), os.W_OK)
        else:
            # 递归向上检查直到找到存在的目录
            ancestor = parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            result["writable"] = os.access(str(ancestor), os.W_OK) if ancestor.exists() else False

    return result


def ensure_directory_writable(path: str) -> bool:
    """确保目录存在且可写，必要时创建。

    Args:
        path: 目录路径

    Returns:
        True 如果目录存在且可写
    """
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return os.access(str(p), os.W_OK)
    except (OSError, PermissionError) as e:
        logger.error("无法创建/写入目录 %s: %s", path, e)
        return False


# ============================================================================
# 默认调度器推荐
# ============================================================================

def get_default_scheduler() -> str:
    """根据当前 OS 推荐默认外部调度器。

    Returns:
        - Linux: "cron（建议 */5 * * * *）"
        - macOS: "cron 或 launchd"
        - Windows: "Task Scheduler（schtasks）"
        - 其他: "sleep loop（跨平台兜底）"
    """
    os_name = detect_os()
    if os_name == "linux":
        return "cron（建议 */5 * * * * loop-hermes --no-pause）"
    if os_name == "macos":
        return "cron 或 launchd"
    if os_name == "windows":
        return "Task Scheduler（schtasks）"
    return "sleep loop（跨平台兜底方案）"


def get_scheduler_examples() -> Dict[str, str]:
    """获取各平台调度器配置示例。

    Returns:
        {"linux": str, "macos": str, "windows": str, "generic": str}
    """
    return {
        "linux": (
            "# 添加到 crontab:\n"
            "# */5 * * * * cd /path/to/project && loop-hermes --no-pause\n"
        ),
        "macos": (
            "# 方案 A: crontab（同上 Linux）\n"
            "# 方案 B: launchd plist\n"
            "#   /Library/LaunchDaemons/com.loop-hermes.plist\n"
        ),
        "windows": (
            "# PowerShell 命令:\n"
            "# schtasks /create /tn loop-hermes /tr "
            "\"C:\\tools\\loop-hermes.exe --no-pause\" /sc minute /mo 5\n"
        ),
        "generic": (
            "#!/bin/bash\n"
            "# 跨平台 sleep loop 兜底方案\n"
            "while true; do\n"
            "  loop-hermes --no-pause\n"
            "  sleep 300  # 5 分钟间隔\n"
            "done\n"
        ),
    }


# ============================================================================
# Hermes 路径探测
# ============================================================================

def probe_hermes_cli_path() -> Optional[str]:
    """探测 hermes CLI 命令的完整路径。

    Returns:
        hermes 命令的路径；不在 PATH 中返回 None
    """
    path = shutil.which("hermes")
    if path:
        logger.info("Hermes CLI 路径: %s", path)
        return path
    return None


def probe_hermes_sdk_available() -> bool:
    """检测 Hermes Agent SDK（run_agent 模块）是否可导入。

    Returns:
        True 如果 `import run_agent` 成功
    """
    try:
        import run_agent  # noqa: F401
        logger.info("Hermes SDK 可导入")
        return True
    except ImportError:
        logger.info("Hermes SDK 不可导入")
        return False


def get_hermes_install_instructions() -> str:
    """生成 Hermes Agent 安装指引（中文）。

    Returns:
        安装说明字符串
    """
    return (
        "请先安装 Hermes Agent:\n"
        "  pip install git+https://github.com/NousResearch/hermes-agent.git@<commit_hash>\n"
        "或克隆仓库后:\n"
        "  git clone https://github.com/NousResearch/hermes-agent.git\n"
        "  cd hermes-agent && pip install -e .\n"
    )
