import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from supercc.security.auth import Authenticator, AuthResult


def test_authorized_user():
    auth = Authenticator(allowed_users=["ou_123", "ou_456"])
    result = auth.authenticate("ou_123")
    assert result.authorized is True
    assert result.user_id == "ou_123"


def test_unauthorized_user():
    auth = Authenticator(allowed_users=["ou_123"])
    result = auth.authenticate("ou_789")
    assert result.authorized is False
    assert "not in allowed list" in result.reason


def test_empty_allowlist():
    auth = Authenticator(allowed_users=[])
    result = auth.authenticate("ou_any")
    assert result.authorized is False
