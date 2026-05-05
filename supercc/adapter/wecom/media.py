"""Media file utilities — re-export from feishu media (platform-agnostic)."""
from __future__ import annotations

from supercc.adapter.feishu.media import (
    MIME_TO_EXT,
    MIME_TO_FILE_TYPE,
    file_type_to_mime,
    guess_file_type,
    make_audio_path,
    make_file_path,
    make_image_path,
    mime_to_ext,
    sanitize_filename,
    save_bytes,
)

__all__ = [
    "MIME_TO_EXT",
    "MIME_TO_FILE_TYPE",
    "file_type_to_mime",
    "guess_file_type",
    "make_audio_path",
    "make_file_path",
    "make_image_path",
    "mime_to_ext",
    "sanitize_filename",
    "save_bytes",
]
