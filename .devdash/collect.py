#!/usr/bin/env python3
"""devdash collector — deterministic project facts, zero LLM, stdlib only.

Lives at <repo>/.devdash/collect.py. Writes metrics.json, graph.json and a
bundled devdash-data.js next to itself. Never touches components.json or
decisions.md (those are agent-maintained) except to bundle them for the
dashboard.

Run:  python .devdash/collect.py
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

# ---------------------------------------------------------------- config
DEVDASH_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEVDASH_DIR.parent

GIT_LOG_DEPTH = 300          # commits to scan for churn / co-change / trend
COCHANGE_MIN = 2             # min shared commits to report a pair
COCHANGE_TOP = 25
HOTSPOTS_TOP = 15
TREND_POINTS = 30            # sparkline resolution
TODO_TOP = 100               # max TODO/FIXME entries reported
DEP_TOP = 250                # max dependency entries reported
TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX", "BUG")
AI_COMMIT_MARKERS = ("claude", "co-authored-by: claude", "[ai]", "🤖")

EXCLUDE_DIRS = {
    ".git", ".devdash", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "target", ".idea", ".vscode", "vendor",
    ".pytest_cache", "coverage", ".mypy_cache", ".gradle", "out",
}

# extension -> language
LANG = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".c": "C", ".h": "C/C++ Header", ".hpp": "C++ Header", ".swift": "Swift",
    ".scala": "Scala", ".sh": "Shell", ".bash": "Shell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".vue": "Vue",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".md": "Markdown", ".lua": "Lua", ".r": "R", ".dart": "Dart",
}
# which languages take part in the import graph
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go",
            ".rs", ".rb", ".php", ".cs", ".cpp", ".cc", ".cxx", ".c",
            ".h", ".hpp", ".swift", ".scala", ".vue"}
# files scanned for TODO/FIXME markers
TODO_EXT = CODE_EXT | {".sh", ".bash", ".sql", ".html", ".css", ".scss",
                       ".lua", ".r", ".dart"}

# import-ish patterns, applied line by line; group 1 = the referenced token
IMPORT_PATTERNS = [
    re.compile(r"""^\s*from\s+([.\w/]+)\s+import""")        ,  # python
    re.compile(r"""^\s*import\s+([.\w]+)""")                ,  # python/java/go
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")    ,  # node
    re.compile(r"""from\s+['"]([^'"]+)['"]""")             ,  # js/ts es-module
    re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""")        ,  # js side-effect
    re.compile(r"""^\s*use\s+([\w:]+)""")                  ,  # rust/php
    re.compile(r"""^\s*#include\s+["<]([^">]+)[">]""")      ,  # c/c++
]

TODO_RE = re.compile(
    r"(?:#|//|/\*|<!--|--|;|\*)?\s*\b(" + "|".join(TODO_MARKERS) +
    r")\b[:\s-]*(.*)")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def git(*args):
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def is_git_repo():
    return git("rev-parse", "--is-inside-work-tree") is not None


# ------------------------------------------------------------- file walk
def walk_files():
    files = []
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and
                   not d.startswith(".") or d in (".github",)]
        for n in names:
            p = Path(root) / n
            rel = p.relative_to(REPO_ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            files.append(rel)
    return files


def count_lines(path):
    try:
        with open(REPO_ROOT / path, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


# ------------------------------------------------------------- inventory
def build_inventory(files):
    by_lang = defaultdict(lambda: {"files": 0, "lines": 0})
    line_counts = {}
    total_files = total_lines = 0
    for rel in files:
        ext = rel.suffix.lower()
        lang = LANG.get(ext)
        if lang is None:
            continue
        lines = count_lines(rel)
        line_counts[str(rel)] = lines
        by_lang[lang]["files"] += 1
        by_lang[lang]["lines"] += lines
        total_files += 1
        total_lines += lines
    breakdown = sorted(
        ({"lang": k, "files": v["files"], "lines": v["lines"]}
         for k, v in by_lang.items()),
        key=lambda x: x["lines"], reverse=True)
    return {"files": total_files, "lines": total_lines}, breakdown, line_counts


# ----------------------------------------------------------------- graph
def build_graph(files, line_counts):
    code_files = [f for f in files if f.suffix.lower() in CODE_EXT]
    # index for best-effort resolution: basename(no ext) -> [relpaths]
    by_stem = defaultdict(list)
    relset = set(str(f) for f in code_files)
    for f in code_files:
        by_stem[f.stem].append(str(f))

    edges = set()
    for f in code_files:
        importer = str(f)
        try:
            text = (REPO_ROOT / f).read_text(errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            for pat in IMPORT_PATTERNS:
                m = pat.match(line) if pat.pattern.startswith("^") else pat.search(line)
                if not m:
                    continue
                target = resolve(m.group(1), f, relset, by_stem)
                if target and target != importer:
                    edges.add((importer, target))
                break  # one pattern per line is enough

    fan_in = defaultdict(int)
    fan_out = defaultdict(int)
    for a, b in edges:
        fan_out[a] += 1
        fan_in[b] += 1

    nodes = []
    for f in code_files:
        s = str(f)
        nodes.append({"id": s, "lang": LANG.get(f.suffix.lower(), "?"),
                      "lines": line_counts.get(s, 0),
                      "fan_in": fan_in[s], "fan_out": fan_out[s]})
    orphans = sorted(n["id"] for n in nodes
                     if n["fan_in"] == 0 and n["fan_out"] == 0)
    edge_list = [{"from": a, "to": b} for a, b in sorted(edges)]
    return nodes, edge_list, orphans


def resolve(token, importer, relset, by_stem):
    """Best-effort map an import token to a repo file. Returns relpath or None."""
    token = token.strip()
    # relative path import (js/ts/c)
    if token.startswith(".") or "/" in token:
        base = (importer.parent / token).as_posix()
        norm = os.path.normpath(base)
        for ext in ("", ".py", ".js", ".jsx", ".ts", ".tsx", ".vue",
                    "/index.js", "/index.ts"):
            cand = norm + ext
            if cand in relset:
                return cand
    # dotted path a.b.c -> match by tail
    parts = re.split(r"[./:\\]", token)
    parts = [p for p in parts if p]
    if parts:
        stem = parts[-1]
        hits = by_stem.get(stem, [])
        if len(hits) == 1:
            return hits[0]
        tail = "/".join(parts)
        for r in relset:
            noext = r.rsplit(".", 1)[0]
            if noext.endswith(tail):
                return r
    return None


# --------------------------------------------------------------- todos
def scan_todos(files):
    out = []
    counts = defaultdict(int)
    for f in files:
        if f.suffix.lower() not in TODO_EXT:
            continue
        try:
            with open(REPO_ROOT / f, "r", errors="ignore") as fh:
                for ln, line in enumerate(fh, 1):
                    if len(line) > 400:
                        continue
                    m = TODO_RE.search(line)
                    if m:
                        kind = m.group(1).upper()
                        text = m.group(2).strip()[:160]
                        counts[kind] += 1
                        if len(out) < TODO_TOP:
                            out.append({"path": str(f), "line": ln,
                                        "kind": kind, "text": text})
        except Exception:
            continue
    return out, dict(counts)


# --------------------------------------------------------- dependencies
def _toml_deps(path, tables):
    """Best-effort: pull dep names from tables using tomllib if available."""
    try:
        import tomllib
    except Exception:
        return []
    try:
        data = tomllib.loads((REPO_ROOT / path).read_text(errors="ignore"))
    except Exception:
        return []
    deps = []
    for table in tables:
        node = data
        ok = True
        for key in table.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if not ok:
            continue
        if isinstance(node, dict):
            for name, ver in node.items():
                if name.lower() == "python":
                    continue
                deps.append((name, str(ver) if not isinstance(ver, dict)
                             else ver.get("version", "")))
        elif isinstance(node, list):
            for item in node:
                deps.append((str(item), ""))
    return deps


def scan_dependencies(files):
    out = []
    found = defaultdict(list)
    for f in files:
        found[f.name].append(f)

    def add(manifest, name, ver, dev=False):
        out.append({"manifest": manifest, "name": str(name),
                    "version": str(ver or ""), "dev": dev})

    for f in found.get("package.json", []):
        try:
            pj = json.loads((REPO_ROOT / f).read_text(errors="ignore"))
            for n, v in (pj.get("dependencies") or {}).items():
                add(str(f), n, v, False)
            for n, v in (pj.get("devDependencies") or {}).items():
                add(str(f), n, v, True)
        except Exception:
            pass
    for f in found.get("requirements.txt", []):
        try:
            for line in (REPO_ROOT / f).read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                mm = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
                if mm:
                    add(str(f), mm.group(1), mm.group(2).strip(), False)
        except Exception:
            pass
    for f in found.get("pyproject.toml", []):
        for n, v in _toml_deps(f, ["project.dependencies",
                                   "tool.poetry.dependencies"]):
            add(str(f), n, v, False)
    for f in found.get("Cargo.toml", []):
        for n, v in _toml_deps(f, ["dependencies"]):
            add(str(f), n, v, False)
    for f in found.get("go.mod", []):
        try:
            for line in (REPO_ROOT / f).read_text(errors="ignore").splitlines():
                mm = re.match(r"^\s*([\w./\-]+)\s+v([\d][\w.\-]*)", line)
                if mm and "module " not in line and "go 1" not in line:
                    add(str(f), mm.group(1), "v" + mm.group(2), False)
        except Exception:
            pass
    return out[:DEP_TOP]


# ------------------------------------------------------------- git stats
def git_stats(line_counts):
    stats = {"indexed_commit": None, "hotspots": [], "cochange": [],
             "loc_trend": [], "ai_commits": None, "human_commits": None,
             "file_mtime": {}}
    if not is_git_repo():
        return stats
    head = git("rev-parse", "--short", "HEAD")
    stats["indexed_commit"] = head.strip() if head else None

    log = git("log", f"-{GIT_LOG_DEPTH}", "--name-only",
              "--pretty=format:__C__%cI")
    if not log:
        return stats
    commits = []
    cur = None
    cur_date = None
    mtime = {}
    for line in log.splitlines():
        if line.startswith("__C__"):
            if cur is not None:
                commits.append(cur)
            cur = set()
            cur_date = line[5:].strip()
        elif line.strip() and cur is not None:
            f = line.strip()
            cur.add(f)
            if f not in mtime and cur_date:
                mtime[f] = cur_date          # newest-first log => first seen = latest
    if cur:
        commits.append(cur)
    stats["file_mtime"] = mtime

    churn = defaultdict(int)
    for c in commits:
        for f in c:
            churn[f] += 1
    hot = []
    for f, ch in churn.items():
        if f in line_counts and ch >= 2:
            hot.append({"path": f, "churn": ch,
                        "lines": line_counts[f],
                        "score": ch * line_counts[f]})
    hot.sort(key=lambda x: x["score"], reverse=True)
    stats["hotspots"] = hot[:HOTSPOTS_TOP]

    pair = defaultdict(int)
    for c in commits:
        tracked = sorted(f for f in c if f in line_counts)
        if 2 <= len(tracked) <= 30:
            for a, b in combinations(tracked, 2):
                pair[(a, b)] += 1
    co = [{"a": a, "b": b, "count": n}
          for (a, b), n in pair.items() if n >= COCHANGE_MIN]
    co.sort(key=lambda x: x["count"], reverse=True)
    stats["cochange"] = co[:COCHANGE_TOP]

    num = git("log", f"-{GIT_LOG_DEPTH}", "--numstat", "--reverse",
              "--pretty=format:__C__")
    running = 0
    series = []
    if num:
        for line in num.splitlines():
            if line.startswith("__C__"):
                series.append(running)
                continue
            m = re.match(r"^(\d+|-)\t(\d+|-)\t", line)
            if m:
                add = int(m.group(1)) if m.group(1).isdigit() else 0
                dele = int(m.group(2)) if m.group(2).isdigit() else 0
                running += add - dele
        series.append(running)
    if len(series) > TREND_POINTS:
        step = len(series) / TREND_POINTS
        series = [series[int(i * step)] for i in range(TREND_POINTS)]
    stats["loc_trend"] = series

    authors = git("log", f"-{GIT_LOG_DEPTH}",
                  "--pretty=format:%an|%b__END__") or ""
    ai = human = 0
    for entry in authors.split("__END__"):
        if not entry.strip():
            continue
        low = entry.lower()
        if any(mk in low for mk in AI_COMMIT_MARKERS):
            ai += 1
        else:
            human += 1
    if ai:
        stats["ai_commits"], stats["human_commits"] = ai, human
    return stats


# --------------------------------------------------------------- tests
def is_test_file(rel):
    parts = [p.lower() for p in rel.parts[:-1]]
    if any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts):
        return True
    name = rel.name.lower()
    stem = rel.stem.lower()
    return (name.startswith("test_") or stem.endswith("_test") or
            ".test." in name or ".spec." in name or stem.endswith("_spec") or
            stem.endswith(".test") or stem.endswith(".spec"))


def subject_stem(rel):
    s = rel.stem.lower()
    s = re.sub(r"^test_", "", s)
    s = re.sub(r"_test$|_spec$", "", s)
    s = s.replace(".test", "").replace(".spec", "")
    return s


def scan_tests(files):
    tfiles = []
    tested = set()
    for f in files:
        if f.suffix.lower() not in CODE_EXT:
            continue
        if is_test_file(f):
            tfiles.append(str(f))
            tested.add(subject_stem(f))
    return {"count": len(tfiles), "files": tfiles[:200],
            "tested_stems": sorted(s for s in tested if s)}


# ------------------------------------------------------------------ main
def read_optional(name):
    p = DEVDASH_DIR / name
    if p.exists():
        try:
            return p.read_text()
        except Exception:
            return None
    return None


def main():
    files = walk_files()
    totals, by_lang, line_counts = build_inventory(files)
    nodes, edges, orphans = build_graph(files, line_counts)
    gs = git_stats(line_counts)
    todos, todo_counts = scan_todos(files)
    deps = scan_dependencies(files)
    tests = scan_tests(files)

    # read previous run for the diff panel (before we overwrite metrics.json)
    old = None
    mp = DEVDASH_DIR / "metrics.json"
    if mp.exists():
        try:
            old = json.loads(mp.read_text())
        except Exception:
            old = None
    file_index = sorted(line_counts.keys())
    diff = None
    if old and isinstance(old.get("file_index"), list):
        old_set, new_set = set(old["file_index"]), set(file_index)
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed or \
                old.get("totals", {}).get("lines") != totals["lines"]:
            diff = {"since": old.get("generated_at"),
                    "added": added[:100], "removed": removed[:100],
                    "added_count": len(added), "removed_count": len(removed),
                    "lines_delta": totals["lines"] -
                    old.get("totals", {}).get("lines", 0)}

    git_mtime = {k: v for k, v in gs.get("file_mtime", {}).items()
                 if k in line_counts}

    metrics = {
        "generated_at": now_iso(),
        "project": REPO_ROOT.name or "project",
        "indexed_commit": gs["indexed_commit"],
        "totals": totals,
        "by_language": by_lang,
        "loc_trend": gs["loc_trend"],
        "hotspots": gs["hotspots"],
        "cochange": gs["cochange"],
        "orphans": orphans,
        "todos": todos,
        "todo_counts": todo_counts,
        "dependencies": deps,
        "tests": tests,
        "git_mtime": git_mtime,
        "file_index": file_index,
    }
    if diff:
        metrics["diff"] = diff
    if gs["ai_commits"] is not None:
        metrics["ai_commits"] = gs["ai_commits"]
        metrics["human_commits"] = gs["human_commits"]

    graph = {"generated_at": metrics["generated_at"],
             "indexed_commit": gs["indexed_commit"],
             "nodes": nodes, "edges": edges}

    (DEVDASH_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (DEVDASH_DIR / "graph.json").write_text(json.dumps(graph, indent=2))

    components_raw = read_optional("components.json")
    try:
        components = json.loads(components_raw) if components_raw else []
    except Exception:
        components = []
    bundle = {
        "metrics": metrics, "graph": graph,
        "components": components,
        "decisions_md": read_optional("decisions.md") or "",
        "roadmap_md": read_optional("roadmap.md") or "",
    }
    (DEVDASH_DIR / "devdash-data.js").write_text(
        "window.DEVDASH = " + json.dumps(bundle, indent=2) + ";\n")

    print(f"devdash: {totals['files']} files, {totals['lines']} lines, "
          f"{len(nodes)} graph nodes, {len(edges)} edges, "
          f"{len(orphans)} orphans, {len(gs['cochange'])} co-change pairs, "
          f"{len(todos)} todos, {len(deps)} deps, "
          f"{tests['count']} test files, commit {gs['indexed_commit']}")


if __name__ == "__main__":
    sys.exit(main())
