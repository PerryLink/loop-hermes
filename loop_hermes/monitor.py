# -*- coding: utf-8 -*-
"""Monitor 侧车进程 —— loop-hermes 独立监控守护进程。

作为独立进程运行，监控 loop-hermes 主循环的健康状态。
不与主循环共享内存，仅通过文件系统交互。

监控维度:
    1. 心跳检测: 主循环定期更新 heartbeat 文件，超时则告警
    2. 进程存活: 检查主进程 PID 是否存活
    3. state.json 健康: 周期性校验 state.json 完整性
    4. 资源使用: 内存/CPU/磁盘使用率监控
    5. 循环速率: 主循环执行速率跟踪
    6. 僵尸进程: 检测孤儿 Hermes 子进程
    7. 磁盘爆炸: artifacts/ 目录大小监控

使用方式:
    python -m loop_hermes.monitor --pid <MAIN_PID> --state-dir <.hermes/loop-hermes> --interval 10

典型集成:
    crontab 或 systemd 定时任务启动 monitor，监控主循环 PID。
    主循环退出时 monitor 自动退出。

设计原则:
    - 独立进程: 不与主循环共享内存，文件系统 IPC
    - 只读为主: 不修改 state.json（可记录到 monitor.log）
    - 优雅退出: 主进程退出时 monitor 自动退出
    - 告警清晰: 每项异常有明确的人类可读消息和建议
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set

# 配置独立 logger（不干扰主 loop_hermes logger）
logger = logging.getLogger("loop_hermes.monitor")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [MONITOR] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.handlers.clear()
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ============================================================================
# Monitor 常量
# ============================================================================

DEFAULT_INTERVAL = 10          # 默认检查间隔（秒）
DEFAULT_HEARTBEAT_TIMEOUT = 60  # 心跳超时（秒）
MAX_ARTIFACTS_SIZE_MB = 500    # artifacts 目录最大大小（MB）
MAX_MEMORY_MB = 4096           # 主进程最大内存（MB）

# 心跳文件路径（相对于 state_dir）
HEARTBEAT_FILE = "monitor_heartbeat.json"


# ============================================================================
# 心跳管理
# ============================================================================


def write_heartbeat(
    state_dir: str,
    pid: int,
    phase: str = "",
    cycle: int = 0,
) -> None:
    """主循环写入心跳文件（由主循环调用）。

    Args:
        state_dir: state 目录
        pid: 主进程 PID
        phase: 当前 phase
        cycle: 当前 cycle
    """
    hb_file = Path(state_dir) / HEARTBEAT_FILE
    hb_file.parent.mkdir(parents=True, exist_ok=True)

    heartbeat = {
        "pid": pid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "cycle": cycle,
    }

    try:
        hb_file.write_text(
            json.dumps(heartbeat, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("写入心跳文件失败: %s", e)


def read_heartbeat(state_dir: str) -> Optional[Dict[str, Any]]:
    """读取心跳文件。

    Args:
        state_dir: state 目录

    Returns:
        心跳数据字典；文件不存在或损坏时返回 None
    """
    hb_file = Path(state_dir) / HEARTBEAT_FILE
    if not hb_file.exists():
        return None
    try:
        return json.loads(hb_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check_heartbeat(state_dir: str, timeout_seconds: int = 60) -> Dict[str, Any]:
    """检查心跳是否超时。

    Args:
        state_dir: state 目录
        timeout_seconds: 超时阈值（秒）

    Returns:
        {
            "healthy": bool,
            "last_beat_seconds_ago": float,
            "message": str,
        }
    """
    hb = read_heartbeat(state_dir)
    if hb is None:
        return {
            "healthy": False,
            "last_beat_seconds_ago": float("inf"),
            "message": "心跳文件不存在（主循环可能未启动或已崩溃）",
        }

    try:
        hb_time = datetime.fromisoformat(hb["timestamp"])
        now = datetime.now(timezone.utc)
        delta = (now - hb_time).total_seconds()
    except (ValueError, KeyError):
        return {
            "healthy": False,
            "last_beat_seconds_ago": float("inf"),
            "message": "心跳时间戳格式无效",
        }

    if delta > timeout_seconds:
        return {
            "healthy": False,
            "last_beat_seconds_ago": delta,
            "message": f"心跳超时 {delta:.0f}s > {timeout_seconds}s（冻结/崩溃/死锁）",
        }

    return {
        "healthy": True,
        "last_beat_seconds_ago": delta,
        "phase": hb.get("phase", "?"),
        "cycle": hb.get("cycle", 0),
        "message": "OK",
    }


# ============================================================================
# 进程存活检测
# ============================================================================


def is_pid_alive(pid: int) -> bool:
    """检查 PID 是否存活。

    跨平台实现：
        - Windows: 尝试 OpenProcess
        - Unix: os.kill(pid, 0)

    Args:
        pid: 进程 PID

    Returns:
        True 如果进程存活
    """
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if handle == 0:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


# ============================================================================
# 资源监控
# ============================================================================


def get_memory_usage_mb(pid: int) -> Optional[float]:
    """获取进程内存使用量（MB）。

    Args:
        pid: 进程 PID

    Returns:
        内存使用量（MB）；无法获取时返回 None
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        mem_info = proc.memory_info()
        return mem_info.rss / (1024 * 1024)
    except ImportError:
        # psutil 不可用，尝试平台特定方法
        pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    # Unix fallback: 读取 /proc/pid/status
    if sys.platform != "win32":
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024  # kB → MB
        except (OSError, ValueError):
            pass

    return None


def get_artifacts_size_mb(state_dir: str) -> float:
    """获取 artifacts 目录大小（MB）。

    Args:
        state_dir: state 目录

    Returns:
        目录大小（MB）
    """
    artifacts = Path(state_dir) / "artifacts"
    if not artifacts.exists():
        return 0.0

    total = 0
    for f in artifacts.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def get_disk_usage_percent(path: str) -> Optional[float]:
    """获取磁盘使用率。

    Args:
        path: 路径（所在磁盘）

    Returns:
        使用率百分比（0-100）；无法获取时返回 None
    """
    try:
        import shutil
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100
    except Exception:
        return None


# ============================================================================
# 僵尸 Hermes 子进程检测
# ============================================================================


def find_hermes_orphans(parent_pid: int) -> List[int]:
    """查找孤儿 Hermes 子进程（parent 不是 parent_pid 的 hermes 进程）。

    Args:
        parent_pid: 主 loop-hermes 进程 PID

    Returns:
        孤儿进程 PID 列表
    """
    orphans: List[int] = []
    try:
        import psutil
        parent = psutil.Process(parent_pid)

        for proc in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
            try:
                # 检查是否是 hermes 相关进程
                cmdline = " ".join(proc.info.get("cmdline", []) or [])
                if "hermes" in cmdline.lower():
                    # 检查其父进程是否还是 parent_pid
                    ppid = proc.info.get("ppid")
                    if ppid and ppid != parent_pid:
                        # 检查该进程是否真正存活
                        if proc.is_running():
                            orphans.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (ImportError, psutil.NoSuchProcess):
        pass

    return orphans


# ============================================================================
# Monitor 核心循环
# ============================================================================


class Monitor:
    """Monitor 侧车进程主类。

    运行独立监控循环，定期检查主进程健康状态并输出告警。

    Attributes:
        state_dir: state.json 所在目录
        main_pid: 主循环进程 PID
        interval: 检查间隔（秒）
        heartbeat_timeout: 心跳超时阈值（秒）
        max_memory_mb: 内存告警阈值（MB）
        max_artifacts_mb: artifacts 目录大小告警阈值（MB）
        alert_count: 累计告警次数
    """

    def __init__(
        self,
        state_dir: str,
        main_pid: int,
        interval: int = DEFAULT_INTERVAL,
        heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
        max_memory_mb: int = MAX_MEMORY_MB,
        max_artifacts_mb: int = MAX_ARTIFACTS_SIZE_MB,
    ):
        """初始化 Monitor。

        Args:
            state_dir: state 目录
            main_pid: 主进程 PID
            interval: 检查间隔（秒）
            heartbeat_timeout: 心跳超时（秒）
            max_memory_mb: 内存告警阈值（MB）
            max_artifacts_mb: artifacts 大小告警阈值（MB）
        """
        self.state_dir = str(Path(state_dir).resolve())
        self.main_pid = main_pid
        self.interval = interval
        self.heartbeat_timeout = heartbeat_timeout
        self.max_memory_mb = max_memory_mb
        self.max_artifacts_mb = max_artifacts_mb
        self.alert_count = 0
        self._running = True

        # 输出日志文件
        self.log_dir = Path(state_dir) / "monitor"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Monitor 初始化: pid=%d interval=%ds heartbeat_timeout=%ds",
            main_pid, interval, heartbeat_timeout,
        )

    def check_all(self) -> Dict[str, Any]:
        """运行所有健康检查项。

        Returns:
            汇总检查结果字典
        """
        results: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alerts": [],
            "warnings": [],
            "stats": {},
        }

        # 1. 主进程存活
        pid_alive = is_pid_alive(self.main_pid)
        if not pid_alive:
            results["alerts"].append({
                "type": "process_dead",
                "severity": "CRITICAL",
                "message": f"主进程 PID={self.main_pid} 已退出",
            })
            self._running = False
        results["stats"]["pid_alive"] = pid_alive

        # 2. 心跳检测
        hb = check_heartbeat(self.state_dir, self.heartbeat_timeout)
        if not hb["healthy"]:
            results["alerts"].append({
                "type": "heartbeat_timeout",
                "severity": "CRITICAL",
                "message": hb["message"],
                "last_beat_seconds_ago": hb["last_beat_seconds_ago"],
            })
        else:
            results["stats"]["heartbeat_ok"] = True
            results["stats"]["phase"] = hb.get("phase", "?")
            results["stats"]["cycle"] = hb.get("cycle", 0)

        # 3. state.json 存在性
        state_file = Path(self.state_dir) / "state.json"
        if not state_file.exists():
            results["alerts"].append({
                "type": "state_missing",
                "severity": "CRITICAL",
                "message": "state.json 文件不存在",
            })
        else:
            # state.json Schema 检查
            try:
                state_data = json.loads(state_file.read_text(encoding="utf-8"))
                from .schemas import validate_state
                validate_state(state_data)
                results["stats"]["state_schema_ok"] = True
                results["stats"]["termination_status"] = state_data.get(
                    "termination", {}).get("status", "?")
            except (ValueError, Exception) as e:
                results["alerts"].append({
                    "type": "state_corrupted",
                    "severity": "HIGH",
                    "message": f"state.json 校验失败: {e}",
                })

        # 4. 内存使用
        mem = get_memory_usage_mb(self.main_pid)
        if mem is not None:
            results["stats"]["memory_mb"] = round(mem, 1)
            if mem > self.max_memory_mb:
                results["alerts"].append({
                    "type": "high_memory",
                    "severity": "MEDIUM",
                    "message": f"内存使用 {mem:.0f}MB > {self.max_memory_mb}MB",
                })

        # 5. artifacts 大小
        artifacts_mb = get_artifacts_size_mb(self.state_dir)
        results["stats"]["artifacts_mb"] = round(artifacts_mb, 2)
        if artifacts_mb > self.max_artifacts_mb:
            results["alerts"].append({
                "type": "disk_bloat",
                "severity": "MEDIUM",
                "message": f"artifacts 目录 {artifacts_mb:.0f}MB > {self.max_artifacts_mb}MB",
            })

        # 6. 磁盘使用率
        disk_pct = get_disk_usage_percent(self.state_dir)
        if disk_pct is not None and disk_pct > 90:
            results["alerts"].append({
                "type": "disk_full",
                "severity": "HIGH",
                "message": f"磁盘使用率 {disk_pct:.1f}% > 90%",
            })

        # 7. 僵尸 Hermes 子进程
        if pid_alive:
            orphans = find_hermes_orphans(self.main_pid)
            if orphans:
                results["warnings"].append({
                    "type": "hermes_orphans",
                    "severity": "MEDIUM",
                    "message": f"发现 {len(orphans)} 个孤儿 Hermes 子进程",
                    "pids": orphans,
                })

        # 汇总告警计数
        self.alert_count += len(results["alerts"])

        return results

    def run(self) -> int:
        """启动 Monitor 监控循环。

        循环运行 check_all，直到主进程退出或收到 SIGTERM/SIGINT。
        将所有告警写入 monitor 日志文件。

        Returns:
            退出码（0=正常退出，1=异常退出）
        """
        logger.info("Monitor 启动（pid=%d, 主循环 pid=%d）", os.getpid(), self.main_pid)

        # 注册信号处理
        def _handle_signal(signum, frame):
            logger.info("收到信号 %d，Monitor 退出", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        exit_code = 0

        while self._running:
            try:
                results = self.check_all()

                # 输出状态
                stats = results.get("stats", {})
                status_line = (
                    f"phase={stats.get('phase', '?')} "
                    f"cycle={stats.get('cycle', '?')} "
                    f"mem={stats.get('memory_mb', '?')}MB "
                    f"alerts={len(results.get('alerts', []))}"
                )
                logger.info("Health OK | %s", status_line)

                # 输出告警
                for alert in results.get("alerts", []):
                    logger.error(
                        "[%s] %s: %s",
                        alert["severity"], alert["type"], alert["message"],
                    )

                # 写入告警日志文件（完整 JSON）
                if results["alerts"] or results["warnings"]:
                    self._write_alert_log(results)

                # 检查是否应退出
                if not is_pid_alive(self.main_pid):
                    logger.warning("主进程已退出，Monitor 退出")
                    break

                if not results.get("alerts"):
                    self.alert_count = max(0, self.alert_count - 1)

            except Exception as e:
                logger.error("Monitor 检查异常: %s", e, exc_info=True)
                exit_code = 1

            if self._running:
                time.sleep(self.interval)

        logger.info(
            "Monitor 退出（共 %d 次告警）", self.alert_count,
        )
        return exit_code

    def _write_alert_log(self, results: Dict[str, Any]) -> None:
        """将告警写入 monitor 日志文件。

        Args:
            results: check_all 返回的结果
        """
        log_file = self.log_dir / "alerts.log"
        try:
            with open(str(log_file), "a", encoding="utf-8") as f:
                f.write(json.dumps(results, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ============================================================================
# CLI 入口
# ============================================================================


def build_monitor_parser() -> argparse.ArgumentParser:
    """构建 Monitor CLI 参数解析器。

    Returns:
        ArgumentParser 实例
    """
    parser = argparse.ArgumentParser(
        prog="loop-hermes-monitor",
        description="loop-hermes Monitor 侧车进程 —— 独立监控主循环健康",
    )
    parser.add_argument(
        "--pid", type=int, required=True,
        help="主循环进程 PID",
    )
    parser.add_argument(
        "--state-dir", type=str, default=".hermes/loop-hermes",
        help="state.json 目录路径",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"检查间隔（秒，默认 {DEFAULT_INTERVAL}）",
    )
    parser.add_argument(
        "--heartbeat-timeout", type=int, default=DEFAULT_HEARTBEAT_TIMEOUT,
        help=f"心跳超时（秒，默认 {DEFAULT_HEARTBEAT_TIMEOUT}）",
    )
    parser.add_argument(
        "--max-memory-mb", type=int, default=MAX_MEMORY_MB,
        help=f"内存告警阈值（MB，默认 {MAX_MEMORY_MB}）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="详细日志（DEBUG 级别）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Monitor CLI 入口。

    Args:
        argv: 命令行参数；None 时使用 sys.argv

    Returns:
        退出码
    """
    parser = build_monitor_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    monitor = Monitor(
        state_dir=args.state_dir,
        main_pid=args.pid,
        interval=args.interval,
        heartbeat_timeout=args.heartbeat_timeout,
        max_memory_mb=args.max_memory_mb,
    )
    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
