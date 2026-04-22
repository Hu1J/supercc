"""Persist user OAuth tokens in a YAML file, keyed by user_open_id."""
from __future__ import annotations

import os
from typing import Optional

import yaml


class UserTokenStore:
    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return yaml.safe_load(f) or {}

    def _write(self, data: dict) -> None:
        with open(self.path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def load(self, user_open_id: str) -> Optional[dict]:
        """Return token dict for user, or None if not found."""
        data = self._read()
        return data.get(user_open_id)

    def save(self, user_open_id: str, token_info: dict) -> None:
        """Save token for user, merging with existing data."""
        data = self._read()
        data[user_open_id] = token_info
        self._write(data)

    def remove(self, user_open_id: str) -> None:
        """Remove token for user."""
        data = self._read()
        data.pop(user_open_id, None)
        self._write(data)