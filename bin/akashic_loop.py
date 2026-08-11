#!/usr/bin/env python3
"""The maintenance loop: keep a fleet of repo wikis fresh without being watched.

Every cycle is gated by `akashic.py stale --check`, which costs zero tokens.
Only a repo that reports outstanding work gets an LLM run, so a quiet fleet is
free to poll and a busy one pays only for what changed.

This is not a watcher or a daemon (DESIGN.md section 8 rejects those). It has no
long-lived process, subscribes to nothing, and holds no state of its own: it is
a periodic poll of a deterministic check, which is the trigger model every
surveyed system converged on.

Repo list: one path per line, `#` comments and blanks ignored, `~` expanded.
Read from $AKASHIC_REPOS if set, else ~/.config/akashic-record/repos.

Exit codes: 0 nothing to do or everything handled, 1 something needs a human,
2 a step failed. Never exits 0 on a failure -- a loop that fails silently
fossilizes the wiki, which is worse than having no loop.

Stdlib only, Python 3.9+, same as akashic.py.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AKASHIC = HERE.parent / "akashic.py"
DEFAULT_REPO_LIST = Path.home() / ".config" / "akashic-record" / "repos"

# The work model is imported, never restated. This file used to keep its own
# list of which buckets mean work, and that copy went stale the same day two
# buckets were added to the core -- the failure this import exists to prevent.
sys.path.insert(0, str(HERE.parent))
import akashic  # noqa: E402

CLEAN = "clean"
NEEDS_UPDATE = "needs-update"
REVIEW_ONLY = "review-only"
REMAP_ONLY = "remap-only"

# Action names in akashic.BUCKET_ACTIONS -> this loop's verdicts.
ACTION_VERDICTS = {
    "update": NEEDS_UPDATE,
    "remap": REMAP_ONLY,
    "review": REVIEW_ONLY,
}
# Most-to-least urgent. A repo with both drifted and stale pages is updated,
# because regeneration rewrites the citations remap would have shifted; a repo
# whose only finding is `edited` is shown to a human and never touched.
VERDICT_PRECEDENCE = (NEEDS_UPDATE, REMAP_ONLY, REVIEW_ONLY)

# Only a remap PR may merge itself. Its diff is line numbers computed by
# integer arithmetic from a git diff -- no model produced any of it, and a
# reviewer can check it against the code in seconds. A PR containing generated
# prose is a different object: verify proves its citations resolve, not that
# its sentences are true, and that difference is exactly what a human is for.
AUTO_MERGEABLE = frozenset({REMAP_ONLY})


def may_auto_merge(verdict):
    return verdict in AUTO_MERGEABLE


def read_repo_list(text):
    """One repo path per line; `#` comments and blank lines ignored."""
    repos = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            repos.append(str(Path(line).expanduser()))
    return repos


def classify(report):
    """What this repo needs, from a `stale` report.

    Routing comes from `akashic.BUCKET_ACTIONS` rather than a list kept here.
    The list kept here is what broke: it covered every bucket that existed when
    it was written, two more were added to the core hours later, and a
    restated-only or unblessed-only repo classified as clean from then on.

    `edited` gets its own action and never folds into update. A human edited
    that page, hard rule 2 says never overwrite it, and regenerating is exactly
    the wrong response -- so a repo whose only finding is `edited` is reported
    for a human and never handed to an LLM. A repo with both gets updated for
    the other buckets; the edited pages are skipped by the update flow itself,
    not by this classifier.

    An action this loop does not recognize raises. Defaulting to update would
    hand a future edited-like bucket to a model and destroy prose, so the one
    safe behaviour for an unknown action is to stop.

    Drifted stays its own verdict: cited lines moved without their content
    changing, which is arithmetic, so `remap` fixes it and no model runs -- the
    cheapest cycle, and the only kind whose PR contains no generated prose."""
    verdicts = set()
    # Iterate the routing table, not the derived tuple: the table is the
    # authority on what a bucket means, and reading the copy would reintroduce
    # a one-step version of the indirection that drifted.
    for bucket, action in akashic.BUCKET_ACTIONS.items():
        if not report.get(bucket):
            continue
        if action not in ACTION_VERDICTS:
            raise RuntimeError(
                f"bucket \"{bucket}\" has action \"{action}\", which this loop "
                "does not know how to run; refusing to guess")
        verdicts.add(ACTION_VERDICTS[action])
    for verdict in VERDICT_PRECEDENCE:
        if verdict in verdicts:
            return verdict
    return CLEAN


def summarize(report):
    parts = [f"{bucket}={len(report[bucket])}"
             for bucket in akashic.WORK_BUCKETS
             if report.get(bucket)]
    if report.get("anchor_state", "ok") != "ok":
        parts.append(report["anchor_state"])
    return ", ".join(parts) or "clean"


def notify(message):
    """Surface something to the owner. $AKASHIC_NOTIFY receives it on stdin
    (`terminal-notifier`, a mail command, whatever this machine has); with
    nothing configured it still reaches stderr, so a cron mail catches it."""
    print(f"akashic-loop: {message}", file=sys.stderr)
    hook = os.environ.get("AKASHIC_NOTIFY")
    if not hook:
        return
    try:
        subprocess.run(hook, shell=True, input=message, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"akashic-loop: notify hook failed: {exc}", file=sys.stderr)


def stale_report(repo):
    """Run the zero-token gate. Returns (report, needs_work) or dies."""
    result = subprocess.run(
        [sys.executable, str(AKASHIC), "-C", repo, "stale", "--check"],
        capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"stale --check failed ({result.returncode}): "
            f"{result.stderr.strip() or 'no output'}")
    try:
        return json.loads(result.stdout), result.returncode == 1
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"stale produced unparseable JSON: {exc}") from exc


def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:3])} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    return result


def remap_repo(repo, branch, dry_run=False):
    """The zero-LLM path: shift drifted citations, verify, anchor, open a PR.

    Nothing generated, nothing stochastic -- the diff is a handful of line
    numbers a reviewer can check against the code in seconds."""
    if dry_run:
        print(f"  would: branch {branch}, remap citations, open a PR (no LLM)")
        return
    run(["git", "switch", "-c", branch], cwd=repo)
    run([sys.executable, str(AKASHIC), "-C", repo, "remap"])
    run([sys.executable, str(AKASHIC), "-C", repo, "verify"])
    if not run(["git", "status", "--porcelain"], cwd=repo).stdout.strip():
        raise RuntimeError("remap reported drift but rewrote nothing")
    run(["git", "add", ".akashic"], cwd=repo)
    run(["git", "commit", "-m", "chore: remap drifted citations"], cwd=repo)
    run([sys.executable, str(AKASHIC), "-C", repo, "anchor"])
    run(["git", "add", ".akashic"], cwd=repo)
    run(["git", "commit", "-m", "chore: anchor remapped wiki"], cwd=repo)
    run(["git", "push", "-u", "origin", branch], cwd=repo)
    open_pr(repo, "chore(wiki): remap drifted citations",
            "Cited lines moved without their content changing, so every line "
            "number here was shifted by integer arithmetic from the diff. No "
            "model ran. `verify` passed before this PR was opened.",
            auto_merge=may_auto_merge(REMAP_ONLY))


def open_pr(repo, title, body, auto_merge=False):
    """Open the PR, and enable auto-merge only where the policy allows it.

    Auto-merge is requested rather than performed: the required status checks
    on the branch are what actually gate it, so a red run holds the PR open
    instead of landing it. If the repo has auto-merge disabled the request
    fails harmlessly and the PR simply waits for a human, which is why this
    warns rather than raising."""
    run(["gh", "pr", "create", "--title", title, "--body", body], cwd=repo)
    if not auto_merge:
        return
    result = run(["gh", "pr", "merge", "--auto", "--merge"], cwd=repo,
                 check=False)
    if result.returncode != 0:
        notify(f"{repo}: PR opened but auto-merge could not be enabled "
               f"({result.stderr.strip()[:200]}); it needs merging by hand")


def update_repo(repo, branch, dry_run=False):
    """Branch, run the skill's update flow headlessly, open a PR.

    Never commits to the checked-out branch: the whole point of a PR is that
    an unattended run's output gets read before it lands."""
    if dry_run:
        print(f"  would: branch {branch}, run the update flow, open a PR")
        return
    run(["git", "switch", "-c", branch], cwd=repo)
    claude = run(["claude", "-p", "/akashic-record update"], cwd=repo,
                 check=False)
    if claude.returncode != 0:
        raise RuntimeError(
            f"update flow failed ({claude.returncode}): "
            f"{claude.stderr.strip()[:400] or 'no output'}")
    verify = run([sys.executable, str(AKASHIC), "-C", repo, "verify"],
                 check=False)
    if verify.returncode != 0:
        raise RuntimeError(
            "the update flow left the wiki unverified; refusing to open a PR: "
            f"{verify.stdout.strip()[:400]}")
    if not run(["git", "status", "--porcelain"], cwd=repo).stdout.strip():
        raise RuntimeError(
            "the update flow reported work but changed nothing")
    run(["git", "add", ".akashic"], cwd=repo)
    run(["git", "commit", "-m", "chore: refresh wiki"], cwd=repo)
    run(["git", "push", "-u", "origin", branch], cwd=repo)
    open_pr(repo, "chore(wiki): refresh stale pages",
            "Pages regenerated because their sources changed. `verify` passed, "
            "but these contain generated prose: verification proves the "
            "citations resolve, not that the sentences above them are true. "
            "Read the diff.",
            auto_merge=may_auto_merge(NEEDS_UPDATE))


def process(repo, dry_run=False):
    """Returns an exit code contribution: 0 fine, 1 needs a human, 2 failed."""
    print(f"{repo}")
    try:
        report, needs_work = stale_report(repo)
    except RuntimeError as exc:
        notify(f"{repo}: {exc}")
        return 2
    try:
        verdict = classify(report)
    except RuntimeError as exc:
        notify(f"{repo}: {exc}")
        return 2
    print(f"  {summarize(report)} -> {verdict}")

    # The parity tripwire. `--check` exits 1 when any work bucket is non-empty,
    # so the gate and the classifier are two answers to one question and they
    # must agree. Disagreement is a defect in this file, not reviewable content
    # in the repo, and it is the only guard that catches a divergence no table
    # knows about -- including a bucket added to a newer core than the routing
    # here was written against. Exit 2, because a loop quietly reporting clean
    # while the gate says otherwise is precisely the failure the zero-token
    # story depends on not happening.
    if needs_work and verdict == CLEAN:
        notify(f"{repo}: stale --check reports outstanding work but the loop "
               f"classified it {CLEAN} ({summarize(report)}). The loop's "
               "routing is out of step with akashic.py's work buckets; this is "
               "a bug in akashic_loop.py, not something to fix in the repo.")
        return 2

    if verdict == CLEAN:
        return 0
    # A dry run's whole product is the report, and under a scheduler stdout is
    # a file nobody opens. Without this, the report-only mode -- the mode you
    # are meant to start in -- says nothing to anyone, which looks exactly
    # like a clean fleet. The live modes stay quiet here on purpose: they
    # speak by opening a PR, and notify() is reserved for what needs a human.
    if dry_run:
        notify(f"{repo}: {summarize(report)} -> {verdict} "
               "(dry run; nothing changed)")
    if verdict == REMAP_ONLY:
        try:
            remap_repo(repo, "chore/wiki-remap", dry_run=dry_run)
        except RuntimeError as exc:
            notify(f"{repo}: {exc}")
            return 2
        return 0
    if verdict == REVIEW_ONLY:
        notify(f"{repo}: {len(report['edited'])} page(s) edited by hand; "
               "not regenerating (hard rule 2). Review them yourself.")
        return 1
    try:
        update_repo(repo, "chore/wiki-refresh", dry_run=dry_run)
    except RuntimeError as exc:
        notify(f"{repo}: {exc}")
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="akashic-loop",
        description="Poll a fleet of repos and refresh stale wikis by PR.")
    parser.add_argument("--repos", help="repo-list file (default: "
                                        "$AKASHIC_REPOS or "
                                        "~/.config/akashic-record/repos)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what each repo needs, spend nothing")
    args = parser.parse_args(argv)

    listing = args.repos or os.environ.get("AKASHIC_REPOS") \
        or str(DEFAULT_REPO_LIST)
    path = Path(listing).expanduser()
    if not path.is_file():
        notify(f"no repo list at {path}; nothing to do")
        return 2
    repos = read_repo_list(path.read_text(encoding="utf-8"))
    if not repos:
        notify(f"repo list {path} is empty")
        return 2

    worst = 0
    for repo in repos:
        worst = max(worst, process(repo, dry_run=args.dry_run))
    return worst


if __name__ == "__main__":
    sys.exit(main())
