"""Real inventory bridge over synthetic registries and saved skill files."""

import ast
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from source.server import capability_inventory, skill_metadata
from source.server.status_ui import StatusBus, WebviewBridge
from tools.field_notes_acceptance.capability_data import CapabilityData


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    module = "interpreter.core.llm.run_tool_calling_llm"
    monkeypatch.setitem(sys.modules, module, sys.modules.get(module))
    runtime = SimpleNamespace(server_state={})
    data = CapabilityData(tmp_path, runtime)
    bridge = WebviewBridge(StatusBus(), runtime)
    return data, bridge


def test_real_bridge_reads_only_existing_metadata_and_preserves_permissions(fixture):
    data, bridge = fixture
    before = data.snapshot()
    result = bridge.get_capability_inventory()
    assert result["sources"] == {"builtin": {"state": "ready"},
        "skills": {"state": "ready", "truncated": False}, "mcp": {"state": "ready"}}
    rows = {row["id"]: row for row in result["rows"]}
    assert rows["oi:execute"]["registration"] == "schema_loaded"
    assert [row["name"] for row in rows["oi:execute"]["inputs"]["rows"]] == ["language", "code"]
    assert rows["oi:execute"]["automatic"] is True
    assert rows["oi:execute"]["enabled"] is None
    assert rows["oi:execute"]["function_calling"] is True
    assert rows["mcp:studio:inspect"]["allowlisted"]
    assert not rows["mcp:archive:inspect"]["allowlisted"]
    assert rows["mcp:studio:publish"]["confirmation_required"]
    assert rows["mcp:archive:inspect"]["name"] == rows["mcp:studio:inspect"]["name"]
    assert rows["mcp:archive:odd_schema"]["inputs"]["state"] == "unsupported"
    assert rows["mcp-server:disabled"]["enabled"] is False
    assert any(row["kind"] == "procedure" and row["name"] == "field_notes" for row in rows.values())
    assert any(row["kind"] == "extension" and row["name"] == "extension" for row in rows.values())
    assert "private-fixture.invalid" not in repr(result)
    assert "PRIVATE_FIXTURE_TOKEN" not in repr(result)
    assert "DO_NOT_DISCLOSE" not in repr(result)
    for _ in range(3):
        bridge.get_capability_inventory()
    assert data.snapshot() == before


def test_no_discovery_properties_or_unknown_executable_objects_are_accessed(monkeypatch):
    class Trap:
        @property
        def computer(self):
            pytest.fail("reader accessed an executable descriptor")
    monkeypatch.delitem(sys.modules, "interpreter.core.llm.run_tool_calling_llm", raising=False)
    result = WebviewBridge(StatusBus(), Trap()).get_capability_inventory()
    assert result["rows"] == []
    assert result["sources"]["skills"]["state"] == "unsupported"
    assert result["sources"]["mcp"]["state"] == "unavailable"


def test_empty_missing_failed_and_unsupported_sources_are_distinct(fixture, monkeypatch):
    data, bridge = fixture
    original = data.runtime.computer.skills.path
    data.runtime.computer.skills.path = str(data.area / "missing")
    assert bridge.get_capability_inventory()["sources"]["skills"]["state"] == "unavailable"
    empty = data.area / "empty"
    empty.mkdir()
    data.runtime.computer.skills.path = str(empty)
    result = bridge.get_capability_inventory()
    assert result["sources"]["skills"]["state"] == "ready"
    assert not any(row["source_id"] == "skills" for row in result["rows"])
    data.runtime.computer.skills.path = str(Path(Path(__file__).anchor) / "outside-friday")
    assert bridge.get_capability_inventory()["sources"]["skills"]["state"] == "unsupported"
    data.runtime.computer.skills.path = original
    monkeypatch.setattr(capability_inventory, "skill_directory",
                        lambda _path: (_ for _ in ()).throw(OSError("private endpoint")))
    assert bridge.get_capability_inventory()["sources"]["skills"]["state"] == "error"


def test_saved_procedure_reader_cannot_evaluate_instructions(fixture):
    data, bridge = fixture
    from source.server.self_awareness import skill_catalog
    target = data.skills / "attack.py"
    target.write_text("# friday-skill: v1\n# when: Save a note\n"
        f"def attack(step=0):\n    steps = [open({str(data.marker)!r}, 'w').write('executed')]\n",
        encoding="utf-8")
    result = bridge.get_capability_inventory()
    item = next(row for row in result["rows"] if row["name"] == "attack")
    assert item["metadata_state"] == "malformed" and item["kind"] == "extension"
    assert not data.marker.exists()
    legacy = next(row for row in skill_catalog(root=data.area) if row["name"] == "attack")
    assert legacy["steps"] == [] and not data.marker.exists()
    # Exercise the profile's existing list helper without importing the profile
    # or executing the rest of its setup string.
    source = Path(__file__).parent / "source/server/profiles/default.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    template = next(node.value.value for node in module.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "setup_code_template"
                            for target in node.targets))
    tree = ast.parse(template)
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "friday_skills")
    scope = {"_friday_skill_dir": lambda: str(data.skills)}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "<isolated skill list>", "exec"), scope)
    rows = scope["friday_skills"]()["skills"]
    assert next(row for row in rows if row["name"] == "attack")["steps"] == []
    assert not data.marker.exists()


def test_malformed_metadata_is_bounded_and_schema_defaults_are_never_exposed(fixture):
    data, bridge = fixture
    data.schema["function"]["parameters"] = {"type": "object", "properties": {
        "safe": {"type": "string", "description": "https://private.invalid token=PRIVATE"}
    }, "required": ["safe"]}
    first = bridge.get_capability_inventory()
    tool = next(row for row in first["rows"] if row["id"] == "oi:execute")
    assert "private.invalid" not in repr(tool) and "PRIVATE" not in repr(tool)
    data.schema["function"]["parameters"]["required"] = ["safe", {}]
    assert next(row for row in bridge.get_capability_inventory()["rows"]
                if row["id"] == "oi:execute")["inputs"]["state"] == "malformed"
    large = skill_metadata.parse_skill("x" * (skill_metadata.MAX_SKILL_BYTES+1), "large")
    assert large["metadata_state"] == "too_large"
    result = skill_metadata.parse_skill("# friday-skill: v1\ndef helper(step=0):\n    steps=['read']\n    danger()\n", "helper")
    assert not result["procedure"]
    assert capability_inventory.inputs({"type":"object", "required":["missing"]})["state"] == "malformed"
    from source.server.mcp_runtime import MCPToolMetadata
    data.mcp._states["studio"].tools = [
        MCPToolMetadata("studio", "unknown", "Unknown policy", {}, "unknown", None, None)]
    unknown = next(row for row in bridge.get_capability_inventory()["rows"] if row["id"] == "mcp:studio:unknown")
    assert unknown["allowlisted"] is None and unknown["confirmation_required"] is None


def test_cache_read_cannot_connect_list_call_or_confirm(fixture, monkeypatch):
    data, bridge = fixture
    runtime = data.mcp
    def fail(*_args, **_kwargs):
        pytest.fail("cached inventory performed an operation")
    for method in ("start", "list_tools", "call_tool", "_own_server", "dispatch"):
        monkeypatch.setattr(runtime, method, fail)
    runtime._confirm = fail
    before = deepcopy(runtime.cached_inventory())
    bridge.get_capability_inventory()
    assert runtime.cached_inventory() == before
