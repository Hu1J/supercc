try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("supercc")
except Exception:
    __version__ = "dev"
