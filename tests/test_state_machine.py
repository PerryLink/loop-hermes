# -*- coding: utf-8 -*-
"""测试: state_machine.py —— 状态机核心（原子写入、锁、备份、恢复、checksum）。"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.state_machine import (
    load_or_init_state, atomic_write_state,
    acquire_lock, release_lock,
    backup_state, restore_from_backup,
    compute_artifact_checksum, update_artifact_meta,
    verify_artifact_integrity,
    DEFAULT_STATE_TEMPLATE,
    is_terminated, is_cycle_exceeded, is_converged,
    state_exists,
)


class FakeArgs:
    """模拟 CLI 参数对象。"""
    state_dir = ""
    safe = False
    unsafe = False
    interactive = False
    goal = "build a weather CLI"
    max_cycles = 5
    convergence_rounds = 2
    hermes_model = "claude-sonnet-4-20250514"
    hermes_toolsets = "code,shell"
    provider_fallback = "claude,openai,deepseek"
    skip_testing = False


class TestInitState:

    def test_init_creates_fresh_state(self):
        """首次调用应创建全新 state.json。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            args = FakeArgs()
            args.state_dir = str(state_dir)
            state = load_or_init_state(str(state_dir), args)
            assert state["schema_version"] == 1
            assert state["progress"]["phase"] == "init"
            assert state["progress"]["cycle"] == 0
            assert state["config"]["user_request"] == "build a weather CLI"
            assert state["config"]["mode"] == "auto"
            assert (state_dir / "state.json").exists()

    def test_load_existing_state(self):
        """已有 state.json 时应正确加载。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)
            template = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            template["progress"]["phase"] = "part_1_1"
            template["progress"]["cycle"] = 3
            (state_dir / "state.json").write_text(
                json.dumps(template, indent=2), encoding="utf-8"
            )
            state = load_or_init_state(str(state_dir))
            assert state["progress"]["phase"] == "part_1_1"
            assert state["progress"]["cycle"] == 3

    def test_init_sets_mode_from_args(self):
        """--safe 标志应设置 mode=safe。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            args = FakeArgs()
            args.state_dir = str(state_dir)
            args.safe = True
            state = load_or_init_state(str(state_dir), args)
            assert state["config"]["mode"] == "safe"

    def test_recover_from_backup_when_main_corrupt(self):
        """state.json 损坏时应从 .bak 恢复。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state_dir.mkdir(parents=True)

            # 先写入合法 state
            template = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            (state_dir / "state.json").write_text(
                json.dumps(template, indent=2), encoding="utf-8"
            )
            # 创建备份
            backup_state(str(state_dir))

            # 损坏主文件
            (state_dir / "state.json").write_text("not json {{{")

            state = load_or_init_state(str(state_dir))
            assert state["progress"]["phase"] == "init"


class TestAtomicWrite:

    def test_atomic_write_preserves_valid_state(self):
        """原子写入后 state.json 内容应正确。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["progress"]["phase"] = "part_1_1"
            state["progress"]["cycle"] = 2

            atomic_write_state(state, str(state_dir))

            loaded = json.loads((state_dir / "state.json").read_text())
            assert loaded["progress"]["phase"] == "part_1_1"
            assert loaded["progress"]["cycle"] == 2

    def test_atomic_write_creates_backup_on_second_write(self):
        """第二次原子写入后应自动创建 .bak（首次写入不存在旧文件则跳过备份）。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))

            # 第一次写入：无旧文件，不创建 .bak
            atomic_write_state(state, str(state_dir))
            assert not (state_dir / "state.json.bak").exists()

            # 第二次写入：已有旧文件，创建 .bak
            state["progress"]["cycle"] = 1
            atomic_write_state(state, str(state_dir))
            assert (state_dir / "state.json.bak").exists()

    def test_atomic_write_cleans_up_tmp(self):
        """原子写入后 .tmp 文件应已重命名。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            atomic_write_state(state, str(state_dir))
            assert not (state_dir / "state.json.tmp").exists()


class TestLock:

    def test_acquire_and_release_lock(self):
        """获取和释放锁应正常工作。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            assert acquire_lock(str(state_dir))
            assert (state_dir / ".lock").exists()
            release_lock(str(state_dir))
            assert not (state_dir / ".lock").exists()

    def test_lock_not_reentrant(self):
        """锁不应可重入（已有锁时再次获取应失败）。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            assert acquire_lock(str(state_dir))
            assert not acquire_lock(str(state_dir))
            release_lock(str(state_dir))


class TestChecksum:

    def test_compute_checksum_returns_hex(self):
        """计算 checksum 应返回 64 位十六进制字符串。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.txt"
            f.write_text("hello world", encoding="utf-8")
            cs = compute_artifact_checksum(str(f))
            assert len(cs) == 64
            assert all(c in "0123456789abcdef" for c in cs)

    def test_update_artifact_meta(self):
        """更新 artifact meta 应设置 checksum/version/status。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Requirements\n\nTest.", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)

            update_artifact_meta(state, "requirements")
            info = state["artifacts"]["requirements"]
            assert info["checksum"] is not None
            assert len(info["checksum"]) == 64
            assert info["version"] == 1
            assert info["status"] == "generated"
            assert info["generated_in_phase"] is not None

    def test_verify_integrity_no_mismatch(self):
        """无篡改时应返回空列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Test", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)
            update_artifact_meta(state, "requirements")

            mismatches = verify_artifact_integrity(state)
            assert len(mismatches) == 0

    def test_verify_integrity_detects_tampering(self):
        """文件被篡改后应检测到 checksum 不匹配。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Original", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)
            update_artifact_meta(state, "requirements")

            # 篡改文件
            f.write_text("# Tampered!!", encoding="utf-8")

            mismatches = verify_artifact_integrity(state)
            assert len(mismatches) == 1
            assert mismatches[0]["artifact"] == "requirements"


class TestStateChecks:

    def test_is_terminated(self):
        """已终止状态应返回 True。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        assert not is_terminated(state)
        state["termination"]["status"] = "complete"
        assert is_terminated(state)

    def test_is_cycle_exceeded(self):
        """超过最大轮次应返回 True。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["config"]["max_cycles"] = 3
        state["progress"]["cycle"] = 2
        assert not is_cycle_exceeded(state)
        state["progress"]["cycle"] = 3
        assert is_cycle_exceeded(state)

    def test_is_converged(self):
        """收敛条件达成应返回 True。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["config"]["convergence_rounds"] = 2
        state["progress"]["convergence_counter"] = 1
        assert not is_converged(state)
        state["progress"]["convergence_counter"] = 2
        # 活跃 issues 清零
        assert is_converged(state)

    def test_state_exists(self):
        """检测 state.json 是否实际存在。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".hermes" / "loop-hermes"
            assert not state_exists(str(state_dir))
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text("{}")
            assert state_exists(str(state_dir))
