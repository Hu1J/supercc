"""Tests for banner module."""
import os
import tempfile
from supercc.banner import print_banner, write_log_banner


class TestPrintBanner:
    """Test print_banner behavior."""

    def test_print_banner_does_not_raise(self):
        """print_banner runs without raising."""
        # Should not raise
        print_banner("0.1.4")


class TestWriteLogBanner:
    """Test write_log_banner behavior."""

    def test_writes_banner_when_file_empty(self):
        """An empty file gets the banner written to it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test.log")
            # File exists but is empty
            open(log_path, "w").close()
            write_log_banner(log_path, "0.1.4")
            content = open(log_path).read()
            assert "SuperCC" in content
            assert "0.1.4" in content
            assert "started at" in content

    def test_does_not_append_when_file_has_content(self):
        """A non-empty file is left untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test.log")
            original = "2026-04-03 12:00:00 INFO hello world\n"
            open(log_path, "w").write(original)
            write_log_banner(log_path, "0.1.4")
            content = open(log_path).read()
            assert content == original

    def test_creates_parent_directory(self):
        """Parent dir is created if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "subdir", "deep", "test.log")
            assert not os.path.exists(os.path.dirname(log_path))
            write_log_banner(log_path, "0.1.4")
            assert os.path.exists(log_path)
            assert "SuperCC" in open(log_path).read()