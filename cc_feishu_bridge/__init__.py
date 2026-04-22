try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("cc-feishu-bridge")
except Exception:
    __version__ = "dev"
