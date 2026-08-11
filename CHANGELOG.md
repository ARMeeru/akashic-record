# Changelog

What changed in behavior or in the on-disk format. Internal refactors, test additions
and prose edits are not recorded here; `git log` has those, and a changelog that lists
everything is one nobody reads before upgrading.

**The tag and the catalog `version` are independent on purpose.** This file numbers the
tool. `catalog.json`'s `version` numbers the format the tool reads and writes, it is
currently `1`, and additive optional fields never bump it (see DESIGN.md §3.2). A tool
release can change behavior without touching the format, which is the common case. Do
not read `v0.1.0` and `"version": 1` as tracking each other.

## v0.1.0

First tagged release. Everything below already shipped on `develop`; the tag exists so a
team can pin one, because a symlink cannot pin anything by itself.

**Format.** `catalog.json` is at `version` 1. `goal_hash`, `blobs` and `ranges` are
optional and absent-means-conservative: a catalog written before any of them keeps
working, and a reader that has them stays quiet rather than guessing on a catalog that
does not.

**Nine `stale` buckets**: `stale`, `edited`, `orphaned`, `uncovered`, `missing`,
`planned`, `drifted`, `restated`, `unblessed`. Each maps to one of three actions in
`BUCKET_ACTIONS`, and `WORK_BUCKETS` is derived from that table, so a bucket no action
covers cannot exist.

**Ten subcommands**: `scan`, `stale`, `verify`, `anchor`, `prompt`, `remap`,
`plan-check`, `plan-critic`, `audit`, `bless`. Exit codes are 0 ok, 1 verification
failure, 2 usage or precondition error.

**The maintenance loop** polls a fleet through `stale --check`, opens pull requests
rather than committing, and refuses to run on a checkout that is dirty or sitting on a
branch a previous cycle created.

**Known limit, stated because pinning it should be deliberate.** `verify` proves every
citation resolves to a real tracked file at a real line range. It does not prove the
prose above the citation is true, and an adversarial audit of seven pages on a
production repo found all seven materially wrong. See "What it does not do" in the
README before deciding what to trust.
