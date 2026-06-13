# -*- coding: utf-8 -*-
"""测试: checksum.py —— SHA-256 三层校验协议。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop_hermes.checksum import (
    compute_checksum,
    compute_checksum_from_content,
    update_checksum_in_state,
    update_all_artifacts_in_state,
    verify_artifact_integrity,
    is_artifact_intact,
)
from loop_hermes.state_machine import DEFAULT_STATE_TEMPLATE


class TestComputeChecksum:

    def test_returns_hex_string(self):
        """SHA-256 应返回 64 位十六进制字符串。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.txt"
            f.write_text("hello world", encoding="utf-8")
            cs = compute_checksum(str(f))
            assert len(cs) == 64
            assert all(c in "0123456789abcdef" for c in cs)

    def test_same_content_same_hash(self):
        """相同内容产生相同哈希。"""
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "a.txt"
            f2 = Path(tmp) / "b.txt"
            f1.write_text("same content", encoding="utf-8")
            f2.write_text("same content", encoding="utf-8")
            assert compute_checksum(str(f1)) == compute_checksum(str(f2))

    def test_different_content_different_hash(self):
        """不同内容产生不同哈希。"""
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "a.txt"
            f2 = Path(tmp) / "b.txt"
            f1.write_text("content A", encoding="utf-8")
            f2.write_text("content B", encoding="utf-8")
            assert compute_checksum(str(f1)) != compute_checksum(str(f2))

    def test_raises_for_missing_file(self):
        """文件不存在应抛出 FileNotFoundError。"""
        try:
            compute_checksum("/nonexistent/file.txt")
            assert False, "Should have raised"
        except FileNotFoundError:
            pass

    def test_compute_from_content(self):
        """从字符串内容计算 checksum。"""
        cs = compute_checksum_from_content("hello")
        assert len(cs) == 64
        # 应与文件写入后的 checksum 一致
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.txt"
            f.write_text("hello", encoding="utf-8")
            assert cs == compute_checksum(str(f))


class TestUpdateChecksumInState:

    def test_sets_checksum_and_version(self):
        """更新 artifact meta 应设置 checksum/version/status。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Requirements\n\nTest content.", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)

            update_checksum_in_state(state, "requirements")
            info = state["artifacts"]["requirements"]
            assert info["checksum"] is not None
            assert len(info["checksum"]) == 64
            assert info["version"] == 1
            assert info["status"] == "generated"

    def test_version_increments_on_subsequent_updates(self):
        """第二次更新应递增 version。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "02-direction.md"
            f.write_text("v1", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["direction"]["path"] = str(f)
            update_checksum_in_state(state, "direction")
            assert state["artifacts"]["direction"]["version"] == 1

            f.write_text("v2", encoding="utf-8")
            update_checksum_in_state(state, "direction")
            assert state["artifacts"]["direction"]["version"] == 2
            assert state["artifacts"]["direction"]["status"] == "updated"

    def test_raises_for_unknown_key(self):
        """未知 artifact 键应抛出 KeyError。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        try:
            update_checksum_in_state(state, "nonexistent_key")
            assert False, "Should have raised"
        except KeyError:
            pass


class TestVerifyIntegrity:

    def test_no_mismatch_when_intact(self):
        """无篡改时应返回空列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Clean content", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)
            update_checksum_in_state(state, "requirements")

            mismatches = verify_artifact_integrity(state)
            assert len(mismatches) == 0

    def test_detects_tampering(self):
        """文件被篡改后应检测到 checksum 不匹配。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            f = artifacts_dir / "01-requirements.md"
            f.write_text("# Original", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(f)
            update_checksum_in_state(state, "requirements")

            # 篡改文件
            f.write_text("# Tampered!!", encoding="utf-8")

            mismatches = verify_artifact_integrity(state)
            assert len(mismatches) == 1
            assert mismatches[0]["artifact"] == "requirements"

    def test_skips_not_generated_artifacts(self):
        """跳过未生成的 artifact。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        # 全部为 not_generated，应返回空
        mismatches = verify_artifact_integrity(state)
        assert len(mismatches) == 0


class TestIsArtifactIntact:

    def test_returns_true_for_missing_checksum(self):
        """无 checksum 记录的 artifact 视为通过。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        assert is_artifact_intact(state, "requirements")

    def test_returns_false_for_missing_file(self):
        """文件已删除应返回 False。"""
        state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
        state["artifacts"]["requirements"]["path"] = "/nonexistent/file.md"
        state["artifacts"]["requirements"]["checksum"] = "abc123"
        state["artifacts"]["requirements"]["status"] = "generated"
        assert not is_artifact_intact(state, "requirements")


class TestUpdateAllArtifacts:

    def test_batch_updates_existing_files(self):
        """批量更新应处理所有存在的 artifact 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp) / "artifacts"
            artifacts_dir.mkdir(parents=True)
            r = artifacts_dir / "01-requirements.md"
            s = artifacts_dir / "03-solution.md"
            r.write_text("# Req", encoding="utf-8")
            s.write_text("# Sol", encoding="utf-8")

            state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
            state["artifacts"]["requirements"]["path"] = str(r)
            state["artifacts"]["solution"]["path"] = str(s)

            updated = update_all_artifacts_in_state(state)
            assert "requirements" in updated
            assert "solution" in updated
            assert state["artifacts"]["requirements"]["checksum"] is not None
            assert state["artifacts"]["solution"]["checksum"] is not None
