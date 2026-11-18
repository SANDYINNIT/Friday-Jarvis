"""Structural invariants for source/server/server.py.

These are guards against a bug class that actually happened: critical dispatch
code ending up INSIDE an `except` handler, so a turn silently disappears when
the guarded call happens to SUCCEED (the handler never fires) and only runs by
accident when it raises.

Real instance, 2026-10-01: on the voice path the `if memory_context:` prepend
and all three `old_input(...)` calls sat inside `except Exception:`. Non-screen
voice turns reached the interpreter only because an earlier line raised
NameError; screen voice turns captured, described and journalled the screen and
then dispatched NOTHING - no brain answer, no TTS, no error anywhere.

AST-based on purpose: indentation bugs are invisible to unit tests that import
callables, because the affected functions are closures created at boot and
cannot be imported by name.
"""

import ast
import unittest
import os

import pytest

SERVER_PY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "source",
    "server",
    "server.py",
)

# Calls that hand a turn to the interpreter / brain / user. None of these may
# ever live inside an error handler.
CRITICAL_CALLS = {
    "old_input",
    "apply_for",
    "_begin_single_turn",
    "_mark_turn_dispatch",
    "dispatch_text",
    "screen_response",
    "queue_direct_response",
}


def _tree():
    with open(SERVER_PY, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=SERVER_PY)


def _called_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_critical_dispatch_calls_are_never_inside_an_except_handler():
    tree = _tree()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        trapped = _called_names(node) & CRITICAL_CALLS
        if trapped:
            offenders.append((node.lineno, sorted(trapped)))
    assert not offenders, (
        "critical dispatch call(s) trapped inside except handler(s): %s. "
        "A turn would be silently dropped whenever the guarded code succeeds."
        % offenders
    )


def test_voice_and_text_paths_both_prepend_memory_context():
    """Regression for the other half of that bug: memory recall was computed on
    the text/Telegram//chat path and then never used at all."""
    with open(SERVER_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count('memory_context + "\\n\\nUser request:\\n" + content') >= 2, (
        "both the voice path and the text/Telegram//chat path must prepend "
        "memory_context, or recall is silently dropped on one channel"
    )


def test_screen_voice_turn_dispatches_after_capturing():
    """A screen voice turn must still reach the interpreter.

    Guards the exact regression: the capture succeeded, so the surrounding
    `except` never fired and the dispatch statements below it never ran.
    """
    tree = _tree()
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
            continue
        body_src = ast.dump(node)
        # Find functions that both capture a screen AND dispatch.
        has_screen = "screen_response" in body_src
        has_dispatch = "old_input" in body_src
        if has_screen and has_dispatch:
            # The dispatch call must not be dominated by an ExceptHandler.
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler):
                    if "old_input" in ast.dump(sub):
                        problems.append((node.name, sub.lineno))
    assert not problems, (
        "screen capture + dispatch in the same function, but dispatch is "
        "inside an except handler: %s" % problems
    )


def test_no_bare_bare_except_that_swallows_dispatch_errors():
    """`except Exception: pass` is fine for journal writes, but a bare pass
    directly after a dispatch call hides a real failure."""
    tree = _tree()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if len(node.body) != 1:
            continue
        stmt = node.body[0]
        if isinstance(stmt, ast.Pass):
            if _called_names(node) & CRITICAL_CALLS:
                offenders.append(node.lineno)
    assert not offenders, "except:pass hiding a critical call at %s" % offenders


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

class LatentScreenTurnBugsTests(unittest.TestCase):
    """Two bugs found by READING, in the exact path the screen work touches.

    Both were invisible to the test count:

    * `server.py` read an undefined local `last_shot` on every screen turn
      through /chat, the HUD and Telegram text. It hid for weeks because the
      phrasing that happened to be tested live ("what application is in the
      foreground on my screen right now?") is NOT a screen request, so it
      never reached the line. "what is on my screen?" is, and would raise
      NameError after screen_response() had already answered correctly.
    * `display_patch.py` called `_local_vision(prompt, model, jpeg)` while
      the signature is `(prompt, jpeg_bytes, timeout=None)`. The model-name
      string went in as the JPEG, base64 raised, and the local-vision fallback
      was permanently dead in that module. test_display_patch.py mocks the
      whole module, so CI could never see it.
    """

    def _server_src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent
                / "source" / "server" / "server.py").read_text(encoding="utf-8")

    def test_last_shot_is_never_read_undefined(self):
        """Round M (2026-10-03): the undefined `last_shot` local is gone because
        the whole inline freshness block was replaced by
        `_screen_context_prefix(server_state)`, which resolves the shot from
        `server_state` itself.

        The original intent is preserved and strengthened: no function may read a
        bare `last_shot` name that is never assigned or passed in, and the screen
        path must go through the helper rather than open-coding the lookup.
        """
        import ast

        tree = ast.parse(self._server_src())
        # Round M: `last_shot` no longer exists as an identifier at all - the
        # inline freshness block became `_screen_context_prefix(server_state)`.
        # The invariant is therefore: IF the name is read, it must be stored or
        # a parameter. Not existing at all satisfies it trivially, which is the
        # strongest form.
        loads, stores = [], False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "last_shot":
                if isinstance(node.ctx, ast.Store):
                    stores = True
                else:
                    loads.append(getattr(node, "lineno", 0))
        params = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                names = [a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)]
                if "last_shot" in names:
                    params.append(node.name)
        if loads:
            self.assertTrue(stores or params,
                            "last_shot is read at line(s) %s but never assigned "
                            "or passed in -> guaranteed NameError on a screen turn"
                            % loads)

    def test_screen_freshness_goes_through_the_helper(self):
        """Round M: the screen path must not open-code the `last_screen_shot`
        lookup, or the two paths drift apart again - which is exactly how
        `last_screen_shot` stayed unwritten on the voice and /chat paths for so
        long while the Telegram path worked."""
        import ast

        tree = ast.parse(self._server_src())
        helper = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) \
                    and node.name == "_screen_context_prefix":
                helper = node
        self.assertIsNotNone(helper,
                             "_screen_context_prefix is missing; the screen "
                             "freshness logic must live in one place")
        self.assertIn("last_screen_shot", ast.unparse(helper),
                      "the helper must resolve the shot from server_state")
        self.assertIn("_SCREEN_CONTEXT_TTL_SECONDS", ast.unparse(helper),
                      "the helper must apply the TTL, not a caller")

    def test_no_function_reads_last_screen_shot_inline_outside_the_helper(self):
        import ast

        tree = ast.parse(self._server_src())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "_screen_context_prefix":
                continue
            body = ast.unparse(node)
            if 'get("last_screen_shot")' in body or '["last_screen_shot"]' in body:
                # Writes are legitimate; only a READ outside the helper is not.
                if ".get(\"last_screen_shot\")" in body:
                    offenders.append(node.name)
        self.assertEqual(offenders, [],
                         "last_screen_shot is read outside _screen_context_prefix "
                         "in: %s" % offenders)

    def test_screen_turn_phrasings_that_really_reach_the_bug(self):
        from source.server.screen_understanding import is_screen_request
        for phrase in ("what is on my screen?", "what am I looking at?",
                       "read my screen", "take a screenshot"):
            self.assertTrue(is_screen_request(phrase), phrase)

    def test_telegram_post_action_screenshot_is_delivered_after_completion(self):
        source = self._server_src()
        self.assertIn('return "after_action"', source)
        self.assertIn("async def _deliver_post_action_screenshot", source)
        # Option 2: the post-action path reads the ownership RECORD, not the
        # retired `telegram_capture` dict, but the ordering contract is the same.
        self.assertIn(
            "ownership.post_action_screenshot and not turn_failed", source)
        self.assertIn("await telegram_adapter.send_photo", source)

    def test_local_vision_is_called_with_arguments_in_the_right_order(self):
        """The fallback silently died because the args were swapped."""
        import inspect
        import re
        from pathlib import Path
        from source.server import screen_understanding as su

        expected = list(inspect.signature(su._local_vision).parameters)[1]
        self.assertEqual(expected, "jpeg_bytes")
        src = (Path(__file__).resolve().parent / "source" / "server"
               / "display_patch.py").read_text(encoding="utf-8")
        for call in re.findall(r"_su\._local_vision\(([^)]*)\)", src):
            self.assertNotIn(
                "select_vision_model", call,
                "_local_vision called with a model name where JPEG bytes "
                "belong: %r" % call)


if __name__ == "__main__":
    unittest.main()
