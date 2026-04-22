import os
import tempfile
from pathlib import Path
import pytest

def test_resolve_config_path_creates_dirs(monkeypatch):
    """resolve_config_path creates .supercc/ in cwd and ~/.supercc/ globally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = str(Path(tmpdir).resolve())
        monkeypatch.chdir(tmpdir)
        monkeypatch.setenv("HOME", tmpdir)  # fake home so ~/.supercc/ is inside tmpdir
        from supercc.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == f"{tmpdir}/.supercc/config.yaml"
        assert data_dir == f"{tmpdir}/.supercc"
        assert Path(cfg).exists()
        assert Path(data_dir).exists()

def test_resolve_config_path_resumes_existing(monkeypatch):
    """If .supercc/config.yaml exists, returns it (auto-resume)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = str(Path(tmpdir).resolve())
        monkeypatch.chdir(tmpdir)
        monkeypatch.setenv("HOME", tmpdir)
        cfg_dir = Path(tmpdir) / ".supercc"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.yaml"
        cfg_file.write_text("feishu:\n  app_id: test\n")

        from supercc.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == str(cfg_file)
        assert data_dir == f"{tmpdir}/.supercc"
