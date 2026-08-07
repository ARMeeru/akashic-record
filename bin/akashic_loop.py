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

CLEAN = "clean"
NEEDS_UPDATE = "needs-update"
REVIEW_ONLY = "review-only"


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

    `edited` is deliberately not lumped in with the rest. A human edited that
    page, hard rule 2 says never overwrite it, and regenerating is exactly the
    wrong response -- so a repo whose only finding is `edited` is reported for
    a human to look at and never handed to an LLM. A repo with both gets
    updated for the other buckets; the edited pages are skipped by the update
    flow itself, not by this classifier."""
    work = {bucket: report.get(bucket) or []
            for bucket in ("stale", "orphaned", "uncovered", "missing",
                           "planned")}
    if any(work.values()):
        return NEEDS_UPDATE
    if report.get("edited"):
        return REVIEW_ONLY
    return CLEAN


def summarize(report):
    parts = [f"{bucket}={len(report[bucket])}"
             for bucket in ("stale", "edited", "orphaned", "uncovered",
                            "missing", "planned")
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
    run(["gh", "pr", "create", "--fill"], cwd=repo)


def process(repo, dry_run=False):
    """Returns an exit code contribution: 0 fine, 1 needs a human, 2 failed."""
    print(f"{repo}")
    try:
        report, needs_work = stale_report(repo)
    except RuntimeError as exc:
        notify(f"{repo}: {exc}")
        return 2
    verdict = classify(report)
    print(f"  {summarize(report)} -> {verdict}")

    if verdict == CLEAN:
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
