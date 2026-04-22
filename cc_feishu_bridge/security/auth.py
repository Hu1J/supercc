"""Whitelist-based authentication for Feishu users."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class AuthResult:
    authorized: bool
    user_id: str
    reason: str | None = None


class Authenticator:
    def __init__(self, allowed_users: List[str]):
        self.allowed_users = set(allowed_users)
        self._logger = logger

    def authenticate(self, user_open_id: str) -> AuthResult:
        """Check if user is in the whitelist."""
        if user_open_id in self.allowed_users:
            return AuthResult(authorized=True, user_id=user_open_id)
        self._logger.warning(f"Unauthorized user attempted access: {user_open_id}")
        return AuthResult(
            authorized=False,
            user_id=user_open_id,
            reason="User not in allowed list",
        )
