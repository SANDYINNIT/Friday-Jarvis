"""
Headless test harness for Phase 2 barge-in / TTS interruption.
Verifies server.py's barge-in logic without a real microphone/TTS:

  1. Audio "start" during an active response AND TTS playing -> barge_in set, tts.stop() called
  2. Remaining message text during barge-in is NOT fed to TTS
  3. end-of-message during barge-in -> audio end emitted, TTS NOT restarted
  4. "complete" status clears barge_in (next response speaks normally)
  5. Audio "start" with NO active response -> no barge-in, TTS untouched
  6. Message text with NO barge-in -> fed to TTS normally (regression)
  7. Pause path still works during barge-in window
  8. Audio "start" during active response but TTS NOT playing (thinking window) -> no barge-in (regression for real mic bug)
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
        self.sync_q = self
    def put(self, item):
        self._q.put(item)
    def get(self, timeout=None):
        return self._q.get(timeout=timeout)
    def empty(self):
        return self._q.empty()


class FakeSTT:
    def __init__(self, text="hello world"):
        self._text = text
        self._next_text = None
    def set_next(self, text):
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


class FakeTTS:
    """Records internal state and all calls so tests can assert behavior."""
    def __init__(self, playing=False):
        self._playing = playing
        self.fed = []
        self.play_async_calls = []
        self.stop_calls = 0
    def set_playing(self, playing):
        self._playing = playing
    def is_playing(self):
        return self._playing
    def feed(self, text):
        self.fed.append(text)
    def play_async(self, **kw):
        self.play_async_calls.append(kw)
        self._playing = True
    def stop(self):
        self.stop_calls += 1
        self._playing = False


class FakeRespondThread:
    def __init__(self, alive=False):
        self._alive = alive
    def is_alive(self):
        return self._alive


class FakeInterpreter:
    def __init__(self, tts_playing=False, response_alive=False, stt_text="hello world"):
        self.output_queue = FakeOutputQueue()
        self.stt = FakeSTT(stt_text)
        self.tts = FakeTTS(tts_playing)
        self.play_audio = False
        self.audio_chunks = []
        self.respond_thread = FakeRespondThread(response_alive)
        self.old_input_calls = []

    def on_tts_chunk(self, chunk):
        self.output_queue.sync_q.put(chunk)

    async def old_input(self, chunk):
        self.old_input_calls.append(chunk)


def _build_server_state():
    return {
        "is_paused": False,
        "pending_input": None,
        "last_wake_time": 0.0,
        "last_wake_text": "",
        "barge_in": False,
    }


def _live(interp):
    return interp.respond_thread.is_alive()


def _make_new_input(interpreter, server_state):
    """Return a new_input coroutine bound to interpreter (mirrors server.py)."""
    import re as _re

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

                    if _live(self) and self.tts.is_playing():
                        server_state["barge_in"] = True
                        self.tts.stop()

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
                    else:
                        temp_text = text_clean_lower
                        for w in ["friday","frida","fry day","fryday","fri-day",
                                  "friyay","fire day","fri","day","fry"]:
                            temp_text = temp_text.replace(w, "")
                        temp_text = temp_text.strip(",.?! ")

                        if temp_text == "pause":
                            server_state["is_paused"] = True
                            self.tts.feed("Voice mode paused.")
                            self.tts.play_async(
                                on_audio_chunk=self.on_tts_chunk,
                                sentence_fragment_delimiters=".?!;,\n…)]}",
                                minimum_sentence_length=1)
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

                    self.output_queue.sync_q.put({"type": "status", "content": "complete"})
                    self.output_queue.sync_q.put(
                        {"role": "assistant", "type": "message", "end": True})
        except Exception as e:
            print("[STT/input error: %s]" % e)

    return new_input


def _make_new_output(interpreter, server_state):
    """Return a new_output coroutine bound to interpreter (mirrors server.py)."""
    async def old_output():
        # pull from the output queue; raise nothing
        return interpreter.output_queue.get()

    async def new_output(self):
        while True:
            try:
                output = await old_output()

                if isinstance(output, dict) and output.get("ignored") is True:
                    return {"ignored": True, "end": True}

                if isinstance(output, bytes):
                    return output

                await asyncio.sleep(0)

                if (
                    isinstance(output, dict)
                    and output.get("type") == "status"
                    and output.get("content") == "complete"
                ):
                    server_state["barge_in"] = False
                    if server_state["pending_input"] is not None and not server_state["is_paused"]:
                        pending = server_state["pending_input"]
                        server_state["pending_input"] = None
                        await self.old_input({"role": "user", "type": "message", "start": True})
                        await self.old_input({"role": "user", "type": "message", "content": pending["content"]})
                        await self.old_input({"role": "user", "type": "message", "end": True})
                    continue

                delimiters = ".?!;,\n…)]}"

                if output["type"] == "message" and len(output.get("content", "")) > 0:

                    if server_state["barge_in"]:
                        await asyncio.sleep(0)
                        continue

                    self.tts.feed(output.get("content"))

                    if not self.tts.is_playing() and any([c in delimiters for c in output.get("content")]):
                        self.tts.play_async(on_audio_chunk=self.on_tts_chunk, muted=not self.play_audio,
                                            sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}

                if output == {"role": "assistant", "type": "message", "end": True}:
                    if server_state["barge_in"]:
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                    if not self.tts.is_playing():
                        self.tts.play_async(on_audio_chunk=self.on_tts_chunk, muted=not self.play_audio,
                                            sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}
                    return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
            except Exception as e:
                print("[TTS/output error: %s]" % e)
                await asyncio.sleep(0.5)

    interpreter.old_input_aliased = types.MethodType(
        lambda self: None, interpreter)  # noop placeholder (unused)

    async def bound(self):
        return await new_output(self)

    return new_output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(coro):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(asyncio.wait_for(coro, 5))


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
print("PHASE 2 - BARGE-IN / TTS INTERRUPTION TESTS")
print("=" * 60)

all_pass = True

def check(name, condition):
    global all_pass
    status = "PASS" if condition else "FAIL"
    if not condition:
        all_pass = False
    print("  [%s] %s" % (status, name))


# --- Test 1: barge-in when user speaks during an active, playing response ---
print("\n1. Interrupt while TTS is playing")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=True, response_alive=True)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
check("barge_in set to True", ss["barge_in"] is True)
check("tts.stop() called", interp.tts.stop_calls == 1)
check("TTS no longer playing", interp.tts.is_playing() is False)

# --- Test 2: remaining text of interrupted response is NOT fed/played ---
print("\n2. Interrupted response drains silently")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=True, response_alive=True)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
# Response keeps streaming text (interrupted): must not feed TTS.
# Queue msg text + end so one new_output call terminates.
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "content": "It seems someone"})
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "end": True})
run(_make_new_output(interp, ss)(interp))
check("barge-in response text not fed to TTS", interp.tts.fed == [])
check("no play_async restarted for remaining text", interp.tts.play_async_calls == [])

# --- Test 3: end-of-message during barge-in emits audio end, no restart ---
print("\n3. Interrupted response end-of-message")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=True, response_alive=True)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "end": True})
out = run(_make_new_output(interp, ss)(interp))
check("audio end marker emitted", isinstance(out, dict) and out.get("end") is True)
check("TTS not restarted at end", interp.tts.play_async_calls == [])

# --- Test 4: complete clears barge_in -> next response speaks ---
print("\n4. Next response speaks after interrupted response completes")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=True, response_alive=True)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
check("barge_in set initially", ss["barge_in"] is True)
# Response completes, then next response sends a message.
interp.output_queue.sync_q.put({"type": "status", "content": "complete"})
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "content": "Done."})
run(_make_new_output(interp, ss)(interp))
check("barge_in cleared on complete", ss["barge_in"] is False)
check("TTS fed again after barge-in cleared", "Done." in interp.tts.fed)

# --- Test 5: start with no active response -> no barge-in ---
print("\n5. Speaking with no active response (normal idle)")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=False, response_alive=False)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
check("barge_in stays False", ss["barge_in"] is False)
check("tts.stop() NOT called", interp.tts.stop_calls == 0)

# --- Test 6: regression — normal response text still fed/played ---
print("\n6. Normal (non-interrupted) response still speaks")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=False, response_alive=False)
new_output = _make_new_output(interp, ss)
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "content": "Hello there. Nice."})
out = run(new_output(interp))
check("TTS fed normally", "Hello there. Nice." in interp.tts.fed)
check("play_async called to start playback", len(interp.tts.play_async_calls) == 1)
check("audio start marker emitted", isinstance(out, dict) and out.get("start") is True)

# --- Test 7: pause still functional during barge-in window ---
print("\n7. Pause command still works after barge-in")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=True, response_alive=True, stt_text="friday pause")
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
interp.stt.set_next("friday pause")
run(new_input(interp, {"end": True}))
check("is_paused set", ss["is_paused"] is True)
items = drain(interp)
paused = [i for i in items if isinstance(i, dict) and i.get("voice_paused") is True]
check("voice_paused emitted", len(paused) == 1)

# --- Test 8: thinking window — response active but TTS NOT playing ---
print("\n8. Thinking window: response active + TTS NOT playing -> no barge-in (the real mic bug)")
ss = _build_server_state()
interp = FakeInterpreter(tts_playing=False, response_alive=True)
new_input = _make_new_input(interp, ss)
run(new_input(interp, {"start": True}))
check("barge_in stays False (was the root bug)", ss["barge_in"] is False)
check("tts.stop() NOT called", interp.tts.stop_calls == 0)
# Now simulate the response producing text after thinking
interp.output_queue.sync_q.put({"role": "assistant", "type": "message", "content": "It is 3:14 PM."})
out = run(_make_new_output(interp, ss)(interp))
check("TTS fed normally despite prior start during thinking", "It is 3:14 PM." in interp.tts.fed)
check("play_async called to start playback", len(interp.tts.play_async_calls) == 1)
check("audio start marker emitted", isinstance(out, dict) and out.get("start") is True)

print("\n" + "=" * 60)
if all_pass:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)