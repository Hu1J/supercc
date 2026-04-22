import os
import tempfile
from pathlib import Path
import pytest

def test_resolve_config_path_creates_cc_dir(monkeypatch):
    """resolve_config_path creates .cc-feishu-bridge/ in cwd if not exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = str(Path(tmpdir).resolve())
        monkeypatch.chdir(tmpdir)
        from cc_feishu_bridge.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == f"{tmpdir}/.cc-feishu-bridge/config.yaml"
        assert data_dir == f"{tmpdir}/.cc-feishu-bridge"
        assert Path(cfg).exists()

def test_resolve_config_path_resumes_existing(monkeypatch):
    """If .cc-feishu-bridge/config.yaml exists, returns it (auto-resume)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = str(Path(tmpdir).resolve())
        cc_dir = Path(tmpdir) / ".cc-feishu-bridge"
        cc_dir.mkdir()
        cfg_file = cc_dir / "config.yaml"
        cfg_file.write_text("feishu:\n  app_id: test\n")

        monkeypatch.chdir(tmpdir)
        from cc_feishu_bridge.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == str(cfg_file)
        assert data_dir == str(cc_dir)