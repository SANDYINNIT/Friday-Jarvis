"""Headless checks for the optional webview chat boundary."""

from source.server.status_ui import ChatBuffer, StatusBus, WebviewBridge


buffer = ChatBuffer(limit=3)
for index in range(5):
    buffer.append("user", f"message {index}")
assert [item["content"] for item in buffer.snapshot()] == ["message 2", "message 3", "message 4"]
assert set(buffer.snapshot()[0]) == {"role", "content", "timestamp", "status"}

# "tool" is a legitimate feed role (tool execution shows in the live feed).
buffer.append("tool", "RUNNING: python code")
assert buffer.snapshot()[-1]["role"] == "tool"


class FakeInterpreter:
    pass


bus = StatusBus()
bridge = WebviewBridge(bus, FakeInterpreter())
unavailable = bridge.send_text("hello")
assert unavailable["accepted"] is False
assert unavailable["status"] == "unavailable"

calls = []
bridge._interpreter.submit_text = lambda text: calls.append(text) or {"accepted": True, "status": "queued"}
assert bridge.send_text("  hello  ") == {"accepted": True, "status": "queued"}
assert calls == ["  hello  "]
bus.chat.append("user", "hello", status="queued")
bus.chat.append("assistant", "Hi", status="streaming")
serialized = bridge.get_chat()
assert serialized[-1]["role"] == "assistant"
assert serialized[-1]["content"] == "Hi"
assert bridge.get_status()["state"] == "idle"
print("PASS: chat buffer bounds, readiness, roles, and bridge serialization")
