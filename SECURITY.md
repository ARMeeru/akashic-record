# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/ARMeeru/akashic-record/security/advisories/new). Do not open a public issue for security reports.

You should receive an acknowledgment within 7 days. This is a solo-maintained project; response and fix timelines are best-effort, and coordinated disclosure after a fix lands is appreciated.

## Scope

akashic-record is a Claude Code skill plus one stdlib-only Python script — no dependencies, no network access. It reads a target git repository and writes generated wiki files into that repository's `.akashic/` directory.

In scope:

- Path traversal out of the target repository root (citation resolution, page-id handling)
- Writes escaping `.akashic/` in the target repository
- Anything that causes stale or human-edited wiki content to be reported as fresh (edit-protection or hash-semantics bypass)
- Bypasses of the `verify` gate (citations accepted that should be rejected)

Out of scope:

- Quality or accuracy of LLM-generated wiki prose (report as a regular bug)
- Vulnerabilities in the content of repositories the tool is run against
- Claude Code itself (report to Anthropic)

## Supported versions

The latest commit on `develop` only. There are no tagged releases; install is a symlink of the working tree.
