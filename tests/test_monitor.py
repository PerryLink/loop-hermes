# -*- coding: utf-8 -*-
"""测试: monitor.py —— Monitor 侧车进程。"""

import os
import sys
import time
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.monitor import (
    write_heartbeat, read_heartbeat, check_heartbeat,
    is_pid_alive, get_artifacts_size_mb,
    DEFAULT_INTERVAL, DEFAULT_HEARTBEAT_TIMEOUT,
)


class TestHeartbeat:
    """心跳文件读写测试。"""

    def test_write_and_read_heartbeat(self):
        """写入后应能正确读取。"""
        with tempfile.TemporaryDirectory() as td:
            write_heartbeat(td, pid=12345, phase="part_2_1", cycle=3)
            hb = read_heartbeat(td)
            assert hb is not None
            assert hb["pid"] == 12345
            assert hb["phase"] == "part_2_1"
            assert hb["cycle"] == 3

    def test_read_missing_heartbeat(self):
        """无心跳文件时应返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            hb = read_heartbeat(td)
            assert hb is None

    def test_heartbeat_file_contains_timestamp(self):
        """心跳应包含时间戳。"""
        with tempfile.TemporaryDirectory() as td:
            write_heartbeat(td, pid=9999)
            hb = read_heartbeat(td)
            assert "timestamp" in hb

    def test_check_heartbeat_fresh(self):
        """新鲜心跳应报告健康。"""
        with tempfile.TemporaryDirectory() as td:
            write_heartbeat(td, pid=os.getpid())
            result = check_heartbeat(td, timeout_seconds=60)
            assert result["healthy"] is True

    def test_check_heartbeat_missing(self):
        """无心跳应报告不健康。"""
        with tempfile.TemporaryDirectory() as td:
            result = check_heartbeat(td, timeout_seconds=60)
            assert result["healthy"] is False

    def test_check_heartbeat_expired(self):
        """过期心跳应报告不健康。"""
        with tempfile.TemporaryDirectory() as td:
            # 写入一个过期的心跳
            hb_data = {
                "pid": 12345,
                "timestamp": "2020-01-01T00:00:00Z",
                "phase": "init",
                "cycle": 0,
            }
            hb_file = Path(td) / "monitor_heartbeat.json"
            hb_file.write_text(json.dumps(hb_data))

            result = check_heartbeat(td, timeout_seconds=60)
            assert result["healthy"] is False


class TestProcessAlive:
    """进程存活检测测试。"""

    def test_current_process_is_alive(self):
        """当前进程应报告存活。"""
        assert is_pid_alive(os.getpid()) is True

    def test_invalid_pid_is_dead(self):
        """无效 PID（如 0）应报告死亡。"""
        # PID 0 在 Windows 上是 System Idle Process，在 Unix 上是无效的
        # 使用一个极大且不存在的 PID
        result = is_pid_alive(99999999)
        # 不存在的 PID 预期为 False
        assert result is False


class TestArtifactsSize:
    """artifacts 目录大小测试。"""

    def test_empty_dir_size_zero(self):
        with tempfile.TemporaryDirectory() as td:
            assert get_artifacts_size_mb(td) == 0.0

    def test_dir_with_files(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td) / "artifacts"
            artifacts.mkdir()
            # 写入一些文件
            for i in range(5):
                (artifacts / f"file_{i}.txt").write_text("x" * 1024)

            size = get_artifacts_size_mb(td)
            # 5 * 1024 bytes ~= 0.005 MB
            assert size > 0.0


class TestConstants:
    """常量验证。"""

    def test_default_interval(self):
        assert DEFAULT_INTERVAL == 10

    def test_default_heartbeat_timeout(self):
        assert DEFAULT_HEARTBEAT_TIMEOUT == 60
