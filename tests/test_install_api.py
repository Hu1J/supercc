import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from cc_feishu_bridge.install.api import FeishuInstallAPI, AppRegistrationResult, BeginResult


@pytest.fixture
def api():
    return FeishuInstallAPI()


def test_begin_result_dataclass():
    result = BeginResult(
        device_code="device123",
        verification_uri="https://example.com/verify",
        verification_uri_complete="https://example.com/verify?code=user123",
        expires_in=600,
        interval=5,
    )
    assert result.device_code == "device123"
    assert result.expires_in == 600


def test_app_registration_result_dataclass():
    result = AppRegistrationResult(
        app_id="cli_xxx",
        app_secret="secret",
        user_open_id="ou_123",
        domain="feishu",
    )
    assert result.app_id == "cli_xxx"
    assert result.domain == "feishu"
