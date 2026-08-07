# Maintenance Loop

`bin/akashic_loop.py` is what turns akashic-record from a generator you remember to run into a wiki that keeps itself current. It walks a list of repositories, asks each one a question that costs nothing, and spends an LLM only where the answer says something actually changed. Everything it decides is downstream of the deterministic report described in [Deterministic Core](./deterministic-core.md), and the update it triggers is the flow documented in [Skill Orchestration](./skill-orchestration.md).

## The zero-token gate

The loop's economics rest on one property: asking "does this repo need work?" is free. Each repo is put through `akashic.py stale --check` as a subprocess, and only exit codes 0 and 1 are treated as answers — 0 meaning nothing outstanding, 1 meaning something is. Any other exit code, or output that will not parse as JSON, is raised rather than absorbed, so a repo the gate cannot read never resembles a repo with nothing to do.

That distinction is the whole reason a schedule is reasonable rather than extravagant. A quiet fleet can be polled as often as you like, because the polling never reaches a model at all.

Sources: [bin/akashic_loop.py:1-27](../../bin/akashic_loop.py#L1-L27), [bin/akashic_loop.py:98-110](../../bin/akashic_loop.py#L98-L110)

## Four verdicts, and the one that never reaches an LLM

`classify` reduces a `stale` report to one of four constants, which `process` then acts on:

- **clean** — nothing in any bucket. The repo is skipped and costs nothing.
- **needs-update** — anything in `stale`, `orphaned`, `uncovered`, `missing` or `planned`. The repo goes through the update flow.
- **remap-only** — the sole finding is `drifted`: cited lines moved without changing. That is arithmetic, so `remap` fixes it and no model is ever invoked. The resulting PR contains no generated prose at all, which is what makes it the one safe candidate for automatic merging.
- **review-only** — `edited` is the *sole* finding. This is the interesting case: a human wrote that page, and hard rule 2 says never overwrite detected human work, so regenerating would be exactly the wrong response. The loop notifies the owner and returns 1 rather than handing the repo to a model.

A repo with `edited` *alongside* real work is still classified `needs-update`, because the other buckets genuinely need doing. Skipping the edited pages themselves is the update flow's job, not the classifier's — the loop does not try to re-implement a rule the skill already enforces.

Sources: [bin/akashic_loop.py:34-37](../../bin/akashic_loop.py#L34-L37), [bin/akashic_loop.py:50-71](../../bin/akashic_loop.py#L50-L71), [bin/akashic_loop.py:174-204](../../bin/akashic_loop.py#L174-L204)

## Pull request, not commit

When a repo needs updating, the loop creates a branch, runs the skill's update flow headlessly via `claude -p`, and opens a pull request. It never commits to whatever branch was checked out.

Before it will open that PR, three things must hold: the update flow itself must exit 0, `verify` must then exit 0, and the working tree must actually contain changes. A flow that reports work but changes nothing, or that leaves the wiki unverified, raises instead of producing a PR — the loop refuses to present unverified output as a finished result.

The reasoning behind the PR is that this output is produced while nobody is watching. A pull request is the cheapest possible place to put a human back in the path without making the loop wait for one.

Sources: [bin/akashic_loop.py:113-120](../../bin/akashic_loop.py#L113-L120), [bin/akashic_loop.py:144-171](../../bin/akashic_loop.py#L144-L171)

## Never exit 0 on failure

The exit-code contract is the loop's most load-bearing property, and it inverts the usual instinct to keep a scheduled job quiet:

```mermaid
flowchart TD
    R[repo] --> G[stale --check]
    G -- unreadable --> E2[exit 2, notify]
    G -- clean --> Z[exit 0]
    G -- edited only --> H[exit 1, notify: needs a human]
    G -- real work --> U[branch, update, verify]
    U -- verify fails or no changes --> E2
    U -- ok --> PR[open PR, exit 0]
```

Sources: [bin/akashic_loop.py:174-204](../../bin/akashic_loop.py#L174-L204)

Across a fleet the worst outcome wins, so one broken repo cannot be hidden by nine healthy ones. A missing or empty repo list is itself a failure rather than a quiet no-op, because a loop configured into silence looks identical to a loop with nothing to do. The stated reason is that a set-and-forget loop failing silently fossilizes the wiki, which is worse than having no loop at all: you would go on believing the docs were current.

Failures reach the owner through `notify`, which always writes to stderr — enough for a scheduler that mails output — and additionally pipes the message to whatever command `$AKASHIC_NOTIFY` names. A notification hook that itself fails is caught and reported rather than being allowed to take down the run.

Sources: [bin/akashic_loop.py:84-95](../../bin/akashic_loop.py#L84-L95), [bin/akashic_loop.py:206-231](../../bin/akashic_loop.py#L206-L231)

## Configuration

The fleet is a plain text file: one repository path per line, `#` comments and blank lines ignored, `~` expanded so paths are never handed to git with a tilde in them. It is read from `--repos`, else `$AKASHIC_REPOS`, else `~/.config/akashic-record/repos`. The list lives outside the repository on purpose — which repos a person maintains is machine-local, not something to commit to a shared project.

`--dry-run` reports what each repo would need and spends nothing, which is also the safe way to confirm a schedule is pointed at the right paths.

Sources: [bin/akashic_loop.py:29-32](../../bin/akashic_loop.py#L29-L32), [bin/akashic_loop.py:40-47](../../bin/akashic_loop.py#L40-L47), [bin/akashic_loop.py:74-81](../../bin/akashic_loop.py#L74-L81)

*Generated from commit `bf1c15e` on 2026-08-07.*
