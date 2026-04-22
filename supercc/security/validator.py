"""Input security validation — path traversal, command injection."""
from __future__ import annotations

import re
from pathlib import Path


# Dangerous patterns
FORBIDDEN_PATTERNS = [
    r"\.\.",           # path traversal
    r"[;|`$<>]",      # command injection chars
    r"&&",             # command chaining
    r"\|\|",           # pipe chaining
    r"^\s*$",          # whitespace only (handled separately)
]

FORBIDDEN_FILENAMES = [
    ".env", ".ssh", "id_rsa", ".pem", ".key",
    ".git", ".bashrc", ".profile",
]

FORBIDDEN_EXTENSIONS = [
    ".exe", ".dll", ".bat", ".cmd", ".sh", ".ps1",
]


class SecurityValidator:
    def __init__(self, approved_directory: str):
        self.approved_directory = Path(approved_directory).resolve()

    def validate(self, user_input: str) -> tuple[bool, str | None]:
        """Validate user input. Returns (ok, error_message)."""
        # Empty/whitespace
        if not user_input or not user_input.strip():
            return False, "Input is empty"

        # Pattern checks
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, user_input):
                return False, f"Forbidden pattern detected: {pattern}"

        # Filename checks
        words = user_input.split()
        for word in words:
            path = Path(word)
            if path.name in FORBIDDEN_FILENAMES:
                return False, f"Forbidden filename: {path.name}"
            if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
                return False, f"Forbidden extension: {path.suffix}"

        return True, None

    def validate_path(self, path: str) -> tuple[bool, str | None]:
        """Validate a path is within approved_directory."""
        try:
            resolved = (self.approved_directory / path).resolve()
            if not str(resolved).startswith(str(self.approved_directory)):
                return False, "Path outside approved directory"
            return True, None
        except Exception as e:
            return False, f"Invalid path: {e}"
