"""Media file utilities — path generation, saving, MIME type mapping."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Tuple


# MIME type → 文件扩展名
MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/json": ".json",
    "application/octet-stream": ".bin",
}

# 飞书 file_type → MIME type（参考飞书文档）
FILE_TYPE_TO_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "txt": "text/plain",
    "csv": "text/csv",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "mp4": "video/mp4",
    "avi": "video/x-msvideo",
}


def mime_to_ext(mime_type: str) -> str:
    """MIME type → 文件扩展名（含点号）。未知类型默认 .bin。"""
    return MIME_TO_EXT.get(mime_type, ".bin")


def file_type_to_mime(file_type: str) -> str:
    """飞书 file_type（如 'pdf'）→ MIME type。未知默认 application/octet-stream。"""
    return FILE_TYPE_TO_MIME.get(file_type.lower(), "application/octet-stream")


def sanitize_filename(name: str) -> str:
    """清理文件名，只移除对文件系统有害的字符，保留 Unicode（中文等）。

    结果总长度（含扩展名）控制在 200 字节以内，防止路径超长。
    """
    cleaned = re.sub(r"[/\\:\x00]", "_", name)
    if len(cleaned.encode("utf-8")) > 200:
        # 按 UTF-8 字节数截断，再解码（可能丢尾字符，但不损及 ASCII 核）
        truncated = cleaned.encode("utf-8")[:200].decode("utf-8", errors="ignore")
        # 若截断后首位字符是 '_'（来自危险字符），再裁一次
        return truncated.rstrip("_") or "file"
    return cleaned


def make_image_path(data_dir: str, message_id: str) -> str:
    """生成图片本地存储路径（不含扩展名）。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"img_{ts}_{message_id}"
    images_dir = os.path.join(data_dir, "received_images")
    os.makedirs(images_dir, exist_ok=True)
    return os.path.join(images_dir, filename)


def make_file_path(data_dir: str, message_id: str, original_name: str, file_type: str) -> str:
    """生成文件本地存储路径。优先从原始文件名取扩展名，飞书 file_type 仅作兜底。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = sanitize_filename(original_name) if original_name else "file"
    # If safe_name already has an extension, strip it — we'll add the correct one
    name_without_ext, orig_ext = os.path.splitext(safe_name)
    # Always prefer real extension from original filename (even if unknown to our map).
    # Fall back to file_type mapping only when there's no extension at all.
    if orig_ext:
        ext = orig_ext.lower()
    else:
        ext = mime_to_ext(file_type_to_mime(file_type))
    filename = f"{name_without_ext}_{ts}{ext}"
    files_dir = os.path.join(data_dir, "received_files")
    os.makedirs(files_dir, exist_ok=True)
    return os.path.join(files_dir, filename)


def save_bytes(path: str, data: bytes) -> None:
    """将字节写入文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# 扩展名 → 飞书 file_type
# 飞书支持的 file_type: opus | mp4 | pdf | doc | xls | ppt | stream
# 不在列表里的扩展名统一用 "stream"，保持原扩展名不变
EXT_TO_FILE_TYPE = {
    # 图片（接收用，不用于发送）
    ".png": "png",
    ".jpg": "png",
    ".jpeg": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
    ".svg": "stream",
    # 文档
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
    # 压缩包
    ".zip": "stream",
    ".tar": "stream",
    ".gz": "stream",
    ".7z": "stream",
    ".rar": "stream",
    # 文本 / 数据格式
    ".txt": "stream",
    ".csv": "stream",
    ".md": "stream",
    ".json": "stream",
    ".xml": "stream",
    ".html": "stream",
    ".htm": "stream",
    ".yaml": "stream",
    ".yml": "stream",
    ".toml": "stream",
    ".ini": "stream",
    ".cfg": "stream",
    ".conf": "stream",
    ".env": "stream",
    ".properties": "stream",
    # 脚本 / 编程语言
    ".py": "stream",
    ".go": "stream",
    ".rs": "stream",
    ".java": "stream",
    ".c": "stream",
    ".cpp": "stream",
    ".cc": "stream",
    ".cxx": "stream",
    ".h": "stream",
    ".hpp": "stream",
    ".cs": "stream",
    ".rb": "stream",
    ".php": "stream",
    ".swift": "stream",
    ".kt": "stream",
    ".scala": "stream",
    ".sh": "stream",
    ".bash": "stream",
    ".zsh": "stream",
    ".fish": "stream",
    ".ps1": "stream",
    ".bat": "stream",
    ".psm1": "stream",
    ".lua": "stream",
    ".r": "stream",
    ".R": "stream",
    ".m": "stream",      # Objective-C / Matlab
    ".mm": "stream",     # Objective-C++
    ".jl": "stream",     # Julia
    ".ex": "stream",     # Elixir
    ".exs": "stream",
    ".erl": "stream",    # Erlang
    ".hs": "stream",     # Haskell
    ".ml": "stream",
    ".fs": "stream",     # F#
    ".fsx": "stream",
    ".dart": "stream",
    ".vue": "stream",
    ".jsx": "stream",
    ".tsx": "stream",
    ".css": "stream",
    ".scss": "stream",
    ".sass": "stream",
    ".less": "stream",
    ".sql": "stream",
    ".graphql": "stream",
    ".proto": "stream",
    ".tf": "stream",     # Terraform
    ".dockerfile": "stream",
    ".nginx": "stream",
    # 可执行 / 二进制
    ".so": "stream",
    ".dll": "stream",
    ".exe": "stream",
    ".dylib": "stream",
    ".a": "stream",
    ".o": "stream",
    ".class": "stream",
    ".jar": "stream",
    ".whl": "stream",
    ".gem": "stream",
    ".npm": "stream",
    ".deb": "stream",
    ".rpm": "stream",
    # 数据库
    ".db": "stream",
    ".sqlite": "stream",
    ".sqlite3": "stream",
    ".mdb": "stream",
    # 其他常见格式
    ".epub": "stream",
    ".mobi": "stream",
    ".odt": "stream",
    ".ods": "stream",
    ".odp": "stream",
    ".rtf": "stream",
    ".log": "stream",
    ".key": "stream",    # Keynote
    ".numbers": "stream",
    ".pages": "stream",
    ".fig": "stream",    # Figma
    ".sketch": "stream",
    ".psd": "stream",
    ".ai": "stream",
    ".xd": "stream",
    ".ttf": "stream",
    ".otf": "stream",
    ".woff": "stream",
    ".woff2": "stream",
    ".eot": "stream",
    ".mp3": "stream",
    ".wav": "stream",
    ".flac": "stream",
    ".aac": "stream",
    ".ogg": "stream",
    ".opus": "opus",
    ".mp4": "mp4",
    ".mkv": "stream",
    ".mov": "stream",
    ".avi": "stream",
    ".webm": "stream",
    ".flv": "stream",
    # Makefile / 构建
    "makefile": "stream",
    ".mk": "stream",
    ".cmake": "stream",
    ".bazel": "stream",
    ".bzl": "stream",
    ".buck": "stream",
    ".gradle": "stream",
    ".sbt": "stream",
    # 版本控制 / 配置
    ".gitignore": "stream",
    ".gitattributes": "stream",
    ".dockerignore": "stream",
    ".editorconfig": "stream",
    # 其他
    ".pem": "stream",
    ".crt": "stream",
    ".cer": "stream",
    ".p12": "stream",
    ".pfx": "stream",
    ".ics": "stream",   # iCalendar
    ".vcf": "stream",   # vCard
}


def guess_file_type(ext: str) -> str:
    """扩展名（如 '.pdf'）→ 飞书 file_type（如 'pdf'）。未知默认 'stream'。"""
    return EXT_TO_FILE_TYPE.get(ext.lower(), "stream")


def make_audio_path(data_dir: str, msg_id: str) -> str:
    """Return the base path for saving an inbound audio file (extension added by caller, typically .ogg)."""
    audio_dir = os.path.join(data_dir, "received_audio")
    os.makedirs(audio_dir, exist_ok=True)
    return os.path.join(audio_dir, f"{msg_id}")
