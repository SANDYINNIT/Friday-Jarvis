"""Focused regression test for the websocket terminal state on STT failure."""
import queue

from source.server.server import emit_input_terminal


class FakeOutputQueue:
    def __init__(self):
        self.sync_q = queue.Queue()


output_queue = FakeOutputQueue()
emit_input_terminal(output_queue)
message = output_queue.sync_q.get(timeout=1)
assert message == {"ignored": True, "end": True}, message
print("PASS: STT failure emits ignored/end terminal message")
