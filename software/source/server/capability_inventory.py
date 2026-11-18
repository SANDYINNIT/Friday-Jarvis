"""Privacy-safe browsing of already loaded FRIDAY tools and saved skills."""

import hashlib
from pathlib import Path
import re
import sys
import stat
import time

from .credential_stripper import redact_for_storage
from .skill_metadata import skill_directory


_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,79}")
_URL = re.compile(r"(?i)\b(?:https?|wss?|file)://[^\s<>\"']+")
_PATH = re.compile(r"(?:[a-zA-Z]:[\\/]|\\\\)[^\s<>\"']+")
_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+")
_MAX_ROWS = 100


def text(value, limit=1500):
    if not isinstance(value, str):
        return ""
    return _PATH.sub("[path omitted]", _URL.sub("[link omitted]",
        _SECRET.sub("[credential omitted]", redact_for_storage(value[:limit]))))


def _fields(value):
    try:
        result = object.__getattribute__(value, "__dict__")
        return result if type(result) is dict else {}
    except (AttributeError, TypeError):
        return {}


def _name(value):
    return value if isinstance(value, str) and _NAME.fullmatch(value) and text(value) == value else None


def inputs(schema):
    if type(schema) is not dict or schema.get("type") != "object":
        return {"state": "unsupported", "rows": []}
    props, required = schema.get("properties", {}), schema.get("required", [])
    if (type(props) is not dict or type(required) is not list
            or any(type(x) is not str or x not in props for x in required)):
        return {"state": "malformed", "rows": []}
    rows = []
    valid_types = {"string", "number", "integer", "boolean", "object", "array", "null"}
    for name, prop in list(props.items())[:20]:
        if not _name(name) or type(prop) is not dict:
            return {"state": "malformed", "rows": []}
        kind = prop.get("type")
        choices = prop.get("enum")
        rows.append({"name": name, "type": kind if isinstance(kind, str) and kind in valid_types else "unspecified",
                     "required": name in required, "description": text(prop.get("description")),
                     "choices": [text(choice,160) for choice in choices[:20] if type(choice) is str]
                     if type(choices) is list else []})
    return {"state": "ready", "rows": rows, "truncated": len(props) > 20}


def _builtin(interpreter):
    module = sys.modules.get("interpreter.core.llm.run_tool_calling_llm")
    if module is None:
        return {"state": "unavailable", "rows": []}
    schema = _fields(module).get("tool_schema")
    function = schema.get("function") if type(schema) is dict else None
    if type(function) is not dict or not _name(function.get("name")):
        return {"state": "error", "rows": []}
    llm = _fields(_fields(interpreter).get("llm"))
    execution = sys.modules.get("interpreter.core.friday_execution")
    gate = execution.gate_for(interpreter) if execution is not None else None
    return {"state": "ready", "rows": [{
        "id": "oi:" + function["name"], "name": function["name"], "kind": "tool",
        "source": "Open Interpreter function schema", "source_id": "oi",
        "description": text(function.get("description")), "inputs": inputs(function.get("parameters")),
        "registration": "schema_loaded", "enabled": None, "function_calling": llm.get("supports_functions") if
            type(llm.get("supports_functions")) is bool else None,
        "automatic": _fields(interpreter).get("auto_run") if
            type(_fields(interpreter).get("auto_run")) is bool else None,
        "execution_gate": gate is not None, "authorization": "request_scoped",
    }]}


def _skill_path(interpreter):
    computer = _fields(_fields(interpreter).get("computer"))
    path = _fields(computer.get("skills")).get("path")
    if not isinstance(path, str) or not path:
        return None
    root = Path(__file__).resolve().parents[2]
    raw = Path(path).absolute()
    if ".." in raw.parts or not raw.is_relative_to(root):
        return None
    current = root
    for part in raw.relative_to(root).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return raw
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return None
    return raw


def _skills(interpreter):
    path = _skill_path(interpreter)
    if path is None:
        return {"state": "unsupported", "rows": []}
    result = skill_directory(path)
    computer = _fields(_fields(interpreter).get("computer"))
    for row in result["rows"]:
        row.update(id="skill:" + hashlib.sha256(row["file"].encode("utf-8")).hexdigest(),
                   kind="procedure" if row["procedure"] else "extension",
                   source="FRIDAY saved skill files", source_id="skills", registration="stored",
                   enabled=computer.get("import_skills") if type(computer.get("import_skills")) is bool else None)
        row["name"] = text(row["name"],80) or "Unnamed skill"
        row["description"] = text(row.pop("when_to_use"))
        row["verify"] = text(row["verify"])
        row["steps"] = [text(step,2000) for step in row["steps"]]
        row["inputs"] = {"state": "ready" if row["metadata_state"] == "ready" else "unavailable",
                         "rows": [{"name": entry["name"], "type": "unspecified", "required": None, "description": ""}
                                  for entry in row["inputs"] if _name(entry["name"])]}
        row.pop("file")
    return result


def _mcp(state):
    runtime = state.get("mcp_runtime")
    module = sys.modules.get(f"{__package__}.mcp_runtime")
    runtime_type = _fields(module).get("MCPRuntime")
    if runtime is None or runtime_type is None or type(runtime) is not runtime_type:
        return {"state": "error" if state.get("mcp_startup_failed") else "unavailable", "rows": []}
    rows = []
    for server in runtime.cached_inventory()[:30]:
        name = _name(server.get("server"))
        if name is None:
            return {"state": "error", "rows": []}
        tools = server.get("tools")
        rows.append({"id": f"mcp-server:{name}", "name": name, "kind": "integration",
                     "source": "FRIDAY MCP adapter", "source_id": f"mcp:{name}",
                     "registration": "configured", "enabled": server["enabled"],
                     "session_attached": server["session_attached"], "discovered_at": server["discovered_at"],
                     "connection_status": server["status"],
                     "description": "Configured MCP server. Its session object does not establish current liveness.",
                     "inputs": {"state": "unavailable", "rows": []}})
        if tools is None:
            continue
        for tool in tools[:_MAX_ROWS]:
            tool_name = _name(tool.get("tool_name"))
            if tool_name is None:
                continue
            rows.append({"id": f"mcp:{name}:{tool_name}", "name": tool_name, "kind": "mcp",
                         "source": f"FRIDAY MCP · {name}", "source_id": f"mcp:{name}",
                         "description": text(tool.get("description")), "inputs": inputs(tool.get("input_schema")),
                         "registration": "cached", "enabled": server["enabled"],
                         "connection_status": server["status"],
                         "schema_supported": isinstance(tool.get("input_schema"), dict),
                         "allowlisted": tool.get("enabled") if type(tool.get("enabled")) is bool else None,
                         "confirmation_required": tool.get("confirmation_required") if
                            type(tool.get("confirmation_required")) is bool else None,
                         "risk": tool.get("risk_class") if tool.get("risk_class") in
                            ("read_only", "external", "destructive", "unknown") else "unknown",
                         "session_attached": server["session_attached"], "discovered_at": server["discovered_at"],
                         "authorization": "request_scoped"})
    return {"state": "ready", "rows": rows}


def capability_inventory(interpreter):
    state = _fields(interpreter).get("server_state") or {}
    sources, rows = {}, []
    for name, read in (("builtin", lambda: _builtin(interpreter)),
                       ("skills", lambda: _skills(interpreter)), ("mcp", lambda: _mcp(state))):
        try:
            result = read()
            sources[name] = {key: value for key, value in result.items() if key != "rows"}
            rows.extend(result["rows"])
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            sources[name] = {"state": "error"}
    # Bound the complete response. Invalid entries never enter another domain.
    return {"available": True, "sources": sources, "rows": rows[:300], "truncated": len(rows) > 300,
            "read_at": time.time()}
