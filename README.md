# akashic-record

A repo wiki generator for Claude Code. It plans a page catalog, generates
repo-committed markdown with line-range citations into source, and incrementally
updates only the pages whose underlying files changed — while never overwriting
human edits.

- **[DESIGN.md](DESIGN.md)** — the normative design and on-disk format spec.
- **[SKILL.md](SKILL.md)** — the Claude Code skill that orchestrates planning and
  generation.
- **[akashic.py](akashic.py)** — the deterministic core (stdlib-only, Python 3.9+):
  everything that must never be hallucinated.

## Install

```sh
ln -s "$(pwd)" ~/.claude/skills/akashic-record
```

## Use

In any git repo (≥1 commit), inside Claude Code:

```
/akashic-record            # first run: plan + generate the wiki
/akashic-record update     # regenerate only stale pages
/akashic-record status     # what would update, without changing anything
```

The wiki lands in `.akashic/wiki/` as plain markdown — commit it; teammates get it
via `git pull`, GitHub renders it (Mermaid included).

The helper also works standalone:

```sh
python3 akashic.py -C <repo> scan     # filtered file tree (planner input)
python3 akashic.py -C <repo> stale    # JSON: stale/edited/orphaned/uncovered pages
python3 akashic.py -C <repo> verify   # citation + catalog checks (exit 1 on failure)
python3 akashic.py -C <repo> anchor   # record deps/hashes, stamp anchor, render TOC
```

## Tests

```sh
python3 test_akashic.py
```
