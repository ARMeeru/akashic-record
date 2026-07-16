#!/usr/bin/env python3
"""akashic-record deterministic core: scan / stale / verify / anchor.

The LLM side of akashic-record (catalog planning, page prose) lives in SKILL.md.
This script owns everything that must never be hallucinated: file scanning, the
diff -> stale-page mapping, citation verification, and hash/anchor stamping.
It never calls an LLM; the LLM never computes a hash, diff, or line range.

Stdlib only, Python 3.9+. Format spec: DESIGN.md.

Exit codes: 0 ok, 1 verification failures, 2 usage/precondition errors.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

AKASHIC_DIR = ".akashic"
CATALOG_NAME = "catalog.json"
WIKI_DIRNAME = "wiki"
DEFAULT_MAX_FILES = 5000

# Committed-but-noisy files we never feed the planner. Small on purpose:
# `git ls-files` already excludes everything gitignored.
LOCKFILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "bun.lockb", "npm-shrinkwrap.json",
    "Pipfile.lock", "go.sum",
}
NOISE_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", ".svg")
VENDOR_DIRS = {"node_modules", "vendor", "third_party"}

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SOURCES_RE = re.compile(r"^Sources:\s*(.*)$")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
FRAGMENT_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
VALID_STATUS = {"planned", "done"}


def die(message, code=2):
    print(f"akashic: error: {message}", file=sys.stderr)
    sys.exit(code)


def warn(message):
    print(f"akashic: warning: {message}", file=sys.stderr)


def git(root, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, errors="surrogateescape",
    )
    if check and result.returncode != 0:
        die(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def repo_root(path):
    """Resolve the git repo root and require >=1 commit (the anchor model needs it)."""
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        die(f"{path} is not inside a git repository. akashic-record requires one.")
    root = Path(result.stdout.strip()).resolve()
    if git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0:
        die("repository has no commits yet; make an initial commit first "
            "(incremental updates anchor to commits).")
    return root


def catalog_path(root):
    return root / AKASHIC_DIR / CATALOG_NAME


def wiki_dir(root):
    return root / AKASHIC_DIR / WIKI_DIRNAME


def load_catalog(root, required=True):
    path = catalog_path(root)
    if not path.is_file():
        if required:
            die(f"{path.relative_to(root)} not found; run the plan phase first.")
        return None
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        die(f"{path.relative_to(root)} is not valid JSON: {exc}")
    if not isinstance(catalog, dict) or catalog.get("version") != 1:
        die(f"{path.relative_to(root)}: unsupported or missing \"version\" (expected 1)")
    if not isinstance(catalog.get("pages"), list):
        die(f"{path.relative_to(root)}: \"pages\" must be a list")
    for i, page in enumerate(catalog["pages"]):
        if not isinstance(page, dict):
            die(f"{path.relative_to(root)}: pages[{i}] is not an object")
        for key in ("id", "title", "goal"):
            if not isinstance(page.get(key), str) or not page.get(key):
                die(f"{path.relative_to(root)}: pages[{i}] missing string \"{key}\"")
    return catalog


def save_catalog(root, catalog):
    path = catalog_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def matches_any(path, patterns):
    """fnmatch-style globs; a slash-free pattern also matches the basename
    at any depth (so \"*.snap\" behaves like gitignore users expect)."""
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False


def tracked_files(root, at_head=False):
    if at_head:
        out = git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout
    else:
        out = git(root, "ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


def normalize_body(data):
    """Normalization for edit-detection hashes (see DESIGN.md 3.2):
    CRLF/CR -> LF, strip trailing whitespace per line, strip trailing newlines."""
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).rstrip("\n").encode("utf-8")


def page_hash(page_file):
    return "sha256:" + hashlib.sha256(normalize_body(page_file.read_bytes())).hexdigest()


def line_count(data):
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def is_binary(path):
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return None  # unreadable / deleted from working tree


# --------------------------------------------------------------------------- scan

def scan_repo(root, catalog=None):
    """Filtered file list with line counts -- the planner's view of the repo."""
    excludes = list((catalog or {}).get("exclude", []))
    max_files = (catalog or {}).get("max_files", DEFAULT_MAX_FILES)
    entries = []
    for path in tracked_files(root):
        parts = path.split("/")
        if any(part in VENDOR_DIRS for part in parts):
            continue
        name = parts[-1]
        if name in LOCKFILE_NAMES or name.endswith(NOISE_SUFFIXES):
            continue
        if path.startswith(AKASHIC_DIR + "/"):
            continue
        if excludes and matches_any(path, excludes):
            continue
        binary = is_binary(root / path)
        if binary or binary is None:
            continue
        entries.append((path, line_count((root / path).read_bytes())))
    if len(entries) > max_files:
        die(f"{len(entries)} files exceed max_files={max_files}; add \"exclude\" "
            f"globs to {AKASHIC_DIR}/{CATALOG_NAME} to scope the wiki.")
    entries.sort()
    return entries


def cmd_scan(root):
    entries = scan_repo(root, load_catalog(root, required=False))
    print(f"# {len(entries)} files, {sum(n for _, n in entries)} lines")
    for path, lines in entries:
        print(f"{lines}\t{path}")
    return 0


# ------------------------------------------------------------------- citations

def parse_page_links(body_text):
    """Return (sources_count, citations, wiki_links).

    citations: (lineno, raw_target) from `Sources:` lines.
    wiki_links: (lineno, raw_target) for other .md links (cross-references).
    """
    sources_count = 0
    citations = []
    wiki_links = []
    for lineno, line in enumerate(body_text.split("\n"), start=1):
        is_sources = bool(SOURCES_RE.match(line))
        if is_sources:
            sources_count += 1
        for match in LINK_RE.finditer(line):
            target = match.group(2)
            if is_sources:
                citations.append((lineno, target))
            elif not SCHEME_RE.match(target) and not target.startswith("#") \
                    and target.split("#")[0].endswith(".md"):
                wiki_links.append((lineno, target))
    return sources_count, citations, wiki_links


def resolve_citation(root, page_file, raw_target):
    """Split a citation target into (repo-relative posix path, fragment, error)."""
    target, _, fragment = raw_target.partition("#")
    target = unquote(target)
    if SCHEME_RE.match(raw_target):
        return None, None, "URL schemes are not allowed in Sources (cite repo files)"
    if target.startswith("/"):
        return None, None, "absolute paths are not allowed; use paths relative to the page"
    resolved = (page_file.parent / target).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None, None, f"resolves outside repository: {target}"
    return rel.as_posix(), fragment, None


def check_fragment(fragment, file_data):
    if not fragment:
        return None
    match = FRAGMENT_RE.match(fragment)
    if not match:
        return f"invalid line fragment \"#{fragment}\" (expected #L<start> or #L<start>-L<end>)"
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    total = line_count(file_data)
    if start < 1 or start > end:
        return f"invalid line range #{fragment}"
    if end > total:
        return f"line range #{fragment} exceeds file length ({total} lines)"
    return None


# ---------------------------------------------------------------------- verify

def verify_repo(root):
    """The deterministic QA gate. Returns a list of error strings (empty = pass)."""
    catalog = load_catalog(root)
    errors = []
    pages = catalog["pages"]
    ids = [p["id"] for p in pages]
    by_id = {}

    for page in pages:
        pid = page["id"]
        if pid in by_id:
            errors.append(f"catalog: duplicate page id \"{pid}\"")
        by_id[pid] = page
        if not SLUG_RE.match(pid):
            errors.append(f"catalog: page id \"{pid}\" is not a kebab-case slug")
        if page.get("status", "planned") not in VALID_STATUS:
            errors.append(f"catalog: page \"{pid}\" has invalid status "
                          f"\"{page.get('status')}\"")

    for page in pages:
        parent, seen = page.get("parent"), {page["id"]}
        while parent:
            if parent not in by_id:
                errors.append(f"catalog: page \"{page['id']}\" has unknown parent "
                              f"\"{parent}\"")
                break
            if parent in seen:
                errors.append(f"catalog: parent cycle involving \"{parent}\"")
                break
            seen.add(parent)
            parent = by_id[parent].get("parent")

    wiki = wiki_dir(root)
    for page in pages:
        if page.get("status") != "done":
            continue
        pid = page["id"]
        page_file = wiki / f"{pid}.md"
        rel = page_file.relative_to(root).as_posix()
        if not page_file.is_file():
            errors.append(f"{pid}: {rel} does not exist")
            continue
        body = page_file.read_text(encoding="utf-8", errors="replace")

        first = next((ln for ln in body.split("\n") if ln.strip()), "")
        if not first.startswith("# "):
            errors.append(f"{pid}: {rel}: first non-empty line must be an H1 title")

        sources_count, citations, wiki_links = parse_page_links(body)
        if sources_count == 0:
            errors.append(f"{pid}: {rel}: no `Sources:` line (cite-or-omit; every page "
                          "cites at least one repo file)")

        for lineno, raw in citations:
            path, fragment, err = resolve_citation(root, page_file, raw)
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {err}")
                continue
            cited = root / path
            if not cited.is_file():
                errors.append(f"{pid}: {rel} line {lineno}: cited file not found: {path}")
                continue
            err = check_fragment(fragment, cited.read_bytes())
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {path}: {err}")

        for lineno, raw in wiki_links:
            path, _, err = resolve_citation(root, page_file, raw)
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {err}")
                continue
            target = root / path
            if wiki.resolve() in target.parents and not target.is_file():
                errors.append(f"{pid}: {rel} line {lineno}: broken wiki link: {raw}")

    if wiki.is_dir():
        known = {f"{p['id']}.md" for p in pages} | {"README.md"}
        for extra in sorted(f.name for f in wiki.glob("*.md") if f.name not in known):
            warn(f"{WIKI_DIRNAME}/{extra} is not in the catalog (left untouched)")
    return errors


def cmd_verify(root):
    errors = verify_repo(root)
    for error in errors:
        print(error)
    if errors:
        print(f"verify: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("verify: ok")
    return 0


# ----------------------------------------------------------------------- stale

def parse_name_status(output):
    """Parse `git diff --name-status -z` into (modified, added, deleted, renames)."""
    tokens = [t for t in output.split("\0") if t]
    modified, added, deleted, renames = set(), set(), set(), []
    i = 0
    while i < len(tokens):
        status = tokens[i][0]
        if status in ("R", "C"):
            old, new = tokens[i + 1], tokens[i + 2]
            i += 3
            if status == "R":
                renames.append((old, new))
            else:
                added.add(new)
        else:
            path = tokens[i + 1]
            i += 2
            if status == "A":
                added.add(path)
            elif status == "D":
                deleted.add(path)
            else:  # M, T, and anything else counts as a content change
                modified.add(path)
    return modified, added, deleted, renames


def compute_stale(root):
    """Read-only staleness report: which pages need what, per DESIGN.md section 5."""
    catalog = load_catalog(root)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    anchor = catalog.get("anchor")
    dirty = bool(git(root, "status", "--porcelain").stdout.strip())
    done = [p for p in catalog["pages"] if p.get("status") == "done"]
    wiki = wiki_dir(root)

    report = {
        "anchor": anchor, "head": head, "anchor_reachable": True, "dirty": dirty,
        "stale": [], "edited": [], "orphaned": [], "uncovered": [], "missing": [],
    }

    # Edit detection is independent of the diff: hash of what exists now vs
    # the recorded hash of what the tool last wrote. Never guessed from git.
    for page in done:
        page_file = wiki / f"{page['id']}.md"
        if not page_file.is_file():
            report["missing"].append(page["id"])
        elif page.get("hash") and page_hash(page_file) != page["hash"]:
            report["edited"].append(page["id"])

    reachable = bool(anchor) and git(
        root, "cat-file", "-e", f"{anchor}^{{commit}}", check=False
    ).returncode == 0
    if not reachable:
        # Never guess: an unreachable/absent anchor means everything regenerates.
        # Wasting a regen is acceptable; marking stale content fresh is not.
        report["anchor_reachable"] = False
        report["stale"] = [{"id": p["id"], "changed": []} for p in done]
        return report

    diff = git(root, "diff", "--name-status", "-M", "-z", anchor, "HEAD").stdout
    modified, added, deleted, renames = parse_name_status(diff)

    # Wiki artifacts must never be staleness inputs: the wiki depending on
    # itself would break the post-anchor invariant the moment it is committed.
    def outside_akashic(paths):
        return {p for p in paths if not p.startswith(AKASHIC_DIR + "/")}

    rename_paths = {o for o, _ in renames} | {n for _, n in renames}
    changed = outside_akashic(modified | deleted | rename_paths)
    added_now = outside_akashic(added | {n for _, n in renames})
    deleted = outside_akashic(deleted)
    head_files = outside_akashic(tracked_files(root, at_head=True))

    for page in done:
        files = set(page.get("files", []))
        scope = page.get("scope", [])
        hits = sorted(files & changed)
        hits += sorted(p for p in added_now
                       if p not in files and scope and matches_any(p, scope))
        # Orphaned only when everything documented is gone AND nothing in the
        # current tree matches scope -- a rewritten module is stale, not orphaned.
        if files and files <= deleted and not any(
                matches_any(p, scope) for p in head_files if scope):
            report["orphaned"].append(page["id"])
        elif hits:
            report["stale"].append({"id": page["id"], "changed": hits})

    covered = []
    for page in catalog["pages"]:
        covered.extend(page.get("files", []))
        covered.extend(page.get("scope", []))
    report["uncovered"] = sorted(p for p in added_now if not matches_any(p, covered))
    return report


def cmd_stale(root):
    print(json.dumps(compute_stale(root), indent=2))
    return 0


# ---------------------------------------------------------------------- anchor

def render_readme(root, catalog, head):
    """Derived TOC -- rebuilt on every anchor so it can never drift from the catalog."""
    children = {}
    for page in catalog["pages"]:
        children.setdefault(page.get("parent"), []).append(page)

    lines = [f"# {root.name} — Wiki", ""]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines += [f"*Generated from commit `{head[:7]}` on {date} by akashic-record. "
              "This file is derived from `../catalog.json`; edit pages, not this TOC.*", ""]

    def emit(parent, depth):
        for page in children.get(parent, []):
            indent = "  " * depth
            if page.get("status") == "done":
                lines.append(f"{indent}- [{page['title']}](./{page['id']}.md)")
            else:
                lines.append(f"{indent}- {page['title']} *(planned)*")
            emit(page["id"], depth + 1)

    emit(None, 0)
    lines.append("")
    return "\n".join(lines)


def anchor_repo(root):
    """The only metadata mutation point: verify, then record files/hashes/anchor."""
    errors = verify_repo(root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        die(f"refusing to anchor: {len(errors)} verification error(s)", code=1)

    catalog = load_catalog(root)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    # .akashic/ files can never be page dependencies (see compute_stale).
    tracked = [p for p in tracked_files(root)
               if not p.startswith(AKASHIC_DIR + "/")]
    wiki = wiki_dir(root)

    anchored = 0
    for page in catalog["pages"]:
        if page.get("status") != "done":
            continue
        page_file = wiki / f"{page['id']}.md"
        body = page_file.read_text(encoding="utf-8", errors="replace")
        _, citations, _ = parse_page_links(body)
        cited = set()
        for _, raw in citations:
            path, _, err = resolve_citation(root, page_file, raw)
            if not err and not path.startswith(AKASHIC_DIR + "/"):
                cited.add(path)
        scope = page.get("scope", [])
        in_scope = {p for p in tracked if scope and matches_any(p, scope)}
        page["files"] = sorted(cited | in_scope)
        page["hash"] = page_hash(page_file)
        anchored += 1

    catalog["anchor"] = head
    catalog["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (wiki / "README.md").write_text(render_readme(root, catalog, head), encoding="utf-8")
    save_catalog(root, catalog)

    if git(root, "status", "--porcelain").stdout.strip():
        warn("working tree is dirty; the wiki may describe uncommitted code "
             "(recommended flow: commit, then anchor)")
    return {"anchor": head, "pages": anchored}


def cmd_anchor(root):
    result = anchor_repo(root)
    print(f"anchored {result['pages']} page(s) at {result['anchor'][:7]}")
    return 0


# ------------------------------------------------------------------------ main

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="akashic",
        description="Deterministic core of akashic-record (see DESIGN.md).")
    parser.add_argument("-C", dest="path", default=".",
                        help="run as if started in this directory (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="filtered file tree with line counts (planner input)")
    sub.add_parser("stale", help="JSON report: stale/edited/orphaned/uncovered pages")
    sub.add_parser("verify", help="check pages, citations, and catalog invariants")
    sub.add_parser("anchor", help="record files/hashes, stamp anchor commit, render TOC")
    args = parser.parse_args(argv)

    root = repo_root(args.path)
    command = {"scan": cmd_scan, "stale": cmd_stale,
               "verify": cmd_verify, "anchor": cmd_anchor}[args.command]
    return command(root)


if __name__ == "__main__":
    sys.exit(main())
