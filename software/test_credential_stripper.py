"""Unit tests for AdvancedCredentialStripper."""

import pytest
from source.server.credential_stripper import AdvancedCredentialStripper


def test_credential_stripper_masks_secrets():
    stripper = AdvancedCredentialStripper()
    sample = "Use bearer Bearer abcdefghijklmnopqrstuvwxyz123456 and key sk-proj-TESTKEY1234567890TESTKEY12345678 and akia AKIATESTKEY123456789."
    cleaned = stripper.strip(sample)
    assert "[REDACTED:BEARER_TOKEN]" in cleaned
    assert "[REDACTED:API_KEY]" in cleaned
    assert "[REDACTED:AWS_KEY]" in cleaned
    assert "sk-proj-" not in cleaned
    assert "Bearer abcdef" not in cleaned
