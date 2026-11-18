"""Scan a release before staging and after commit; no matched values are emitted."""
from pathlib import Path
import argparse
import importlib.util
import io
import json
import os
import re
from urllib.parse import urlsplit
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publication_patterns", ROOT / "packaging/scan_public.py")
patterns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patterns)


def git(*args, input=None):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], input=input, stderr=subprocess.DEVNULL)


def scan(old_commits):
    findings, fixtures, files = [], [], []
    def inspect(data, name, oid=None):
        found = patterns.inspect(data, name)
        for item in found:
            if oid:
                item["object"] = oid
            # Reserved invalid-host auth is intentionally rejected by these tests.
            reserved_auth = False
            if item["category"] == "url-embedded-auth" and name in (
                    "tests/setup/test_user_routes.py", "software/test_n8n_bridge.py"):
                lines = data.decode("utf-8", errors="replace").splitlines()
                urls = [url for number in item["lines"] for url in re.findall(
                    r"https?://[^\s\x22\x27<>]+", lines[number - 1])]
                try:
                    reserved_auth = bool(urls) and all(
                        (urlsplit(url).hostname or "").endswith(".invalid") for url in urls)
                except ValueError:
                    pass
            if reserved_auth:
                fixtures.append(item)
            else:
                findings.append(item)
    for folder, dirs, names in os.walk(ROOT, followlinks=False):
        base = Path(folder)
        if base == ROOT and ".git" in dirs:
            dirs.remove(".git")
        for name in list(dirs):
            path = base / name
            if path.is_symlink() or name == ".git":
                findings.append({"file": path.relative_to(ROOT).as_posix(), "category": "nested-git-or-symlink"})
                dirs.remove(name)
        for name in names:
            path = base / name
            rel = path.relative_to(ROOT).as_posix()
            files.append(rel)
            if path.is_symlink():
                findings.append({"file": rel, "category": "symlink"})
                continue
            if (path.suffix.lower() in (".db",".sqlite",".sqlite3",".log",".wav",".mp3",".etl",".dmp",".pyc",".pyo")
                    or name.startswith(".env") or name == "api_credentials.json" or rel in patterns.PRIVATE_FILES
                    or any(part in ("__pycache__",".pytest_cache","node_modules","publication-export","unit-scratch","screenshots","captures")
                           or part.startswith(".venv") for part in path.relative_to(ROOT).parts)):
                findings.append({"file": rel, "category": "private-or-generated-file"})
            data = path.read_bytes()
            if path.suffix.lower() in (".gz",".7z",".tar",".rar") or (
                    path.suffix.lower() in (".whl",".zip") and not zipfile.is_zipfile(io.BytesIO(data))):
                findings.append({"file": rel, "category": "unsupported-archive"})
            inspect(data, rel)
    objects = [row.split() for row in git("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").decode().splitlines()]
    commits = {oid for oid, kind in objects if kind == "commit"}
    overlap = sorted(commits.intersection(old_commits))
    names = {}
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            names[metadata.decode().split()[1]] = name.decode()
    if commits:
        for row in git("rev-list", "--objects", "--all", "--reflog").decode().splitlines():
            oid, _, name = row.partition(" ")
            if name:
                names[oid] = name
    selected = [(oid,kind) for oid,kind in objects if kind in ("blob","commit","tag")]
    stream = io.BytesIO(git("cat-file", "--batch", input="".join(oid+"\n" for oid,_ in selected).encode()))
    for oid,kind in selected:
        header = stream.readline().decode().split()
        assert header[:2] == [oid,kind]
        data = stream.read(int(header[2]))
        assert stream.read(1) == b"\n"
        name = names.get(oid, "["+kind+"]")
        inspect(data, name, oid)
        if name in patterns.PRIVATE_FILES:
            findings.append({"file": name, "object": oid, "category": "private-history"})
    if (ROOT/".git/objects/info/alternates").exists():
        findings.append({"file": ".git/objects/info/alternates", "category": "external-object-store"})
    inspect((ROOT/".git/config").read_bytes(), ".git/config")
    return {"values_exported":False, "worktree_files":len(files), "all_git_objects":len(objects),
            "commits":len(commits), "old_commits_present":overlap, "findings":findings,
            "reviewed_synthetic_fixtures":fixtures, "passed":not findings and not overlap,
            "limits":"Explicit patterns, archive and file review. Unknown secret formats and arbitrary private prose require human review. GitHub retained objects are outside this scan."}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-commits",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=ROOT/"packaging/publication-scan.json")
    args=parser.parse_args()
    report=scan(json.loads(args.old_commits.read_text()))
    args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report))
    raise SystemExit(0 if report["passed"] else 1)
