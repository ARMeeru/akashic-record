# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Repo wiki: .akashic/wiki/README.md (architecture + module docs with source citations)
Roadmap: GitHub issues #1–#12 (milestones M1–M3 + Backlog) — issues carry sequencing constraints and a per-task definition of done; run `gh issue view <n>` before starting feature work.

## What this repo is

akashic-record is a Claude Code **skill** — not a CLI, service, or MCP server — that generates and incrementally maintains a repo-committed wiki (`.akashic/wiki/`) with line-range citations into source. Three artifacts:

- **SKILL.md** — the LLM side: plan/generate/update/status orchestration and the hard rules (script authority, edit protection, cite-or-omit).
- **akashic.py** — the deterministic core: one file, stdlib only, Python 3.9+. Subcommands `scan`, `stale`, `verify`, `anchor`, `prompt`.
- **DESIGN.md** — the **normative** design and on-disk format spec. Any behavior or format change must keep DESIGN.md in sync, and §8 lists things deliberately not built (embeddings/RAG, DB storage, MCP server, watchers, Mermaid validation…) — don't reintroduce them without updating the design first.

Install = symlink this repo to `~/.claude/skills/akashic-record` (the repo *is* the skill); don't assume the symlink exists on the current machine.

## Commands

No build step, no dependencies, no lint config. Stdlib only — do not add third-party imports.

```sh
python3 test_akashic.py                                  # full suite
python3 test_akashic.py TestStale                        # one class
python3 test_akashic.py TestStale.test_rename_marks_stale_not_orphaned   # one test
```

The helper runs against any target git repo (hard precondition: ≥1 commit):

```sh
python3 akashic.py -C <repo> scan          # filtered file list with line counts (planner input)
python3 akashic.py -C <repo> stale         # JSON: stale/edited/orphaned/uncovered/missing/planned
python3 akashic.py -C <repo> stale --check # same, exit 1 if any bucket is non-empty (runner gate)
python3 akashic.py -C <repo> verify        # citation + catalog gate; exit 1 on any error
python3 akashic.py -C <repo> anchor        # verify, then record deps/hashes, stamp anchor, render TOC
python3 akashic.py -C <repo> prompt <id>   # render one page's exact subagent prompt
python3 akashic.py -C <repo> remap         # shift drifted citations to new line numbers (no LLM)
python3 akashic.py -C <repo> bless <id>    # hash -> null after regenerating; --done also flips status
```

Exit codes: 0 ok, 1 verification failure, 2 usage/precondition error.

## Architecture

The one non-negotiable division of labor (DESIGN.md §2): **the LLM never computes a hash, diff, or line range; akashic.py never calls an LLM.** Anything that could lose human work or mark stale content as fresh is deterministic code; everything stochastic passes through `verify` before it can be anchored.

On-disk format the tool writes into *target* repos: `.akashic/catalog.json` is the single authoritative metadata file (page ids/titles/goals/scopes/files/status/hash, anchor commit, settings). Pages live flat as `.akashic/wiki/<id>.md` — hierarchy exists only in catalog `parent` fields — and `wiki/README.md` is a TOC re-derived on every `anchor`, never hand-edited.

Invariants that shape most of the code:

- **Page ids are frozen forever** and slug-validated at catalog load in every command — an id is a path component, so a non-slug id is a traversal vector.
- **`hash` permanently means "what the tool last wrote."** `null` is the bless signal, set immediately after (re)generating a page; `anchor` records a new hash only for null/unchanged pages. A changed body under a non-null hash is a human edit — never re-hash it (that would launder the `edited` marker) and never overwrite the page.
- **Staleness is range-level where a page cites line numbers.** `anchor` records `ranges` (path -> cited spans, clamped to real file length); `compute_stale` intersects `git diff -U0` hunks against them, so a change outside the cited lines is not stale. Everything else stays file-level: uncited scope files, fragment-less citations, renames (no hunks under `-M`), deletions, and blob-fallback mode.
- **An unreachable anchor is recoverable, not fatal.** `anchor` records a `blobs` map (path -> blob sha) per page; when the anchor commit is gone, a page is provably fresh iff every recorded blob is unchanged at HEAD *and* its scope adds nothing new. Byte-equality is proof, so this never marks stale content fresh.
- **`verify` also warns on invented identifiers.** A backticked identifier-shaped token in an H2 section that appears in none of that section's cited files is surfaced as a warning, never an error. Filenames and fenced examples are excluded, and the search covers the whole cited file rather than the cited span.
- **`verify` also warns when anchored content is no longer cited.** `ranges` are in anchor coordinates and the anchor commit is recorded, so `verify` reads what a span held at anchor, relocates it by its first/last non-blank line pair, and warns if no citation covers where it landed. Ambiguous relocation yields no answer rather than a guess; silent when the anchor is unreachable.
- **Citations resolve against git's tracked-path set, not the filesystem.** An untracked or wrong-case path can never appear in an anchor→HEAD diff, so it would make the page permanently fresh. Resolved paths must also realpath inside the repo root (traversal guard).
- **A citation's end line may exceed the real line count by exactly one** — Claude's Read tool shows one extra empty line for files ending in a trailing newline; `check_fragment` tolerates that specific +1 and rejects anything further.
- **`.akashic/` paths are never staleness inputs or recorded dependencies** — the wiki must not depend on itself.
- **Immediately after `anchor`, `stale` is empty** (tested). An unreachable anchor reports *all* pages stale rather than guessing — wasting a regen is acceptable; marking stale content fresh is not.
- **Subagent prompts are rendered by `prompt <id>`, never hand-written** — mechanical templating from catalog + filesystem is what prevents the untracked-citation class of bug.

## Tests

`test_akashic.py` is stdlib `unittest`; fixtures are throwaway git repos built in `tempfile` (see `RepoCase`). One smallest-possible check per deterministic component, per DESIGN.md §9. The LLM phases have no unit tests — the runtime `verify` gate is their coverage.

## Dogfooding

This repo carries its own generated wiki in `.akashic/`. Follow the skill's own rules here: never hand-edit `catalog.json`'s `anchor`/`hash`/`files`/`generated` fields — run `bless <id>` after regenerating a page instead — never edit the derived `wiki/README.md`, and after source changes refresh via the update flow (`stale` → regenerate → `bless` → `verify` → `anchor`), committing as `chore: refresh self-dogfooded wiki` per the existing history.
