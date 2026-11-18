"""Structural scope tests for the tool-output branch of `new_output`.

TWO REAL UnboundLocalErrors WERE FIRING LIVE
---------------------------------------------
1. `fragment` - a regression introduced on 2026-10-03 while repairing an
   IndentationError in this same block. `chat_upsert_tool(... len(fragment) ...)`
   sat one indent level ABOVE the `if` that assigns `fragment`, so any
   code-type chunk whose payload was empty or already ended raised
   `UnboundLocalError`. Caught by the handler at the bottom of `new_output`,
   which logged "output error" and slept 0.5s - so the tool script row was lost
   and the rest of that chunk was skipped. 12 occurrences in one live boot.
   (The AST diff used to check this missed it: difflib realigned on the
   identical call text.)

2. `language` - PRE-EXISTING, present identically in the untouched copy. Bound
   only inside the tool-START branch but read by the tool-END branch, so a
   terminal chunk with no preceding start raised the same error.

These tests assert STRUCTURE, not behaviour, because the bug was purely
structural and a behavioural test would mean driving the whole async generator.
The last class deliberately re-creates the broken shape and proves it both
raises AND is flagged, so the guard is known to be capable of failing.
"""

import ast
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.getcwd())

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")


def _new_output():
    with open(SERVER_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "new_output":
            return node
    raise AssertionError("new_output not found")


def _parents(root):
    return {id(child): parent
            for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


def _assignments(fn, name):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id == name]


def _loads(fn, name):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == name
            and isinstance(n.ctx, ast.Load)]


def _enclosing_ifs(fn, assignments):
    """The `if` nodes whose BODY contains one of these assignments.

    Comparing an `If` node against a list of `Assign` nodes can never match,
    which is the trap the first version of this test fell into.
    """
    wanted = {id(a) for a in assignments}
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            for child in ast.iter_child_nodes(node):
                if id(child) in wanted:
                    found.add(id(node))
                    break
    return found


def _dominated(fn, use, guarding_ids, parents):
    """True when some guarding `if` is an ancestor of `use`."""
    child = parents.get(id(use))
    while child is not None:
        if isinstance(child, ast.If) and id(child) in guarding_ids:
            return True
        child = parents.get(id(child))
    return False


class FragmentScopeTests(unittest.TestCase):
    def setUp(self):
        self.fn = _new_output()
        self.parents = _parents(self.fn)
        self.assigns = _assignments(self.fn, "fragment")
        self.guarding = _enclosing_ifs(self.fn, self.assigns)

    def test_fragment_is_assigned(self):
        self.assertTrue(self.assigns)
        self.assertTrue(self.guarding,
                        "the assignment is not inside any `if` - the guard "
                        "would be vacuous")

    def test_every_fragment_read_is_dominated_by_its_assignment(self):
        loads = _loads(self.fn, "fragment")
        self.assertTrue(loads, "fragment should still be read somewhere")
        for load in loads:
            self.assertTrue(
                _dominated(self.fn, load, self.guarding, self.parents),
                "read of `fragment` at line %d is NOT inside the `if` that "
                "assigns it - this is the 2026-10-03 regression"
                % load.lineno)

    def test_the_script_row_is_emitted_from_inside_the_payload_branch(self):
        """The visible symptom was a missing tool-script row in the chat feed.

        2026-11-06: the row now reports the ASSEMBLED script length
        (`len(assembled)`) instead of the single-fragment length, because the
        assembler accumulates streaming deltas - the needle accepts either
        spelling, the scope property under test is unchanged.
        """
        target = None
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Name) \
                    and node.func.id == "chat_upsert_tool" \
                    and node.args and isinstance(node.args[0], ast.JoinedStr):
                rendered = ast.unparse(node.args[0])
                if "len(fragment)" in rendered or "len(assembled)" in rendered:
                    target = node
                    break
        self.assertIsNotNone(target, "the tool-script row call was not found")
        self.assertTrue(
            _dominated(self.fn, target, self.guarding, self.parents),
            "the tool-script row must be emitted inside the branch that "
            "assigns `fragment` (line %d)" % target.lineno)


class LanguageScopeTests(unittest.TestCase):
    def setUp(self):
        self.fn = _new_output()
        self.parents = _parents(self.fn)
        self.assigns = _assignments(self.fn, "language")
        self.guarding = _enclosing_ifs(self.fn, self.assigns)

    def test_every_language_read_is_dominated_by_an_assignment(self):
        loads = _loads(self.fn, "language")
        self.assertTrue(loads, "language should still be read")
        for load in loads:
            self.assertTrue(
                _dominated(self.fn, load, self.guarding, self.parents),
                "read of `language` at line %d is not inside the branch that "
                "assigns it - the tool-END branch reads a name bound only in "
                "the tool-START branch" % load.lineno)

    def test_language_is_bound_before_the_start_and_end_branches(self):
        """The fix hoisted the assignment to the top of the code-type block."""
        lines = sorted(n.lineno for n in self.assigns)
        self.assertTrue(lines)
        first = lines[0]
        branches = sorted(
            n.lineno for n in ast.walk(self.fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Call)
            and isinstance(n.test.func, ast.Attribute)
            and n.test.func.attr == "get"
            and n.test.args
            and isinstance(n.test.args[0], ast.Constant)
            and n.test.args[0].value in ("start", "end"))
        self.assertTrue(branches, "expected start/end branches in the tool block")
        for line in branches:
            self.assertLess(first, line,
                            "`language` is bound at line %d, after a branch "
                            "at line %d that reads it" % (first, line))


class RegressionGuardTests(unittest.TestCase):
    """Prove the guard above can actually fail."""

    BROKEN = """
    def handle(output):
        if output.get("type") == "code":
            if output.get("code") and not output.get("end"):
                fragment = str(output.get("code"))
            emit(len(fragment))
    """

    FIXED = """
    def handle(output):
        if output.get("type") == "code":
            if output.get("code") and not output.get("end"):
                fragment = str(output.get("code"))
                emit(len(fragment))
    """

    LANGUAGE_BROKEN = """
    def handle(output):
        if output.get("type") == "code":
            if output.get("start"):
                language = output.get("language") or "code"
            if output.get("end"):
                emit(language)
    """

    LANGUAGE_FIXED = """
    def handle(output):
        if output.get("type") == "code":
            language = output.get("language") or "code"
            if output.get("start"):
                emit(language)
            if output.get("end"):
                emit(language)
    """

    def _run(self, source, payload):
        namespace = {"emit": lambda n: None}
        exec(compile(textwrap.dedent(source), "<probe>", "exec"), namespace)
        return namespace["handle"](payload)

    def test_the_broken_fragment_shape_really_raises(self):
        for payload in ({"type": "code", "code": "", "end": True},
                        {"type": "code", "code": None, "end": True}):
            with self.assertRaises(UnboundLocalError):
                self._run(self.BROKEN, payload)

    def test_the_broken_language_shape_really_raises(self):
        with self.assertRaises(UnboundLocalError):
            self._run(self.LANGUAGE_BROKEN, {"type": "code", "end": True})

    def test_the_fixed_shapes_pass_both_branches(self):
        self._run(self.FIXED, {"type": "code", "code": "x", "end": False})
        self._run(self.FIXED, {"type": "code", "code": "", "end": True})
        self._run(self.LANGUAGE_FIXED, {"type": "code", "start": True})
        self._run(self.LANGUAGE_FIXED, {"type": "code", "end": True})

    def test_the_guard_flags_the_deliberately_broken_fragment_shape(self):
        fn = ast.parse(textwrap.dedent(self.BROKEN)).body[0]
        parents = _parents(fn)
        guarding = _enclosing_ifs(fn, _assignments(fn, "fragment"))
        loads = _loads(fn, "fragment")
        self.assertTrue(loads)
        flagged = [n.lineno for n in loads
                   if not _dominated(fn, n, guarding, parents)]
        self.assertTrue(flagged,
                        "the guard failed to flag the deliberately broken shape")

    def test_the_guard_flags_the_deliberately_broken_language_shape(self):
        fn = ast.parse(textwrap.dedent(self.LANGUAGE_BROKEN)).body[0]
        parents = _parents(fn)
        guarding = _enclosing_ifs(fn, _assignments(fn, "language"))
        loads = _loads(fn, "language")
        self.assertTrue(loads)
        flagged = [n.lineno for n in loads
                   if not _dominated(fn, n, guarding, parents)]
        self.assertTrue(flagged,
                        "the guard failed to flag the deliberately broken shape")

    def test_the_guard_accepts_the_fixed_shapes(self):
        for source in (self.FIXED, self.LANGUAGE_FIXED):
            fn = ast.parse(textwrap.dedent(source)).body[0]
            parents = _parents(fn)
            for name in ("fragment", "language"):
                guarding = _enclosing_ifs(fn, _assignments(fn, name))
                loads = _loads(fn, name)
                for load in loads:
                    self.assertTrue(
                        _dominated(fn, load, guarding, parents),
                        "%s read at line %d should be dominated"
                        % (name, load.lineno))


if __name__ == "__main__":
    unittest.main()