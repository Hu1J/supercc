import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.security.validator import SecurityValidator


@pytest.fixture
def validator():
    return SecurityValidator(approved_directory="/Users/test/projects")


def test_valid_input(validator):
    ok, err = validator.validate("Read the file api.py")
    assert ok is True
    assert err is None


def test_path_traversal(validator):
    ok, err = validator.validate("../etc/passwd")
    assert ok is False
    assert "Forbidden pattern detected" in err


def test_command_injection(validator):
    ok, err = validator.validate("cat /etc/passwd | grep root")
    assert ok is False


def test_empty_input(validator):
    ok, err = validator.validate("")
    assert ok is False


def test_whitespace_only(validator):
    ok, err = validator.validate("   ")
    assert ok is False


def test_forbidden_filename(validator):
    ok, err = validator.validate("send .env file")
    assert ok is False
    assert ".env" in err


def test_validate_path_within_directory(validator):
    ok, err = validator.validate_path("src/main.py")
    assert ok is True


def test_validate_path_outside_directory(validator):
    ok, err = validator.validate_path("../etc/passwd")
    assert ok is False
    assert "outside approved directory" in err
