# akashic-record

A repo wiki generator for Claude Code. It plans a page catalog, generates
repo-committed markdown with line-range citations into source, and incrementally
updates only the pages whose underlying files changed — while never overwriting
human edits.

## Why

Every codebase accumulates knowledge that lives nowhere but in the heads of
whoever wrote it — how the auth layers stack, why a route bypasses the normal
client factory, which admin routes actually check permissions. That knowledge
goes stale the moment someone forgets to update a doc, or never wrote one.
Products like Qoder's Repo Wiki and DeepWiki solve this by generating a wiki
from the code itself, but they're hosted services or IDE features: your code
goes through someone else's pipeline, and the output lives in their format, not
yours. akashic-record does the same job — architecture pages with real source
citations, kept fresh as the code changes — as a Claude Code skill: no service
to trust with your code, no proprietary format, just markdown committed
straight into your repo alongside the code it describes.

- **[DESIGN.md](DESIGN.md)** — the normative design, on-disk format spec, and
  the research this was built against (Qoder Repo Wiki, DeepWiki, and others).
- **[SKILL.md](SKILL.md)** — the Claude Code skill that orchestrates planning
  and generation.
- **[akashic.py](akashic.py)** — the deterministic core (stdlib-only, Python
  3.9+): everything that must never be hallucinated — diffing, citation
  verification, hashing, anchoring.

## How it works

1. **Plan** — Claude reads the repo (README, existing docs, scan output) and
   writes a page catalog: titles, a generation brief per page, and which files
   each page is scoped to.
2. **Generate** — one subagent per page, in parallel, each reading only its
   assigned files and writing a page where every section ends in a citation
   like `Sources: [src/auth/jwt.ts:42-88](../../src/auth/jwt.ts#L42-L88)`.
3. **Verify** — `akashic.py verify` is the one part of this that isn't an LLM:
   it mechanically checks that every citation resolves to a real, git-tracked
   file with a valid line range before anything gets anchored. Nothing reaches
   the wiki that a script hasn't confirmed is real.
4. **Anchor** — records each page's file dependencies and content hash against
   the current commit, so a later `update` knows exactly which pages a given
   code change should regenerate — and never touches a page a human has
   since edited.

A generated page looks like a normal architecture doc: prose, an occasional
Mermaid diagram, and a `Sources:` line under every section pointing at the
exact lines that back the claim above it.

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
python3 akashic.py -C <repo> scan          # filtered file tree (planner input)
python3 akashic.py -C <repo> stale         # JSON: stale/edited/orphaned/uncovered pages
python3 akashic.py -C <repo> verify        # citation + catalog checks (exit 1 on failure)
python3 akashic.py -C <repo> anchor        # record deps/hashes, stamp anchor, render TOC
python3 akashic.py -C <repo> prompt <id>   # render one page's exact subagent prompt
```

## Tests

```sh
python3 test_akashic.py
```
