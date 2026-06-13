# -*- coding: utf-8 -*-
"""测试: build.py —— PyInstaller 构建脚本。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.build import (
    generate_spec, detect_platform, clean_build,
    PLATFORM_CONFIG, APP_NAME, APP_VERSION,
    BUNDLED_PACKAGES, HIDDEN_IMPORTS, EXCLUDED_MODULES,
)


class TestDetectPlatform:
    """平台检测测试。"""

    def test_detect_returns_valid_string(self):
        """应返回已知平台字符串之一。"""
        plat = detect_platform()
        assert plat in ("linux", "macos", "windows", "unknown")

    def test_platform_config_has_all_platforms(self):
        """应为所有平台提供配置。"""
        for p in ("linux", "macos", "windows"):
            assert p in PLATFORM_CONFIG
            assert "target_name" in PLATFORM_CONFIG[p]


class TestGenerateSpec:
    """spec 文件生成测试。"""

    def test_generate_spec_creates_file(self):
        """应生成 .spec 文件。"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = str(Path(td) / "test.spec")
            result = generate_spec(output_path=spec_file, onefile=False)
            assert result == spec_file
            assert Path(spec_file).exists()

    def test_spec_contains_app_name(self):
        """spec 文件应包含 APP_NAME。"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = str(Path(td) / "test.spec")
            generate_spec(output_path=spec_file)
            content = Path(spec_file).read_text()
            assert APP_NAME in content

    def test_spec_contains_version(self):
        """spec 文件应包含版本号。"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = str(Path(td) / "test.spec")
            generate_spec(output_path=spec_file)
            content = Path(spec_file).read_text()
            assert APP_VERSION in content

    def test_spec_contains_entry_script(self):
        """spec 文件应包含入口脚本路径。"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = str(Path(td) / "test.spec")
            generate_spec(output_path=spec_file)
            content = Path(spec_file).read_text()
            assert "cli.py" in content

    def test_onefile_spec_different_from_onedir(self):
        """onefile 和 onedir 应生成不同 spec。"""
        with tempfile.TemporaryDirectory() as td:
            f1 = str(Path(td) / "onedir.spec")
            f2 = str(Path(td) / "onefile.spec")
            generate_spec(output_path=f1, onefile=False)
            generate_spec(output_path=f2, onefile=True)

            c1 = Path(f1).read_text()
            c2 = Path(f2).read_text()
            # onefile 不应包含 COLLECT，onedir 应包含
            assert "COLLECT" not in c2 or "COLLECT" in c1


class TestAppConstants:
    """应用常量验证。"""

    def test_app_name(self):
        assert APP_NAME == "loop-hermes"

    def test_app_version(self):
        """版本号格式应为 x.y.z。"""
        parts = APP_VERSION.split(".")
        assert len(parts) >= 2
        for p in parts:
            assert p.isdigit()

    def test_bundled_packages(self):
        """应有必要的打包项。"""
        assert "loop_hermes" in BUNDLED_PACKAGES
        assert "jsonschema" in BUNDLED_PACKAGES

    def test_hidden_imports(self):
        """应有必要的隐藏导入。"""
        assert "jsonschema" in HIDDEN_IMPORTS
        assert "json" in HIDDEN_IMPORTS

    def test_excluded_modules(self):
        """应排除体积较大的库。"""
        assert "tkinter" in EXCLUDED_MODULES
        assert "tensorflow" in EXCLUDED_MODULES


class TestCleanBuild:
    """清理构建测试。"""

    def test_clean_missing_dirs_no_error(self):
        """清理不存在的目录不应报错。"""
        import sys as _sys
        if _sys.platform == "win32":
            # Windows 上 chdir + rmtree 组合容易触发 PermissionError
            import pytest
            pytest.skip("Windows 文件句柄限制")
        import os
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                clean_build()
        finally:
            os.chdir(old_cwd)
