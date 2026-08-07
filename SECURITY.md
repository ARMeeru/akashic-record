# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/ARMeeru/akashic-record/security/advisories/new). Do not open a public issue for security reports.

You should receive an acknowledgment within 7 days. This is a solo-maintained project; response and fix timelines are best-effort, and coordinated disclosure after a fix lands is appreciated.

## Scope

akashic-record is a Claude Code skill plus two stdlib-only Python scripts. It reads a target git repository and writes generated wiki files into that repository's `.akashic/` directory.

`akashic.py` has no dependencies and makes no network calls — it shells out only to local git operations (`rev-parse`, `diff`, `ls-files`, `ls-tree`, `cat-file`, `show`, `status`).

`bin/akashic_loop.py` does reach the network, by design: it runs `claude -p`, `git push`, and `gh pr create`. Two behaviours there are intentional rather than defects, and are named here so they are not reported as vulnerabilities: it executes the command named by `$AKASHIC_NOTIFY` through a shell to deliver failure notifications, and it opens pull requests on repositories listed in your own config file.

In scope:

- Path traversal out of the target repository root (citation resolution, page-id handling)
- Writes escaping `.akashic/` in the target repository
- Anything that causes stale or human-edited wiki content to be reported as fresh (edit-protection or hash-semantics bypass)
- Bypasses of the `verify` gate (citations accepted that should be rejected)

Worth stating plainly, because it reads like a control and is not one: the
read-only mandate rendered into every subagent prompt, and the blind labelling
in the claim audit, are **instructions to a model, not a sandbox**. A subagent
has whatever tools its harness grants it. Treat them as conventions the tool
makes hard to forget, not as enforcement.

Out of scope:

- Quality or accuracy of LLM-generated wiki prose (report as a regular bug)
- Vulnerabilities in the content of repositories the tool is run against
- Claude Code itself (report to Anthropic)

## Supported versions

The latest commit on `develop` only. There are no tagged releases; install is a symlink of the working tree.
