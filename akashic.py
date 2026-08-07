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
# Non-greedy text group, not [^\]]* -- link text is often a literal file path,
# and dynamic-route frameworks (Next.js "[id]/route.ts") put unescaped "]"
# characters inside that path. [^\]]* stops at the first one and never finds
# the real "](" delimiter, silently reporting zero links for a valid citation.
# The destination alternates CommonMark's <...> wrapper (stripped in
# resolve_citation) with a bare form allowing ONE level of balanced
# parentheses: framework route groups name path segments that way (Next.js
# "api/(cron)/route.ts"), and a plain [^)\s]+ truncates at the first ")".
# That truncation is the dangerous kind -- the shortened prefix still
# resolves to a path, so resolve_citation returns no error and the citation
# fails verification as "not tracked by git" instead of parsing correctly.
LINK_RE = re.compile(r"\[(.*?)\]\((<[^<>]*>|(?:[^()\s]|\([^()\s]*\))+)\)")
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
    rel = path.relative_to(root)
    if not isinstance(catalog.get("exclude", []), list) or not all(
            isinstance(x, str) for x in catalog.get("exclude", [])):
        die(f"{rel}: \"exclude\" must be a list of strings")
    if not isinstance(catalog.get("max_files", DEFAULT_MAX_FILES), int):
        die(f"{rel}: \"max_files\" must be an integer")
    for i, page in enumerate(catalog["pages"]):
        if not isinstance(page, dict):
            die(f"{rel}: pages[{i}] is not an object")
        for key in ("id", "title", "goal"):
            if not isinstance(page.get(key), str) or not page.get(key):
                die(f"{rel}: pages[{i}] missing string \"{key}\"")
        # ids are path components in every command; a non-slug id is a
        # traversal vector, so this is a hard trust-boundary check.
        if not SLUG_RE.match(page["id"]):
            die(f"{rel}: pages[{i}] id \"{page['id']}\" is not a kebab-case slug")
        for key in ("scope", "files"):
            value = page.get(key, [])
            if not isinstance(value, list) or not all(
                    isinstance(x, str) for x in value):
                die(f"{rel}: pages[{i}].{key} must be a list of strings")
        if page.get("parent") is not None and not isinstance(page["parent"], str):
            die(f"{rel}: pages[{i}].parent must be a string or null")
        blobs = page.get("blobs", {})
        if not isinstance(blobs, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in blobs.items()):
            die(f"{rel}: pages[{i}].blobs must be an object of path -> sha")
    return catalog


def save_catalog(root, catalog):
    path = catalog_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def escape_brackets(pattern):
    """Make \"[\" literal for fnmatch, so character classes are NOT supported
    in scope/exclude globs. Framework routing conventions put literal brackets
    in path segments (Next.js \"[id]/route.ts\") far more often than a catalog
    wants a one-character class, and reading \"[id]\" as a class made the
    natural glob for such a path match nothing -- silently: files vanished
    from a page's citable set, files added under the scope never marked the
    page stale (marking stale content fresh is the one thing this tool must
    never do), and the rescue clause that keeps a rewritten module from being
    called `orphaned` failed, producing a false positive whose documented
    remediation deletes the page. \"*\" and \"?\" keep their glob meaning; a
    lone \"]\" is already literal to fnmatch."""
    return pattern.replace("[", "[[]")


def matches_any(path, patterns):
    """fnmatch-style globs with two gitignore-flavored affordances: a slash-free
    pattern also matches the basename at any depth (\"*.snap\"), and a \"**/\"
    prefix also matches at the root (\"**/*.snap\" matches \"top.snap\" —
    plain fnmatch would require a slash). Literal brackets survive matching
    (see escape_brackets); slicing after escaping is safe because escaping
    never touches the \"**/\" prefix."""
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        escaped = escape_brackets(pattern)
        if fnmatch.fnmatch(path, escaped):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, escaped[3:]):
            return True
        if "/" not in pattern and fnmatch.fnmatch(name, escaped):
            return True
    return False


def is_noise(path, excludes):
    """The scan-time filter, shared with `uncovered` so stale never reports
    files the wiki is configured to never see."""
    parts = path.split("/")
    if any(part in VENDOR_DIRS for part in parts):
        return True
    name = parts[-1]
    if name in LOCKFILE_NAMES or name.endswith(NOISE_SUFFIXES):
        return True
    if path.startswith(AKASHIC_DIR + "/"):
        return True
    return bool(excludes) and matches_any(path, excludes)


def tracked_files(root, at_head=False):
    if at_head:
        out = git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout
    else:
        out = git(root, "ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


def tracked_blobs(root):
    """{path: blob sha} for every file at HEAD, from one ls-tree call.

    Content identity that survives losing the anchor commit: blob shas are
    content hashes, so comparing them proves a file is byte-identical without
    needing the old commit to still exist. One subprocess rather than a
    rev-parse per file -- a page can depend on a hundred paths."""
    out = git(root, "ls-tree", "-r", "-z", "HEAD").stdout
    blobs = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob" and path:
            blobs[path] = parts[2]
    return blobs


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
        if is_noise(path, excludes):
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
    """Return (sources_blocks, citations, wiki_links, empty_sources).

    A Sources block is a paragraph: the `Sources:` line plus following lines
    until a blank line or heading (DESIGN.md 3.3 — wrapped citations count).
    Fenced code blocks are ignored entirely: an example citation in a fence is
    neither verified nor a dependency. empty_sources lists the start line of
    any Sources block that yielded zero parseable links.
    """
    citations, wiki_links, empty_sources = [], [], []
    sources_blocks = 0
    in_fence = False
    in_sources = False
    block_start = block_links = 0

    def close_block():
        nonlocal in_sources
        if in_sources and block_links == 0:
            empty_sources.append(block_start)
        in_sources = False

    for lineno, line in enumerate(body_text.split("\n"), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            close_block()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if SOURCES_RE.match(line):
            close_block()
            sources_blocks += 1
            in_sources = True
            block_start, block_links = lineno, 0
        elif in_sources and (not stripped or stripped.startswith("#")):
            close_block()
        for match in LINK_RE.finditer(line):
            target = match.group(2)
            if in_sources:
                citations.append((lineno, target))
                block_links += 1
            elif not SCHEME_RE.match(target) and not target.startswith("#") \
                    and target.split("#")[0].endswith(".md"):
                wiki_links.append((lineno, target))
    close_block()
    return sources_blocks, citations, wiki_links, empty_sources


def resolve_citation(root, page_file, raw_target):
    """Split a citation target into (repo-relative posix path, fragment, error)."""
    # CommonMark lets a link destination be wrapped in <...> to include
    # characters like literal "[" "]" without percent-encoding -- exactly
    # what a citation link TEXT containing a Next.js "[id]/route.ts" path
    # forces the destination to need too. Strip the wrapper before parsing,
    # or "<.." is treated as one literal path segment instead of "..".
    if raw_target.startswith("<") and raw_target.endswith(">") and len(raw_target) >= 2:
        raw_target = raw_target[1:-1]
    target, _, fragment = raw_target.partition("#")
    target = unquote(target)
    if SCHEME_RE.match(raw_target):
        return None, None, "URL schemes are not allowed in Sources (cite repo files)"
    if target.startswith("/"):
        return None, None, "absolute paths are not allowed; use paths relative to the page"
    if any(ord(ch) < 0x20 for ch in target):
        return None, None, "control characters are not allowed in citation paths"
    try:
        resolved = (page_file.parent / target).resolve()
        rel = resolved.relative_to(root)
    except (ValueError, OSError):
        return None, None, f"resolves outside repository or is invalid: {target}"
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
    # +1 tolerance: a file ending in a trailing newline splits into one more
    # line than line_count()/`wc -l` report if you enumerate on "\n" without
    # dropping the final empty segment -- exactly what Claude's Read tool
    # does, so every citation naturally trusts a line count one higher than
    # ours for the (huge majority of) files that end in a newline. The extra
    # line is empty; citing it costs nothing on GitHub (nothing highlights)
    # and rejecting it here just means every generation run needs a fix-up
    # pass for a harmless, 100%-reproducible display artifact. Genuinely
    # wrong ranges (more than one over) are still rejected.
    if end > total + 1:
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
        pid = page["id"]  # slug-validated at load (trust boundary)
        if pid in by_id:
            errors.append(f"catalog: duplicate page id \"{pid}\"")
        by_id[pid] = page
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
    # Citations resolve against git's exact path strings, not the filesystem:
    # untracked/ignored files can never appear in an anchor..HEAD diff (the page
    # would be permanently fresh), and on case-insensitive filesystems is_file()
    # accepts a wrong-case path that the diff intersection would never match.
    tracked = set(tracked_files(root))
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

        sources_blocks, citations, wiki_links, empty_sources = parse_page_links(body)
        if sources_blocks == 0:
            errors.append(f"{pid}: {rel}: no `Sources:` line (cite-or-omit; every page "
                          "cites at least one repo file)")
        for lineno in empty_sources:
            errors.append(f"{pid}: {rel} line {lineno}: Sources block has no "
                          "parseable citation links")

        for lineno, raw in citations:
            path, fragment, err = resolve_citation(root, page_file, raw)
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {err}")
                continue
            cited = root / path
            if path.startswith(AKASHIC_DIR + "/"):
                if not cited.is_file():
                    errors.append(f"{pid}: {rel} line {lineno}: cited file not found: "
                                  f"{path}")
                continue
            if path not in tracked:
                errors.append(f"{pid}: {rel} line {lineno}: cited file is not tracked "
                              f"by git (untracked, ignored, or wrong case): {path}")
                continue
            if not cited.is_file():
                errors.append(f"{pid}: {rel} line {lineno}: cited file missing from "
                              f"working tree: {path}")
                continue
            err = check_fragment(fragment, cited.read_bytes())
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {path}: {err}")
            # "Read these files -- this is the ENTIRE set you may cite" (the
            # rendered prompt) is a promise nothing mechanically enforced
            # until now. A citation outside the page's own scope isn't
            # necessarily wrong -- the file is real and correctly resolved --
            # but it's drift from the catalog's stated intent, and it's the
            # same signal in both directions: scope too wide (duplicated
            # content) or too narrow (the page needed more than it was
            # given). Surface it; don't block anchor over it.
            scope = page.get("scope", [])
            if scope and not matches_any(path, scope):
                warn(f"{pid}: {rel} line {lineno}: cites {path}, which is outside "
                     "this page's catalog scope (not necessarily wrong -- consider "
                     "whether scope should be widened, or the citation trimmed)")

        for lineno, raw in wiki_links:
            path, _, err = resolve_citation(root, page_file, raw)
            if err:
                errors.append(f"{pid}: {rel} line {lineno}: {err}")
                continue
            target = root / path
            if wiki.resolve() not in target.parents:
                continue
            target_id = target.stem
            if target_id == "README":
                continue  # derived at anchor time, not a catalog page
            target_page = by_id.get(target_id)
            if target_page is None:
                errors.append(f"{pid}: {rel} line {lineno}: link to unknown page id "
                              f"\"{target_id}\" (no such page in catalog): {raw}")
            elif target_page.get("status") != "done":
                # A forward reference to a planned sibling is expected during
                # progressive/partial generation (DESIGN.md 5) -- not an error.
                warn(f"{pid}: {rel} line {lineno}: forward link to not-yet-generated "
                     f"page \"{target_id}\" (currently planned)")

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


def fallback_stale(root, catalog, done, excludes):
    """Staleness without a diff, using recorded blob shas as proof of identity.

    A page is provably fresh only when both hold:

      (a) every dependency it recorded still hashes to the same blob at HEAD --
          a deleted or modified file breaks this immediately; and
      (b) nothing new has entered its scope, i.e. the scope expanded against
          HEAD is a subset of the files already recorded.

    (b) is not redundant. Blob comparison can only speak about paths already
    recorded, so a brand-new file matching the page's scope globs is invisible
    to it -- exactly the case the glob half of the reachable path exists to
    catch. Without (b) a page would be declared fresh while a new module sat
    undocumented inside its own scope.

    A page with no recorded blobs (written before this field existed, or never
    anchored) is never provably fresh, so it stays stale. Cheaper to
    regenerate one page than to invent freshness."""
    head_blobs = tracked_blobs(root)
    head_files = [p for p in head_blobs
                  if not p.startswith(AKASHIC_DIR + "/")]
    stale = []
    for page in done:
        recorded_blobs = page.get("blobs") or {}
        recorded_files = set(page.get("files", []))
        unchanged = bool(recorded_blobs) and all(
            head_blobs.get(path) == sha for path, sha in recorded_blobs.items())
        in_scope = set(expand_scope(root, page.get("scope", []), excludes,
                                    paths=head_files))
        if not (unchanged and in_scope <= recorded_files):
            stale.append({"id": page["id"], "changed": []})
    return stale


def compute_stale(root):
    """Read-only staleness report: which pages need what, per DESIGN.md section 5."""
    catalog = load_catalog(root)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    anchor = catalog.get("anchor")
    dirty = bool(git(root, "status", "--porcelain").stdout.strip())
    done = [p for p in catalog["pages"] if p.get("status") == "done"]
    wiki = wiki_dir(root)
    excludes = catalog.get("exclude", [])

    report = {
        "anchor": anchor, "head": head, "anchor_reachable": True,
        "anchor_state": "ok", "dirty": dirty,
        "stale": [], "edited": [], "orphaned": [], "uncovered": [], "missing": [],
        "planned": [],
    }

    # `missing` only ever inspects done pages, so it cannot see a page that was
    # never generated at all -- and `verify`/`anchor` skip non-done pages too.
    # Without this bucket a generate run whose subagents died reports
    # "verify: ok", stamps an anchor and shows a clean `stale`, because a
    # planned page is indistinguishable from one nobody attempted. Reported,
    # not an error: a partly generated wiki is a valid resume state
    # (DESIGN.md section 5), so strictness is opt-in via --check.
    for page in catalog["pages"]:
        if page.get("status") != "done" and not (
                wiki / f"{page['id']}.md").is_file():
            report["planned"].append(page["id"])

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
        # No diff is possible, but the recorded blob shas still prove content
        # identity: they are content hashes, so equality at HEAD means the file
        # is byte-identical whether or not the anchor commit survives. That is
        # proof, not a guess, which is why it may mark a page fresh here.
        #
        # The two causes need different fixes, so name them apart: a first run
        # just needs `anchor`, whereas a vanished commit is usually a shallow
        # clone (git cat-file -e exits 128 at depth 1) or an anchor stamped on
        # a branch commit that a squash-merge discarded -- the shape a runner
        # hits every cycle once its own PR is squash-merged, which without this
        # fallback means a full regeneration of every page, forever.
        report["anchor_reachable"] = False
        report["anchor_state"] = "never_anchored" if not anchor \
            else "anchor_unreachable"
        report["stale"] = fallback_stale(root, catalog, done, excludes)
        # Coverage has no "added since the anchor" to work from here, so it is
        # computed over the whole HEAD tree instead of the diff -- a wider
        # question than usual, answered honestly rather than skipped.
        head_blobs = tracked_blobs(root)
        covered_files, covered_scopes = set(), []
        for page in catalog["pages"]:
            covered_files.update(page.get("files", []))
            covered_scopes.extend(page.get("scope", []))
        report["uncovered"] = sorted(
            p for p in head_blobs
            if not p.startswith(AKASHIC_DIR + "/")
            and p not in covered_files
            and not matches_any(p, covered_scopes)
            and not is_noise(p, excludes)
            and is_binary(root / p) is not True)
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

    # Coverage: `files` entries are exact paths (never patterns — a recorded
    # root Makefile must not shadow a new nested Makefile); `scope` is globs.
    covered_files, covered_scopes = set(), []
    for page in catalog["pages"]:
        covered_files.update(page.get("files", []))
        covered_scopes.extend(page.get("scope", []))
    report["uncovered"] = sorted(
        p for p in added_now
        if p not in covered_files
        and not matches_any(p, covered_scopes)
        and not is_noise(p, excludes)
        and is_binary(root / p) is not True)
    return report


WORK_BUCKETS = ("stale", "edited", "orphaned", "uncovered", "missing", "planned")


def cmd_stale(root, check=False):
    report = compute_stale(root)
    print(json.dumps(report, indent=2))
    if not check:
        return 0
    # The zero-token gate a scheduled runner polls with: exit 1 means an LLM
    # is worth invoking. `edited` counts even though nothing regenerates for
    # it, because a human edit still needs surfacing to a human.
    if any(report[bucket] for bucket in WORK_BUCKETS):
        outstanding = ", ".join(
            f"{bucket}={len(report[bucket])}"
            for bucket in WORK_BUCKETS if report[bucket])
        print(f"stale: work outstanding ({outstanding})", file=sys.stderr)
        if report["anchor_state"] != "ok":
            print(f"stale: {report['anchor_state']}: "
                  + ("no anchor recorded yet; run anchor after generating"
                     if report["anchor_state"] == "never_anchored" else
                     "the recorded anchor commit is not in this clone -- "
                     "usually a shallow clone, or an anchor stamped on a "
                     "commit a squash-merge discarded"), file=sys.stderr)
        return 1
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
    head_blobs = tracked_blobs(root)
    wiki = wiki_dir(root)

    anchored = 0
    for page in catalog["pages"]:
        if page.get("status") != "done":
            continue
        page_file = wiki / f"{page['id']}.md"
        body = page_file.read_text(encoding="utf-8", errors="replace")
        _, citations, _, _ = parse_page_links(body)
        cited = set()
        for _, raw in citations:
            path, _, err = resolve_citation(root, page_file, raw)
            if not err and not path.startswith(AKASHIC_DIR + "/"):
                cited.add(path)
        scope = page.get("scope", [])
        in_scope = {p for p in tracked if scope and matches_any(p, scope)}
        page["files"] = sorted(cited | in_scope)
        # Content identity for each dependency, so freshness survives losing
        # the anchor commit (see fallback_stale). A dependency that is not at
        # HEAD -- staged but uncommitted, say -- is simply absent, and absence
        # means "cannot be proven fresh", which is the safe direction.
        page["blobs"] = {path: head_blobs[path] for path in page["files"]
                         if path in head_blobs}

        # The recorded hash permanently means "what the tool wrote". A null
        # hash is the bless signal (the orchestrator sets it after writing a
        # page). If the current text differs from the recorded hash, a human
        # edited it — re-hashing here would launder the `edited` marker away
        # and let the next update silently overwrite their work.
        current = page_hash(page_file)
        recorded = page.get("hash")
        if recorded and current != recorded:
            warn(f"page \"{page['id']}\" is human-edited; keeping its recorded "
                 "hash so it stays protected (set \"hash\": null after an "
                 "intentional regeneration to bless new content)")
        else:
            page["hash"] = current
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


# ----------------------------------------------------------------------- bless

def bless_pages(root, page_ids, mark_done=False):
    """Set `hash` to null for pages the tool just (re)wrote -- the one metadata
    mutation the LLM side used to perform by hand-editing catalog.json.

    Hand-editing was the documented exception to "the script owns catalog
    metadata", and it was also the exception that could lose work: it is two
    steps (write the page, then edit the JSON) with no atomicity between them,
    so a session dying in the gap leaves the tool's own fresh output sitting
    under the previous hash -- which `stale` then reports as `edited`, i.e. as
    human work to be protected from the very tool that wrote it. One command,
    one atomic save, no exception left to forget."""
    catalog = load_catalog(root)
    by_id = {p["id"]: p for p in catalog["pages"]}
    wiki = wiki_dir(root)

    for page_id in page_ids:
        page = by_id.get(page_id)
        if page is None:
            die(f"no such page \"{page_id}\" in catalog")
        page_file = wiki / f"{page_id}.md"
        if not page_file.is_file():
            die(f"cannot bless \"{page_id}\": "
                f"{page_file.relative_to(root).as_posix()} does not exist "
                "(write the page first)")

    for page_id in page_ids:
        page = by_id[page_id]
        page["hash"] = None
        if mark_done:
            page["status"] = "done"
    save_catalog(root, catalog)
    return len(page_ids)


def cmd_bless(root, page_ids, mark_done=False):
    if not page_ids:
        die("usage: akashic.py bless <page-id> [<page-id> ...]")
    count = bless_pages(root, page_ids, mark_done=mark_done)
    suffix = " and marked done" if mark_done else ""
    print(f"blessed {count} page(s){suffix}: {', '.join(page_ids)}")
    return 0


# ---------------------------------------------------------------------- prompt

# Common local-only agent-guidance filenames -- often excluded from git via
# .git/info/exclude or a personal gitignore so one contributor's AI-alignment
# notes don't leak into the shared repo. Fine to read for planning context;
# never citable, since an untracked file may not exist for anyone else who
# clones the repo. Hand-typing this reminder into every subagent prompt is
# exactly the kind of mechanical step a human orchestrator forgets -- that
# is why this is a script concern, not a prompt-writer's memory.
CONTEXT_DOC_NAMES = [
    "CLAUDE.md", "AGENTS.md", ".cursorrules", ".windsurfrules", "GEMINI.md",
    ".github/copilot-instructions.md",
]


def expand_scope(root, scope, excludes=(), paths=None):
    """Scope-matched tracked files, filtered exactly as `scan` filters the
    planner's view. Sharing one definition of the citable universe is the
    point: otherwise `exclude` globs, lockfiles, minified assets, and
    binaries are dropped from planning yet still land in a page's "read
    these files -- this is also the ENTIRE set you may cite" list, and
    `verify` accepts citations to them. A `scope` glob must not be able to
    re-admit what the catalog excluded."""
    candidates = tracked_files(root) if paths is None else paths
    return sorted(p for p in candidates
                  if scope and matches_any(p, scope)
                  and not is_noise(p, excludes)
                  and is_binary(root / p) is False)


def find_context_docs(root):
    tracked = set(tracked_files(root))
    return [name for name in CONTEXT_DOC_NAMES
            if (root / name).is_file() and name not in tracked]


def render_prompt(root, catalog, page_id):
    """Deterministically render the exact subagent prompt for one catalog
    page (SKILL.md's page contract). No LLM judgment in this function --
    it is plain string templating from catalog.json + the filesystem, so
    every dispatch gets an identical, correct instantiation of the rules
    (citable-file boundary, citation grammar, context-doc handling)."""
    by_id = {p["id"]: p for p in catalog["pages"]}
    page = by_id.get(page_id)
    if page is None:
        die(f"no such page \"{page_id}\" in catalog")
    files = expand_scope(root, page.get("scope", []), catalog.get("exclude", []))
    siblings = sorted((p["id"], p["title"]) for p in catalog["pages"]
                      if p["id"] != page_id)
    context_docs = find_context_docs(root)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"Write the wiki page **{page['title']}** for the repository at {root}.",
        f"Goal: {page['goal']}",
        "Read these files -- this is also the ENTIRE set of files you may cite "
        "in Sources: lines (paths relative to repo root):",
    ]
    lines += [f"  - {f}" for f in files] or [
        "  (none matched -- fix this page's catalog scope before generating)"]
    lines += ["", "Sibling pages for cross-links (id -> title):"]
    lines += [f"  - {sid} -> {stitle}" for sid, stitle in siblings]
    lines.append("")
    if context_docs:
        lines.append(
            "This repo also has local-only agent-guidance docs present on disk "
            "but NOT tracked by git (commonly excluded via .git/info/exclude or "
            f"a personal gitignore): {', '.join(context_docs)}. Read them for "
            "domain and convention context if useful -- but they are NOT in "
            "your citable file list above. Never write a Sources: line pointing "
            "at any of them. If a fact from one of them needs a citation, find "
            "and cite the underlying tracked source code that implements it "
            "instead, state it as general prose without a citation, or omit it.")
        lines.append("")
    lines += [
        f"Write to `{AKASHIC_DIR}/{WIKI_DIRNAME}/{page_id}.md`, exactly this shape:",
        f"- First line: `# {page['title']}`, then a one-paragraph orientation.",
        "- H2 sections. Every H2 section ends with a citation paragraph: "
        "`Sources: [path/to/file.ts:12-40](../../path/to/file.ts#L12-L40)` -- "
        "the literal token `Sources:`, comma-separated markdown links, paths "
        "relative to the page (repo root is `../../`), optional "
        "`#L<start>-L<end>` with line numbers that are exactly right in the "
        "current working tree (double-check every range against the actual "
        "file before writing it). Only cite files from the list above. Never "
        "`file://`, never absolute paths, never URLs on Sources lines.",
        "- Plain Mermaid (no style directives) only where a diagram genuinely "
        "clarifies; put a Sources: line directly under each diagram.",
        "- Cross-reference the most relevant sibling pages as "
        "`[Title](./other-id.md)` in prose.",
        "- Cite-or-omit: prefer \"not documented here\" over invention.",
        f"- Last line: *Generated from commit `{head[:8]}` on {date}.*",
        f"- Prose language: {catalog.get('language', 'en')}. Structural tokens "
        "(`Sources:`, heading syntax) stay as specified regardless of language.",
    ]
    return "\n".join(lines)


def cmd_prompt(root, page_id):
    if not page_id:
        die("usage: akashic.py prompt <page-id>")
    print(render_prompt(root, load_catalog(root), page_id))
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
    stale_parser = sub.add_parser(
        "stale",
        help="JSON report: stale/edited/orphaned/uncovered/missing/planned pages")
    stale_parser.add_argument(
        "--check", action="store_true",
        help="exit 1 when any bucket is non-empty (zero-token gate for a runner)")
    sub.add_parser("verify", help="check pages, citations, and catalog invariants")
    sub.add_parser("anchor", help="record files/hashes, stamp anchor commit, render TOC")
    prompt_parser = sub.add_parser(
        "prompt", help="render the exact subagent prompt for one catalog page id")
    prompt_parser.add_argument("page_id")
    bless_parser = sub.add_parser(
        "bless", help="mark pages as tool-written (hash -> null) after regenerating")
    bless_parser.add_argument("page_ids", nargs="+", metavar="page-id")
    bless_parser.add_argument(
        "--done", action="store_true",
        help="also set status to done (use after generating a planned page)")
    args = parser.parse_args(argv)

    root = repo_root(args.path)
    if args.command == "prompt":
        return cmd_prompt(root, args.page_id)
    if args.command == "bless":
        return cmd_bless(root, args.page_ids, mark_done=args.done)
    if args.command == "stale":
        return cmd_stale(root, check=args.check)
    command = {"scan": cmd_scan,
               "verify": cmd_verify, "anchor": cmd_anchor}[args.command]
    return command(root)


if __name__ == "__main__":
    sys.exit(main())
