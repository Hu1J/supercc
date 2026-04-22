try:
    from importlib.metadata import version as _get_version

    try:
        __version__ = _get_version("supercc")
    except Exception:
        __version__ = _get_version("pysupercc")
except Exception:
    __version__ = "dev"
