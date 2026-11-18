"""Read FRIDAY's saved Python skill metadata without evaluating it."""

import ast
import heapq
from pathlib import Path
import re
import stat


MAX_SKILL_BYTES = 64 * 1024
MAX_SKILLS = 100


def parse_skill(source, name):
    result = {"name": name, "when_to_use": "", "verify": "", "steps": [], "uses": 0,
              "metadata_state": "ready", "procedure": False, "inputs": []}
    if len(source.encode("utf-8")) > MAX_SKILL_BYTES:
        result["metadata_state"] = "too_large"
        return result
    for field, marker in (("when_to_use", "when"), ("verify", "verify")):
        match = re.search(rf"^#\s*{marker}\s*:\s*(.+)$", source, re.M)
        if match:
            result[field] = match.group(1).strip()[:1500]
    match = re.search(r"^#\s*uses\s*:\s*([0-9]{1,9})\s*$", source, re.M)
    if match:
        result["uses"] = int(match.group(1))
    try:
        tree = ast.parse(source)
        if sum(1 for _ in ast.walk(tree)) > 4000:
            raise ValueError("metadata tree too large")
        function = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name), None)
        if function is None:
            return result
        result["inputs"] = [{"name": arg.arg, "type": "unspecified"}
                            for arg in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]][:20]
        assignments = [node for node in function.body if isinstance(node, ast.Assign)
                       and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                       and node.targets[0].id == "steps"]
        if assignments:
            steps = ast.literal_eval(assignments[0].value)
            if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
                raise ValueError("expected literal instruction strings")
            result["steps"] = [step[:2000] for step in steps[:30]]
        allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Name,
                   ast.Store, ast.Load, ast.List, ast.Constant, ast.If, ast.Compare, ast.Lt,
                   ast.Call, ast.Expr, ast.BinOp, ast.Add, ast.Subscript)
        nodes = list(ast.walk(tree))
        result["procedure"] = bool(source.startswith("# friday-skill: v1") and len(tree.body) == 1
            and assignments and all(isinstance(node, allowed) for node in nodes)
            and all(isinstance(node.func, ast.Name) and node.func.id in ("print", "str", "len")
                    for node in nodes if isinstance(node, ast.Call))
            and all(node in assignments for node in nodes if isinstance(node, ast.Assign)))
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        result.update(metadata_state="malformed", steps=[], procedure=False)
    return result


def read_skill(path):
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_SKILL_BYTES + 1)
    if len(data) > MAX_SKILL_BYTES:
        return {"name": Path(path).stem, "when_to_use": "", "verify": "", "steps": [], "uses": 0,
                "metadata_state": "too_large", "procedure": False, "inputs": []}
    return parse_skill(data.decode("utf-8-sig"), Path(path).stem)


def skill_directory(path):
    """One level only. Symbolic links and Windows reparse entries are excluded."""
    root = Path(path)
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return {"state": "unavailable", "rows": []}
    if (stat.S_ISLNK(root_stat.st_mode)
            or getattr(root_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return {"state": "unsupported", "rows": []}
    if not root.is_dir():
        return {"state": "unavailable", "rows": []}
    paths = heapq.nsmallest(MAX_SKILLS + 1,
                           (file for file in root.iterdir() if file.suffix == ".py"),
                           key=lambda item: item.name)
    output, limited = [], len(paths) > MAX_SKILLS
    for file in paths:
        if file.suffix != ".py":
            continue
        metadata = file.lstat()
        if (not stat.S_ISREG(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            continue
        if len(output) == MAX_SKILLS:
            limited = True
            break
        try:
            entry = read_skill(file)
        except (OSError, UnicodeError):
            entry = {"name": file.stem, "when_to_use": "", "verify": "", "steps": [], "uses": 0,
                     "metadata_state": "error", "procedure": False, "inputs": []}
        output.append({**entry, "file": file.name, "updated_at": metadata.st_mtime})
    return {"state": "ready", "rows": output, "truncated": limited}
