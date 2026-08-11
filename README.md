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
- **[bin/akashic_loop.py](bin/akashic_loop.py)** — the maintenance loop: poll a
  fleet, refresh only what changed, open a PR.

## How it works

1. **Plan** — Claude reads the repo (README, existing docs, scan output) and
   writes a page catalog: titles, a generation brief per page, and which files
   each page is scoped to.
2. **Generate** — one subagent per page, in parallel, each reading only its
   assigned files and writing a page where every section ends in a citation
   like `Sources: [src/auth/jwt.ts:42-88](../../src/auth/jwt.ts#L42-L88)`.
3. **Verify** — `akashic.py verify` is the one part of this that isn't an LLM:
   it mechanically checks that every citation resolves to a real, git-tracked
   file with a valid line range before anything gets anchored. It proves the
   citation is real. It cannot prove the sentence above it is true — see
   [what it does not do](#what-it-does-not-do).
4. **Anchor** — records each page's file dependencies and content hash against
   the current commit, so a later `update` knows exactly which pages a given
   code change should regenerate — and never touches a page a human has
   since edited.

A generated page looks like a normal architecture doc: prose, an occasional
Mermaid diagram, and a `Sources:` line under every section pointing at the
exact lines that back the claim above it.

## What it does not do

`verify` proves every citation resolves to a real, tracked file at a real line
range. It does not prove the prose above the citation is true, and that gap is
wider than it sounds. Across four update runs against a 28-page production repo,
an adversarial claim audit was pointed at seven pages and found **all seven
materially wrong** — a section describing a mechanism that existed in one
migration and nowhere else; "nothing here is hard-deleted" above a literal
`DELETE`; "nine components" above a table listing ten.

So read a generated page as a map with footnotes rather than as territory. The
citations tell you exactly where to check, which is the point of having them.

Three things exist because of that gap, and none of them gates `anchor` — a
heuristic over prose that blocked publishing would eventually block a correct
page:

- **`plan-check`** and **`plan-critic`** run *before* generation. The first is
  free and deterministic: unmatched scopes, a scope inside a sibling's, every
  overlapping pair ranked by duplicated lines. The second renders one adversarial
  review prompt for the whole catalog and an LLM judges whether each page's brief
  is truthful and reachable from its own files. A pass is one sample, not a
  measurement; re-running on an unchanged catalog keeps finding things.
- **`audit prompt <id>`** is an on-demand blind refuter. It hands a judge each
  claim beside the exact bytes it cites, with the evidence labelled `[E1]`,
  `[E2]` rather than by filename — because a judge shown a path fills gaps with
  what a file of that name usually contains.
- **Standing generation rules** in every page prompt: scope generalizations to
  what was actually read, state the method behind any absence claim, and write
  only what the goal asks.

The one intervention that reliably fixes a wrong page is correcting its `goal`
and regenerating.

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
python3 akashic.py -C <repo> stale         # JSON: stale/edited/orphaned/uncovered/missing/planned/drifted/restated/unblessed
python3 akashic.py -C <repo> stale --check # same, exit 1 if any bucket is non-empty
python3 akashic.py -C <repo> stale --ids <bucket>  # one page id per line, for shell loops
python3 akashic.py -C <repo> verify        # citation + catalog checks (exit 1 on failure)
python3 akashic.py -C <repo> anchor        # record deps/hashes/blobs, stamp anchor, render TOC
python3 akashic.py -C <repo> prompt <id>   # render one page's prompt (--update adds what changed, --out to a file)
python3 akashic.py -C <repo> remap         # shift drifted citations to new line numbers (no LLM)
python3 akashic.py -C <repo> plan-check    # catalog shape checks before a fan-out
python3 akashic.py -C <repo> plan-critic   # render the adversarial plan-review prompt (--out to a file)
python3 akashic.py -C <repo> audit prompt <id>  # blind claim refuter (on demand; --out to write it to a file)
python3 akashic.py -C <repo> bless <id>    # hash -> null after regenerating (--done also flips status)
```

## Tests

```sh
python3 test_akashic.py
```

## Keeping it fresh

`bin/akashic_loop.py` walks a list of repos, runs `stale --check` on each (no tokens),
and only invokes Claude where something actually changed. It opens a PR rather than
committing, leaves human-edited pages alone, and exits non-zero on any failure.

```sh
mkdir -p ~/.config/akashic-record && echo "$HOME/code/my-repo" >> ~/.config/akashic-record/repos
python3 bin/akashic_loop.py --dry-run     # what each repo needs, spending nothing
python3 bin/akashic_loop.py               # refresh what needs it, by PR
```

Only one kind of PR merges itself. A remap PR contains nothing but line numbers
shifted by integer arithmetic from the diff, so it auto-merges once the branch's
required checks pass. A PR containing regenerated pages always waits for a human:
`verify` proves the citations resolve, not that the prose above them is true.

Schedule it however this machine prefers. Set `$AKASHIC_NOTIFY` to a command that should
receive failures on stdin; without it they still reach stderr.

## Roadmap

All four milestones have shipped, and most of what came after them came out of
running the tool against a real 28-page repo and fixing what broke:

- **M0 — Hygiene and safety**: CI on the supported interpreters, documented glob semantics, a read-only mandate rendered into every subagent prompt.
- **M1 — The loop exists**: the wiki updates itself via PR; no-op cycles cost zero tokens.
- **M2 — Cheap, reviewable cycles**: most update PRs are tiny; pure line drift costs zero tokens and can auto-merge.
- **M3 — Trust, right-sized**: `verify` warns on invented identifiers and on anchored content no citation covers; `plan-check` and `plan-critic` review the plan before a fan-out instead of only its output.
- **Field-driven since**: an on-demand blind claim audit; a `restated` bucket for a page whose *brief* changed rather than its sources; `--out` on every rendered prompt; and the generation rules above.

What is left is the Backlog milestone — deliberately deferred, each item carrying the condition that would bring it back.

Live status: [milestones](https://github.com/ARMeeru/akashic-record/milestones).
