# -*- coding: utf-8 -*-
"""测试: platform_config.py —— 平台配置（OS 检测、路径、权限）。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.platform_config import (
    detect_os, get_os_info, normalize_path, get_state_dir,
    resolve_runtime_paths, check_path_permissions,
    ensure_directory_writable, get_default_scheduler,
    get_scheduler_examples,
)


class TestOSDetection:

    def test_detect_os_returns_known(self):
        """OS 检测应返回已知类型。"""
        result = detect_os()
        assert result in ("linux", "macos", "windows")

    def test_get_os_info_has_keys(self):
        """OS 信息应包含关键字段。"""
        info = get_os_info()
        assert "os" in info
        assert "platform" in info
        assert "release" in info
        assert "machine" in info
        assert "python_version" in info
        assert "python_executable" in info


class TestPathNormalization:

    def test_normalize_returns_absolute(self):
        """normalize_path 应返回绝对路径。"""
        result = normalize_path(".")
        assert Path(result).is_absolute()

    def test_get_state_dir_default(self):
        """默认 state_dir 应在当前工作目录下。"""
        result = get_state_dir()
        assert result.name == "loop-hermes"
        assert result.parent.name == ".hermes"

    def test_get_state_dir_custom(self):
        """自定义 state_dir 应被使用。"""
        result = get_state_dir("/custom/path")
        assert result == Path("/custom/path").resolve()

    def test_resolve_runtime_paths(self):
        """运行时路径解析应返回所有关键路径。"""
        paths = resolve_runtime_paths("/tmp/test-state")
        assert "state_file" in paths
        assert "lock_file" in paths
        assert "bak_file" in paths
        assert "artifacts_dir" in paths
        assert "runs_log" in paths


class TestPermissions:

    def test_check_existing_directory(self):
        """检查已有目录的权限。"""
        with tempfile.TemporaryDirectory() as tmp:
            result = check_path_permissions(tmp)
            assert result["exists"] is True

    def test_check_nonexistent_path(self):
        """检查不存在路径的权限。"""
        result = check_path_permissions("/nonexistent/test/path/check")
        assert result["exists"] is False

    def test_ensure_directory_writable(self):
        """确保目录可写（应创建新目录）。"""
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = Path(tmp) / "new" / "subdir"
            result = ensure_directory_writable(str(new_dir))
            assert result is True
            assert new_dir.exists()


class TestScheduler:

    def test_default_scheduler_non_empty(self):
        """调度器推荐不应为空。"""
        result = get_default_scheduler()
        assert len(result) > 0

    def test_scheduler_examples_all_platforms(self):
        """应覆盖所有平台。"""
        examples = get_scheduler_examples()
        for platform in ("linux", "macos", "windows", "generic"):
            assert platform in examples, f"缺少 {platform} 平台示例"
            assert len(examples[platform]) > 0
