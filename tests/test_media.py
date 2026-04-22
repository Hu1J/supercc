import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.feishu.media import (
    sanitize_filename,
    mime_to_ext,
    file_type_to_mime,
    make_image_path,
    make_file_path,
    save_bytes,
)
import os
import tempfile


class TestSanitizeFilename:
    def test_keeps_unicode(self):
        # 中文等 Unicode 字符全部保留
        assert sanitize_filename("我的文件") == "我的文件"

    def test_replaces_slashes(self):
        # 路径分隔符替换为下划线
        assert sanitize_filename("doc/v2/test") == "doc_v2_test"

    def test_keeps_underscores_and_dots(self):
        assert sanitize_filename("my_file.v2.pdf") == "my_file.v2.pdf"

    def test_replaces_only_dangerous_chars(self):
        # 只移除文件系统危险字符：/ \ : NUL
        assert sanitize_filename("file<>:\"|?*.txt") == "file<>_\"|?*.txt"
        assert sanitize_filename("file/name:here") == "file_name_here"

    def test_length_limit_200_bytes(self):
        # 超过 200 UTF-8 字节时截断
        long_name = "中" * 100  # 每个汉字 3 字节，100 个 = 300 字节 > 200
        result = sanitize_filename(long_name)
        assert len(result.encode("utf-8")) <= 200
        assert result  # 不为空

    def test_length_limit_preserves_ending_if_possible(self):
        # 截断时优先保留有意义的字符（尾随 _ 会被 rstrip）
        result = sanitize_filename("a" * 250)
        assert len(result.encode("utf-8")) <= 200
        assert result == "a" * 200  # 全 ASCII，精确截断

    def test_trim_trailing_underscores(self):
        # 截断后尾部 _ 来自危险字符替换，应被裁掉
        result = sanitize_filename("file/" + "x" * 200)
        assert not result.endswith("_")
        assert len(result.encode("utf-8")) <= 200

    def test_fallback_to_file_on_empty(self):
        # 极端情况：截断后全空或只剩 _，返回 "file"
        result = sanitize_filename("_" * 300)  # 全 _ 截断后 rstrip 成空
        assert result == "file"

    def test_short_underscore_name_not_truncated(self):
        # 短名称（< 200 字节）直接返回，不截断
        result = sanitize_filename("/" * 100)
        assert result == "_" * 100
        assert len(result) == 100


class TestMimeToExt:
    def test_png(self):
        assert mime_to_ext("image/png") == ".png"

    def test_jpeg(self):
        assert mime_to_ext("image/jpeg") == ".jpg"

    def test_unknown_returns_bin(self):
        assert mime_to_ext("application/x-unknown") == ".bin"


class TestFileTypeToMime:
    def test_pdf(self):
        assert file_type_to_mime("pdf") == "application/pdf"

    def test_docx(self):
        assert file_type_to_mime("docx") == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def test_unknown(self):
        assert file_type_to_mime("unknowntype") == "application/octet-stream"


class TestMakeImagePath:
    def test_returns_path_in_received_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_image_path(tmpdir, "om_abc12345xyz")
            assert "received_images" in path
            assert path.startswith(tmpdir)
            assert os.path.exists(os.path.dirname(path))

    def test_path_contains_message_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_image_path(tmpdir, "om_abc12345xyz")
            assert "abc12345" in path


class TestMakeFilePath:
    def test_returns_path_in_received_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "report", "pdf")
            assert "received_files" in path
            assert path.startswith(tmpdir)
            assert os.path.exists(os.path.dirname(path))

    def test_includes_original_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "document", "pdf")
            assert "document" in path

    def test_unknown_file_type_gets_bin_ext(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 无扩展名的文件走 MIME 兜底
            path = make_file_path(tmpdir, "om_abc12345", "data", "unknowntype")
            assert path.endswith(".bin")

    def test_unknown_ext_preserved(self):
        """有扩展名但不在映射表里，扩展名仍然保留（不走 .bin）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "script.py", "bin")
            assert path.endswith(".py")

    def test_arbitrary_ext_preserved(self):
        """任意扩展名（.xyz/.tmp）都保留，不变成 .bin"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "backup.xyz", "bin")
            assert path.endswith(".xyz")

    def test_composite_ext(self):
        """复合扩展名（.tar.gz）保留最后一个扩展名"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "archive.tar.gz", "bin")
            # splitext("archive.tar.gz") -> ("archive.tar", ".gz")
            assert path.endswith(".gz")


class TestSaveBytes:
    def test_writes_and_reads_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.bin")
            save_bytes(path, b"\x89PNG\r\n\x1a\n")
            with open(path, "rb") as f:
                assert f.read() == b"\x89PNG\r\n\x1a\n"

    def test_creates_intermediate_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = os.path.join(tmpdir, "a", "b", "c")
            path = os.path.join(nested, "test.bin")
            save_bytes(path, b"data")
            assert os.path.exists(path)