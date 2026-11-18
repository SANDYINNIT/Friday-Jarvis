"""ROUND S (2026-10-03) - FRIDAY must not mutate the stdlib json module.

THE DEFECT THIS LOCKS
=====================
`source/server/profiles/default.py` used to install a `safe_json_loads` wrapper
onto litellm's prompt-template factory, "to fix a Gemini tool-call bug" whose
`function.arguments` contained raw Python instead of JSON.

It looked scoped and was not. `factory.json` IS the stdlib json module
(verified: `factory2.json is json` -> True), so

    factory2.json.loads = safe_json_loads

was literally `json.loads = ...` for the WHOLE process. `json.loads` then never
raised `JSONDecodeError`; it returned `{"code": <raw string>}` instead.

Verified consequences, all reproduced:

* `audit.py:122` - a truncated audit line stopped being skipped and became a
  phantom `{"code": ...}` event in the append-only audit trail.
* `mcp_runtime.py:143` - a malformed MCP config stopped raising
  MCPConfigError and was passed to `_validate_config` as a dict.
  (A third victim, `intent_judge.py`, was removed entirely in Round U - it had
  produced 0 useful decisions in 34/34 attempts - so its case is now covered
  generically in `NoJsonWrapperSurvivorTests` and via the MCP/audit parsers.)
* litellm's OWN `except json.JSONDecodeError` handlers (factory.py:1478, 2898,
  3311, 3648) became dead code, including its documented recovery for
  BerriAI/litellm#20543.

Removal was chosen over narrowing because the wrapped call site
(`_gemini_tool_call_invoke_helper`, factory.py:1130) is reachable ONLY from
`litellm/llms/vertex_ai/gemini/transformation.py`, while FRIDAY prefixes every
cloud model with `"openai/"` (brain_router.py:604) and `get_llm_provider`
resolves `"openai/gemini-*"` to `custom_llm_provider="openai"` - never
`vertex_ai`. There was no reachable site to narrow it to.

WHY THESE TESTS ARE STRUCTURAL, NOT VACUOUS
===========================================
They import the REAL production module and assert on the REAL stdlib object and
the REAL FRIDAY parsers - `AuditLog.list_recent` and `mcp_runtime.load_config`.
Nothing reimplements production behaviour, so re-inserting the patch turns them
red. `NoGlobalJsonPatchTests` additionally parses `default.py` with `ast` and
fails on ANY executable assignment to `json.loads` (or a wholesale replacement
of the json module), so the regression cannot come back in a differently-shaped
form.
"""

import ast
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server.audit import AuditLog  # noqa: E402
from source.server.mcp_runtime import MCPConfigError, load_config  # noqa: E402

PROFILE_PY = os.path.join(
    os.getcwd(), "source", "server", "profiles", "default.py"
)

# Captured BEFORE the production module is imported, so "stdlib" means the real
# interpreter's implementation and not whatever the profile left behind.
_STDLIB_LOADS = json.loads
_STDLIB_DECODER = json.JSONDecoder
_STDLIB_MODULE = json


def _import_production_profile():
    """Import the real production module (already imported is fine)."""
    import source.server.profiles.default  # noqa: F401

    return source.server.profiles.default


# ---------------------------------------------------------------------------
# 1 + 2. The core contract: importing FRIDAY leaves json.loads alone.
# ---------------------------------------------------------------------------


class StdlibJsonContractTests(unittest.TestCase):
    def setUp(self):
        _import_production_profile()

    def test_json_loads_is_still_the_stdlib_implementation(self):
        self.assertIs(
            json.loads,
            _STDLIB_LOADS,
            "importing source.server.profiles.default replaced json.loads - "
            "that is the process-wide monkey patch Round S removed",
        )
        self.assertEqual(getattr(json.loads, "__module__", None), "json")
        self.assertIs(json, _STDLIB_MODULE)

    def test_json_loads_really_is_the_interpreters_own(self):
        code = getattr(json.loads, "__code__", None)
        self.assertIsNotNone(code, "json.loads must be the stdlib Python function")
        self.assertIn("json", code.co_filename.replace("\\", "/").lower())

    def test_invalid_json_raises_again(self):
        for bad in ('{"a":', "{'directed': true}", "not json", "", '{"a": 1,}'):
            with self.subTest(bad=bad):
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(bad)

    def test_valid_json_is_unchanged(self):
        self.assertEqual(json.loads('{"a": 1}'), {"a": 1})
        self.assertEqual(json.loads("[1, 2, 3]"), [1, 2, 3])
        self.assertEqual(json.loads('"plain"'), "plain")
        self.assertEqual(json.loads("17"), 17)
        self.assertEqual(json.loads("null"), None)
        self.assertEqual(json.loads('{"n": {"deep": [1, {"x": true}]}}'),
                         {"n": {"deep": [1, {"x": True}]}})

    def test_json_decoder_is_not_replaced_either(self):
        self.assertIs(json.JSONDecoder, _STDLIB_DECODER)
        with self.assertRaises(ValueError):
            json.JSONDecoder().decode("{")

    def test_the_old_wrapper_name_is_gone_from_production(self):
        with open(PROFILE_PY, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        executable = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn(
            "safe_json_loads", executable,
            "the helper became unused and must not be left as live code",
        )
        self.assertNotIn("original_json_loads", executable)
        # And no import of json survives to be re-patched later.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name, "json",
                        "default.py no longer needs to import json; a future "
                        "re-patch would start from here",
                    )


# ---------------------------------------------------------------------------
# 4. Audit: a malformed line must be skipped, never turned into a phantom.
# ---------------------------------------------------------------------------


class AuditParsingRegressionTests(unittest.TestCase):
    def test_truncated_line_is_skipped_not_turned_into_a_phantom(self):
        import tempfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(tmp)
            day = datetime.now().strftime("%Y%m%d")
            path = os.path.join(tmp, f"friday-audit-{day}.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"operation": "assistant_turn", "actor": "text"}\n')
                # Truncated mid-object: must be skipped.
                handle.write('{"operation": "assistant_turn", "actor":\n')
                # Single-quoted: regex-valid shape, invalid JSON.
                handle.write("{'operation': 'assistant_turn'}\n")
                # Valid, must survive.
                handle.write('{"operation": "assistant_turn", "actor": "voice"}\n')

            events = log.list_recent(limit=50)

        self.assertEqual(len(events), 2, "malformed lines must be skipped entirely")
        self.assertEqual(
            [e.get("actor") for e in events], ["text", "voice"],
            "only the two well-formed events may come back",
        )
        for event in events:
            self.assertNotEqual(
                set(event.keys()), {"code"},
                "a phantom {'code': ...} event leaked into the audit trail - "
                "that is the monkey patch's signature",
            )
            self.assertNotIn("code", event)

    def test_a_file_of_only_junk_yields_no_events(self):
        import tempfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(tmp)
            day = datetime.now().strftime("%Y%m%d")
            with open(os.path.join(tmp, f"friday-audit-{day}.jsonl"), "w",
                      encoding="utf-8") as handle:
                handle.write('{"broken":\n')
                handle.write("{'broken': 1}\n")
                handle.write("garbage\n")
            events = log.list_recent(limit=50)

        self.assertEqual(events, [])

    def test_list_recent_does_not_raise_on_junk(self):
        import tempfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(tmp)
            day = datetime.now().strftime("%Y%m%d")
            with open(os.path.join(tmp, f"friday-audit-{day}.jsonl"), "w",
                      encoding="utf-8") as handle:
                handle.write("\x00 not json at all \n")
            self.assertEqual(log.list_recent(limit=10), [])


# ---------------------------------------------------------------------------
# 5. ROUND U RE-HOME (2026-10-04): the intent judge is gone.
#
# This section used to prove that `intent_judge._extract_json` returned None for
# malformed JSON. The judge was removed in Round U (34/34 no-op, 6.52s median),
# so that subject no longer exists and importing it would be a lie.
#
# The guarantee is NOT dropped - it is moved onto a SURVIVING production
# consumer of `json.loads` that the monkey patch also broke, and which still
# distinguishes "reject malformed input" from "wrap it in {'code': ...}":
# `mcp_runtime.load_config`, covered in McpConfigRegressionTests below, plus a
# direct check here that no FRIDAY json.loads caller can receive the wrapper's
# fake dict. The audit path (section 4) is the other survivor.
# ---------------------------------------------------------------------------


class NoJsonWrapperSurvivorTests(unittest.TestCase):
    """No production parser may ever see the monkey patch's `{'code': ...}`."""

    def setUp(self):
        _import_production_profile()

    def test_malformed_braced_json_still_raises_for_any_caller(self):
        # Exactly the shapes the old judge's greedy regex let through, and which
        # the wrapper turned into a dict instead of an exception.
        for bad in ("{'directed': true}", "{'a': 1}", '{"a": 1,}'):
            with self.subTest(bad=bad):
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(bad)

    def test_the_wrapper_dict_shape_is_unreachable(self):
        for bad in ("{'directed': true}", "{'a': 1}", "not json", ""):
            with self.subTest(bad=bad):
                try:
                    result = json.loads(bad)
                except json.JSONDecodeError:
                    continue
                self.assertNotEqual(
                    set(result) if isinstance(result, dict) else set(),
                    {"code"},
                    "a caller received the monkey patch's {'code': ...} wrapper",
                )

    def test_the_removed_judge_module_is_not_importable(self):
        """Guards the re-home: nothing may reintroduce the dependency."""
        with self.assertRaises(ImportError):
            importlib.import_module("source.server.intent_judge")  # noqa: F401


# ---------------------------------------------------------------------------
# 6. MCP: an invalid config must follow the validation/error path.
# ---------------------------------------------------------------------------


class McpConfigRegressionTests(unittest.TestCase):
    def test_malformed_config_raises_mcp_config_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{'servers': [")  # invalid JSON
            with self.assertRaises(MCPConfigError):
                load_config(path)

    def test_single_quoted_config_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{'servers': {}}")
            with self.assertRaises(MCPConfigError):
                load_config(path)

    def test_a_phantom_code_dict_would_have_been_accepted(self):
        """Demonstrates the exact regression, without asserting on MCP policy.

        Under the patch, `json.loads("{'servers': [")` returned
        `{"code": "..."}` - a dict - so MCPConfigError was never raised and
        `_validate_config` received a dict with no "servers" key. This test only
        pins that json.loads no longer produces that shape.
        """
        _import_production_profile()
        with self.assertRaises(json.JSONDecodeError):
            json.loads("{'servers': [")

    def test_valid_config_still_loads(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp.json")
            # mcp_runtime._validate_config requires an object whose ONLY key is
            # "servers", holding a LIST.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"servers": []}')
            self.assertEqual(load_config(path), [])

    def test_a_config_with_the_wrong_shape_still_raises(self):
        """Proves validation is genuinely reached, not bypassed."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"servers": {}}')
            with self.assertRaises(MCPConfigError):
                load_config(path)

    def test_absent_config_still_disables_mcp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_config(os.path.join(tmp, "nope.json")), [])


# ---------------------------------------------------------------------------
# 7. Structural guard: no future assignment to json.loads in production.
# ---------------------------------------------------------------------------


class NoGlobalJsonPatchTests(unittest.TestCase):
    def _default_py(self):
        with open(PROFILE_PY, encoding="utf-8") as handle:
            return handle.read()

    def test_default_py_never_assigns_json_loads(self):
        tree = ast.parse(self._default_py())
        offenders = []
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for target in targets:
                # json.loads = ... / json.loads += ...  (any module alias too)
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "loads"
                    and isinstance(target.value, ast.Name)
                    and "json" in target.value.id.lower()
                ):
                    offenders.append(ast.unparse(node))
        self.assertEqual(
            offenders, [],
            "production code must never assign to json.loads; blast radius is "
            "the whole process: %r" % offenders,
        )

    def test_default_py_never_replaces_the_json_module_or_decoder(self):
        tree = ast.parse(self._default_py())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                name = None
                if isinstance(target, ast.Name):
                    name = target.id
                elif isinstance(target, ast.Attribute):
                    name = target.attr
                if name in ("JSONDecoder", "decoder", "loads", "dumps"):
                    offenders.append(ast.unparse(node))
        self.assertEqual(
            offenders, [],
            "production code must not replace json internals: %r" % offenders,
        )

    def test_no_module_level_attribute_assignment_to_json_in_default_py(self):
        """Catches `factory.json.loads = ...` style patching by any name."""
        tree = ast.parse(self._default_py())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                inner = target.value
                if isinstance(inner, ast.Attribute) and inner.attr == "json":
                    offenders.append(ast.unparse(node))
        self.assertEqual(
            offenders, [],
            "do not patch through a factory alias (alias.json.X = ...) - the "
            "alias IS the stdlib module: %r" % offenders,
        )

    def test_default_py_still_imports_and_configures(self):
        """The module must remain importable and keep its other patches."""
        module = _import_production_profile()
        self.assertTrue(hasattr(module, "interpreter"))
        # The Edge/Selenium patches further down must survive this change.
        source = self._default_py()
        self.assertIn("html2image", source)
        self.assertIn("selenium", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)