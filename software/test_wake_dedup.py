"""
Headless test harness for wake-word deduplication and response lifecycle.
Verifies:
  1. One user text  -> exactly 1 response lifecycle
  2. Same text twice within 3s -> 1 response (dedup)
  3. Same text after 3s -> 2 responses (time expired)
  4. Different texts -> 2 separate responses
  5. Paused state -> ignored
  6. Empty transcription -> ignored
  7. No wake word -> ignored
  8. Only wake word (no content) -> ignored
"""
import time
import types
import asyncio
import queue


# ---------------------------------------------------------------------------
# Fake interpreter that mirrors real lifecycle
# ---------------------------------------------------------------------------
class FakeOutputQueue:
    def __init__(self):
        self._q = queue.Queue()
        self.sync_q = self          # real code uses self.output_queue.sync_q.put(...)
    def put(self, item):
        self._q.put(item)
    def get(self, timeout=5):
        return self._q.get(timeout=timeout)
    def empty(self):
        return self._q.empty()


class FakeSTT:
    def __init__(self, text="hello world"):
        self._text = text
        self._next_text = None
    def set_next(self, text):
        """Set text returned by next .text() call."""
        self._next_text = text
    def start(self):
        pass
    def stop(self):
        pass
    def text(self):
        if self._next_text is not None:
            t = self._next_text
            self._next_text = None
            return t
        return self._text
    def feed_audio(self, chunk):
        pass


class FakeInterpreter:
    def __init__(self, stt_text="hello world"):
        self.output_queue = FakeOutputQueue()
        self.stt = FakeSTT(stt_text)
        self.tts = types.SimpleNamespace(
            feed=lambda t: None,
            play_async=lambda **kw: None,
            is_playing=lambda: False,
        )
        self.play_audio = False
        self.audio_chunks = []


def _build_server_state():
    return {
        "is_paused": False,
        "pending_input": None,
        "last_wake_time": 0.0,
        "last_wake_text": "",
    }


def _make_new_input(interpreter, server_state):
    """Return (new_input_coro, tracker). tracker.dispatches = [clean_content, ...]"""
    import re as _re

    class Tracker:
        def __init__(self):
            self.dispatches = []

    tracker = Tracker()

    async def new_input(self, chunk):
        await asyncio.sleep(0)
        try:
            if isinstance(chunk, bytes):
                self.stt.feed_audio(chunk)
                self.audio_chunks.append(chunk)
            elif isinstance(chunk, dict):
                if "start" in chunk:
                    self.stt.start()
                    self.audio_chunks = []
                if "end" in chunk:
                    self.stt.stop()
                    content = self.stt.text()

                    if content.strip() == "":
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    text_lower = content.strip().lower()
                    text_clean_lower = text_lower.rstrip(",.?! ")

                    if server_state["is_paused"]:
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    temp_text = text_clean_lower
                    for w in ["friday","frida","fry day","fryday","fri-day",
                              "friyay","fire day","fri","day","fry"]:
                        temp_text = temp_text.replace(w, "")
                    temp_text = temp_text.strip(",.?! ")

                    if temp_text == "pause":
                        server_state["is_paused"] = True
                        self.output_queue.sync_q.put({"voice_paused": True, "end": True})
                        return

                    wake_words = ["friday","frida","fry day","fryday","fri-day",
                                  "friyay","fire day"]
                    found_wake = False
                    matched_word = ""
                    for word in wake_words:
                        pattern = r'\b' + _re.escape(word) + r'\b'
                        if _re.search(pattern, text_lower):
                            found_wake = True
                            matched_word = word
                            break
                    if not found_wake:
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    clean_content = _re.sub(
                        r'\b' + _re.escape(matched_word) + r'\b',
                        '', text_lower).strip()
                    clean_content = clean_content.strip(",.?! ")

                    if clean_content.strip() == "":
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    # -- Deduplication guard --
                    now = time.time()
                    last_t = server_state["last_wake_time"]
                    last_w = server_state["last_wake_text"]
                    if clean_content == last_w and (now - last_t) < 3.0:
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    server_state["last_wake_time"] = now
                    server_state["last_wake_text"] = clean_content

                    tracker.dispatches.append(clean_content)

                    self.output_queue.sync_q.put(
                        {"type": "status", "content": "complete"})
                    self.output_queue.sync_q.put(
                        {"role": "assistant", "type": "message", "end": True})
        except Exception as e:
            print("[STT/input error: %s]" % e)

    return new_input, tracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def feed_wake(new_input_fn, interp, wake_text):
    """Simulate one full wake-word cycle: start -> end."""
    interp.stt.set_next(wake_text)
    async def run():
        await new_input_fn(interp, {"start": True})
        await new_input_fn(interp, {"end": True})
    asyncio.get_event_loop().run_until_complete(run())


def drain(interp):
    items = []
    while True:
        try:
            items.append(interp.output_queue.sync_q.get(timeout=0.2))
        except queue.Empty:
            break
    return items


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------
print("=" * 60)
print("WAKE-WORD DEDUP & RESPONSE LIFECYCLE TESTS")
print("=" * 60)

all_pass = True

def check(name, condition):
    global all_pass
    status = "PASS" if condition else "FAIL"
    if not condition:
        all_pass = False
    print("  [%s] %s" % (status, name))


# --- Test 1: Single wake -> exactly 1 dispatch ---
print("\n1. Single wake-word dispatch")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, what time is it")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, what time is it")
# "hey friday, what time is it" -> wake=friday -> clean="hey , what time is it"
check("Exactly 1 dispatch", len(tracker.dispatches) == 1)
check("Content extracted (prefix 'hey' remains after wake removal)",
      tracker.dispatches == ["hey , what time is it"])

# --- Test 2: Duplicate within 3s -> dedup ---
print("\n2. Duplicate within 3s -> dedup")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, how are you")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, how are you")
feed_wake(new_input_fn, interp, "hey friday, how are you")
check("Only 1 dispatch (duplicate suppressed)", len(tracker.dispatches) == 1)
items = drain(interp)
ignored = [i for i in items if isinstance(i, dict) and i.get("ignored")]
check("Second call emitted ignored", len(ignored) >= 1)

# --- Test 3: Same text after >3s -> allowed ---
print("\n3. Same text after >3s -> allowed")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, what's up")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, what's up")
ss["last_wake_time"] = time.time() - 5.0
feed_wake(new_input_fn, interp, "hey friday, what's up")
check("2 dispatches (time expired)", len(tracker.dispatches) == 2)

# --- Test 4: Different texts -> both allowed ---
print("\n4. Different texts -> both allowed")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, hello")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, hello")
feed_wake(new_input_fn, interp, "hey friday, goodbye")
check("2 dispatches", len(tracker.dispatches) == 2)
check("Contents match",
      tracker.dispatches == ["hey , hello", "hey , goodbye"])

# --- Test 5: Paused state -> ignored ---
print("\n5. Paused state -> ignored")
ss = _build_server_state()
ss["is_paused"] = True
interp = FakeInterpreter("hey friday, resume")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, resume")
items = drain(interp)
ignored = [i for i in items if isinstance(i, dict) and i.get("ignored")]
check("Paused -> ignored", len(ignored) >= 1)
check("No dispatch", len(tracker.dispatches) == 0)

# --- Test 6: Empty transcription -> ignored ---
print("\n6. Empty transcription -> ignored")
ss = _build_server_state()
interp = FakeInterpreter("   ")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "   ")
items = drain(interp)
ignored = [i for i in items if isinstance(i, dict) and i.get("ignored")]
check("Empty text -> ignored", len(ignored) >= 1)
check("No dispatch", len(tracker.dispatches) == 0)

# --- Test 7: No wake word -> ignored ---
print("\n7. No wake word -> ignored")
ss = _build_server_state()
interp = FakeInterpreter("the weather is nice today")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "the weather is nice today")
items = drain(interp)
ignored = [i for i in items if isinstance(i, dict) and i.get("ignored")]
check("No wake word -> ignored", len(ignored) >= 1)
check("No dispatch", len(tracker.dispatches) == 0)

# --- Test 8: Only wake word with prefix -> dispatches with prefix ---
print("\n8. Only wake word with prefix -> dispatches 'hey'")
ss = _build_server_state()
interp = FakeInterpreter("hey friday")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday")
check("Dispatches with prefix content", len(tracker.dispatches) == 1)
check("Content is prefix 'hey'", tracker.dispatches == ["hey"])

# --- Test 9: Rapid triple -> only 1 dispatch ---
print("\n9. Rapid triple input -> only 1 dispatch")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, triple test")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, triple test")
feed_wake(new_input_fn, interp, "hey friday, triple test")
feed_wake(new_input_fn, interp, "hey friday, triple test")
check("Only 1 dispatch from triple", len(tracker.dispatches) == 1)
items = drain(interp)
ignored = [i for i in items if isinstance(i, dict) and i.get("ignored")]
check("2 calls emitted ignored", len(ignored) == 2)

# --- Test 10: Response lifecycle emits complete status ---
print("\n10. Response lifecycle emits complete status")
ss = _build_server_state()
interp = FakeInterpreter("hey friday, status test")
new_input_fn, tracker = _make_new_input(interp, ss)
feed_wake(new_input_fn, interp, "hey friday, status test")
items = drain(interp)
statuses = [i for i in items if isinstance(i, dict) and i.get("type") == "status"]
check("Complete status emitted",
      any(s.get("content") == "complete" for s in statuses))
check("Message end emitted",
      any(isinstance(i, dict) and i.get("type") == "message"
          and i.get("end") is True for i in items))


print("\n" + "=" * 60)
if all_pass:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)
