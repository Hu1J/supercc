"""
DM Pairing System

Code-based approval flow for authorizing new users on messaging platforms.
Instead of static allowlists with user IDs, unknown users receive a one-time
pairing code that the bot owner approves via the CLI.

Security features:
  - 8-char codes from 32-char unambiguous alphabet (no 0/O/1/I)
  - Cryptographic randomness via secrets.choice()
  - 1-hour code expiry
  - Max 3 pending codes per platform
  - Rate limiting: 1 request per user per 10 minutes
  - File permissions: chmod 0600 on all data files

Storage: {project}/.supercc/pairing/
"""

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

# Unambiguous alphabet -- excludes 0/O, 1/I to prevent confusion
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

# Timing constants
CODE_TTL_SECONDS = 3600             # Codes expire after 1 hour
RATE_LIMIT_SECONDS = 600            # 1 request per user per 10 minutes

# Limits
MAX_PENDING_PER_PLATFORM = 3        # Max pending codes per platform


def _get_pairing_dir() -> Path:
    """Get the pairing directory for the current project."""
    from supercc.config import resolve_config_path
    try:
        _, data_dir = resolve_config_path()
    except Exception:
        # Fallback to ~/.supercc if no project config
        return Path.home() / ".supercc" / "pairing"
    return Path(data_dir) / "pairing"


def _secure_write(path: Path, data: str) -> None:
    """Write data to file with restrictive permissions (owner read/write only).

    Uses a temp-file + atomic rename so readers always see either the old
    complete file or the new one — never a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows doesn't support chmod the same way
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PairingStore:
    """
    Manages pairing codes and approved user lists.

    Data files per platform:
      - {platform}-pending.json   : pending pairing requests
      - {platform}-approved.json : approved (paired) users
      - _rate_limits.json        : rate limit tracking
    """

    _instance: Optional["PairingStore"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._pairing_dir = _get_pairing_dir()
        self._pairing_dir.mkdir(parents=True, exist_ok=True)
        # Protects all read-modify-write cycles
        self._lock = threading.RLock()

    def _pending_path(self, platform: str) -> Path:
        return self._pairing_dir / f"{platform}-pending.json"

    def _approved_path(self, platform: str) -> Path:
        return self._pairing_dir / f"{platform}-approved.json"

    def _rate_limit_path(self) -> Path:
        return self._pairing_dir / "_rate_limits.json"

    def _load_json(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_json(self, path: Path, data: dict) -> None:
        _secure_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    # ----- Approved users -----

    def is_approved(self, platform: str, user_id: str) -> bool:
        """Check if a user is approved (paired) on a platform."""
        approved = self._load_json(self._approved_path(platform))
        return user_id in approved

    def list_approved(self, platform: str = None) -> list:
        """List approved users, optionally filtered by platform."""
        results = []
        platforms = [platform] if platform else self._all_platforms("approved")
        for p in platforms:
            approved = self._load_json(self._approved_path(p))
            for uid, info in approved.items():
                results.append({"platform": p, "user_id": uid, **info})
        return results

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        """Add a user to the approved list. Must be called under self._lock."""
        approved = self._load_json(self._approved_path(platform))
        approved[user_id] = {
            "user_name": user_name,
            "approved_at": time.time(),
        }
        self._save_json(self._approved_path(platform), approved)

    def revoke(self, platform: str, user_id: str) -> bool:
        """Remove a user from the approved list. Returns True if found."""
        path = self._approved_path(platform)
        with self._lock:
            approved = self._load_json(path)
            if user_id in approved:
                del approved[user_id]
                self._save_json(path, approved)
                return True
        return False

    # ----- Pending codes -----

    def generate_code(
        self, platform: str, user_id: str, user_name: str = ""
    ) -> Optional[str]:
        """
        Generate a pairing code for a new user.

        Returns the code string, or None if:
          - User is rate-limited (too recent request)
          - Max pending codes reached for this platform
        """
        with self._lock:
            self._cleanup_expired(platform)

            # Check rate limit for this specific user
            if self._is_rate_limited(platform, user_id):
                return None

            # Check max pending
            pending = self._load_json(self._pending_path(platform))
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None

            # Generate cryptographically random code
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))

            # Store pending request
            pending[code] = {
                "user_id": user_id,
                "user_name": user_name,
                "created_at": time.time(),
            }
            self._save_json(self._pending_path(platform), pending)

            # Record rate limit
            self._record_rate_limit(platform, user_id)

            return code

    def approve_code(self, platform: str, code: str) -> Optional[dict]:
        """
        Approve a pairing code. Adds the user to the approved list.

        Returns {user_id, user_name} on success, None if code is invalid/expired.
        """
        with self._lock:
            self._cleanup_expired(platform)
            code = code.upper().strip()

            pending = self._load_json(self._pending_path(platform))
            if code not in pending:
                return None

            entry = pending.pop(code)
            self._save_json(self._pending_path(platform), pending)

            # Add to approved list
            self._approve_user(platform, entry["user_id"], entry.get("user_name", ""))

            return {
                "user_id": entry["user_id"],
                "user_name": entry.get("user_name", ""),
                "platform": platform,
            }

    def approve_code_global(self, code: str) -> Optional[dict]:
        """
        Approve a pairing code across all platforms.

        Searches all platform pending files for the code. Returns
        {user_id, user_name, platform} on success, None if not found.
        """
        code = code.upper().strip()
        with self._lock:
            for platform in self._all_platforms("pending"):
                self._cleanup_expired(platform)
                pending = self._load_json(self._pending_path(platform))
                if code in pending:
                    entry = pending.pop(code)
                    self._save_json(self._pending_path(platform), pending)
                    self._approve_user(platform, entry["user_id"], entry.get("user_name", ""))
                    return {
                        "user_id": entry["user_id"],
                        "user_name": entry.get("user_name", ""),
                        "platform": platform,
                    }
            return None

    def list_pending(self, platform: str = None) -> list:
        """List pending pairing requests, optionally filtered by platform."""
        results = []
        platforms = [platform] if platform else self._all_platforms("pending")
        for p in platforms:
            self._cleanup_expired(p)
            pending = self._load_json(self._pending_path(p))
            for code, info in pending.items():
                age_min = int((time.time() - info["created_at"]) / 60)
                results.append({
                    "platform": p,
                    "code": code,
                    "user_id": info["user_id"],
                    "user_name": info.get("user_name", ""),
                    "age_minutes": age_min,
                })
        return results

    def clear_pending(self, platform: str = None) -> int:
        """Clear all pending requests. Returns count removed."""
        with self._lock:
            count = 0
            platforms = [platform] if platform else self._all_platforms("pending")
            for p in platforms:
                pending = self._load_json(self._pending_path(p))
                count += len(pending)
                self._save_json(self._pending_path(p), {})
        return count

    # ----- Rate limiting -----

    def _is_rate_limited(self, platform: str, user_id: str) -> bool:
        """Check if a user has requested a code too recently."""
        limits = self._load_json(self._rate_limit_path())
        key = f"{platform}:{user_id}"
        last_request = limits.get(key, 0)
        return (time.time() - last_request) < RATE_LIMIT_SECONDS

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        """Record the time of a pairing request for rate limiting."""
        limits = self._load_json(self._rate_limit_path())
        key = f"{platform}:{user_id}"
        limits[key] = time.time()
        self._save_json(self._rate_limit_path(), limits)

    # ----- Cleanup -----

    def _cleanup_expired(self, platform: str) -> None:
        """Remove expired pending codes."""
        path = self._pending_path(platform)
        pending = self._load_json(path)
        now = time.time()
        expired = [
            code for code, info in pending.items()
            if (now - info["created_at"]) > CODE_TTL_SECONDS
        ]
        if expired:
            for code in expired:
                del pending[code]
            self._save_json(path, pending)

    def _all_platforms(self, suffix: str) -> list:
        """List all platforms that have data files of a given suffix."""
        platforms = []
        if not self._pairing_dir.exists():
            return platforms
        for f in self._pairing_dir.iterdir():
            if f.name.endswith(f"-{suffix}.json"):
                platform = f.name.replace(f"-{suffix}.json", "")
                if not platform.startswith("_"):
                    platforms.append(platform)
        return platforms


# ----- CLI helper -----

def get_pairing_store() -> PairingStore:
    """Get the singleton PairingStore instance."""
    return PairingStore()
