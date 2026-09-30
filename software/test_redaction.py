"""WP2: auto-redaction unit tests."""
import os

from source.server import memory
from source.server.redaction import redact, redact_secret_values, redaction_enabled


def test_email_phone_ip_paths_are_masked():
    assert "example.com" not in redact("reach me at john.doe@example.com now")
    assert "<EMAIL>" in redact("reach me at john.doe@example.com now")
    assert redact("call 555-123-4567 later") == "call <PHONE> later"
    assert redact("server is at 192.168.1.100 port 80").count("<IP>") == 1
    assert redact(r"file is at C:\\Users\\testuser\\secret.txt ok") == "file is at <PATH> ok"
    assert redact("saved to /home/testuser/docs/plan.md ta") == "saved to <PATH> ta"


def test_keys_and_tokens_are_masked():
    assert "<KEY>" in redact("key sk-abcdefghij1234567890AB used")
    assert "<TOKEN>" in redact("now abc def a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8 end")


def test_plain_text_is_unchanged():
    value = "friday what time is it please"
    assert redact(value) == value


def test_redaction_can_be_disabled():
    os.environ["FRIDAY_REDACTION"] = "0"
    try:
        assert redact("testuser@example.com") == "testuser@example.com"
        assert redaction_enabled() is False
    finally:
        del os.environ["FRIDAY_REDACTION"]
    assert redaction_enabled() is True


def test_secret_values_nested_structure():
    payload = {"args": {"email": "a@b.com"}, "nested": [{"token": "abc123"}], "count": 3}
    cleaned = redact_secret_values(payload)
    assert cleaned["args"]["email"] == "<EMAIL>"
    assert cleaned["nested"][0]["token"] == "<TOKEN>"
    assert cleaned["count"] == 3


def test_secret_key_names_are_fully_masked():
    cleaned = redact_secret_values({"authorization": "Bearer abcdefghijklmnop", "normal": "keep"})
    assert cleaned["authorization"] == "<TOKEN>"
    assert cleaned["normal"] == "keep"


def test_memory_store_redacts_on_write(tmp_path):
    db = tmp_path / "m.db"
    store = memory.MemoryStore(db_path=db)
    record = store.remember(key="email", content="my email is a.b@example.com", source="voice")
    assert "<EMAIL>" in record["content"]
    assert "a.b@example.com" not in record["content"]