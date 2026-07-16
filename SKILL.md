---
name: akashic-record
description: Generate and incrementally maintain a repo wiki (.akashic/wiki) with line-range source citations. Use when asked to generate or create a repo/project wiki, update or refresh the wiki, check wiki status, or when a repo already has .akashic/ and its architecture docs could answer the question. Triggers - /akashic-record, "repo wiki", "generate wiki", "update the wiki", "wiki status".
---

# akashic-record

Generates a repo-committed wiki under `.akashic/wiki/` — architecture pages with plain
Mermaid diagrams and per-section line-range citations into source — and keeps it fresh
by regenerating only pages whose underlying files changed. Format spec: `DESIGN.md`
next to this file.

## Hard rules

1. **The script is the only authority** for hashes, diffs, staleness, and citation
   validity. Never compute or guess any of those yourself; never edit the `anchor`,
   `hash`, `files`, or `generated` fields of `catalog.json` by hand — only `anchor`
   writes them.
2. **Never overwrite a page the script reports as `edited`.** That is detected human
   work. Regenerate it only when the user explicitly asks, and then pass the current
   human text into the generation prompt as immutable context to preserve.
3. **Cite-or-omit**: every claim a reader would act on carries a `Sources:` citation;
   if you cannot cite it, do not write it.
4. Every mutating flow ends `verify` → fix until clean → `anchor`. Never leave the wiki
   un-anchored.

## The helper

```
python3 "<this skill's base directory>/akashic.py" -C <target-repo> <command>
```

| Command | Does | Output |
|---|---|---|
| `scan` | filtered file list with line counts (planner input) | `lines<TAB>path` |
| `stale` | staleness report | JSON: `stale/edited/orphaned/uncovered/missing` |
| `verify` | check pages, citations, catalog invariants | errors, exit 1 if any |
| `anchor` | record deps + hashes, stamp anchor commit, render TOC | summary |

Exit codes: 0 ok, 1 verification failure, 2 precondition/usage error (e.g. no git repo,
no commits, too many files — relay these to the user verbatim; they are actionable).

## Flow: generate (first run)

1. Run `scan`. Relay precondition errors and stop if it fails.
2. Read the repo's README and `.akashic/notes.md` if present (free-text steering —
   honor it during planning). If a tokensave MCP or `graphify-out/` graph exists for
   this repo, use them to identify modules, hotspots, and entry points; otherwise the
   scan output plus targeted Grep is enough.
3. Write `.akashic/catalog.json`:

   ```json
   {
     "version": 1, "anchor": null, "generated": null, "language": "en",
     "exclude": [], "max_files": 5000,
     "pages": [
       {"id": "index", "title": "Project Overview", "parent": null,
        "goal": "<one-to-three sentence generation brief>",
        "scope": ["README.md", "<build files>", "src/main.*"],
        "files": [], "status": "planned", "hash": null, "frozen": false}
     ]
   }
   ```

   Planning rules: 8–30 pages scaled to repo size; `index` first; parents before
   children in array order; ids are kebab-case and **frozen forever** (retitle freely,
   never re-id); every page a distinct `goal` and non-empty `scope`; C/C++ header/impl
   pairs live in the same scope; build files (`Makefile`, `CMakeLists.txt`,
   `package.json`, `pyproject.toml`, …) belong to the index page's scope. A scope
   matching more than ~200 files means the page should be split.
4. Generate every `planned` page with parallel subagents (all in one message), one per
   page, using the page contract below. Flip each page's `status` to `"done"` as its
   file lands. Interrupted? Just re-run: generate pages still `planned`.
5. Run `verify`. Fix every error (repair citations or regenerate the page) and re-run
   until exit 0.
6. Run `anchor`.
7. Offer to (a) add `Repo wiki: .akashic/wiki/README.md (architecture + module docs with source citations)`
   to the repo's CLAUDE.md, and (b) commit `.akashic/` (`docs: generate repo wiki`).

## Page contract (subagent prompt template)

> Write the wiki page **{title}** for the repository at {repo}.
> Goal: {goal}
> Read these files (your only sources): {scope-expanded file list}
> Sibling pages for cross-links: {id → title list}
>
> Write to `.akashic/wiki/{id}.md`, exactly this shape:
> - First line: `# {title}`, then a one-paragraph orientation.
> - H2 sections. **Every H2 section ends with a citation paragraph**:
>   `Sources: [src/auth/jwt.ts:42-88](../../src/auth/jwt.ts#L42-L88), [README.md](../../README.md)`
>   — the literal token `Sources:`, comma-separated markdown links, paths relative to
>   the page (repo root is `../../`), optional `#L<start>-L<end>` with line numbers
>   that are exactly right in the current working tree. Never `file://`, never
>   absolute paths, never URLs on Sources lines.
> - Plain Mermaid (no style directives) only where a diagram genuinely clarifies;
>   put a `Sources:` line directly under each diagram.
> - Cross-reference sibling pages as `[Title](./other-id.md)` in prose.
> - Cite-or-omit: prefer "not documented here" over invention.
> - Last line: `*Generated from commit `{short-sha}` on {YYYY-MM-DD}.*`
> - Prose language: {language}. Structural tokens (`Sources:`, headings syntax) stay
>   as specified regardless of language.

## Flow: update

1. Run `stale`. If `anchor_reachable` is false, tell the user everything regenerates
   and why (unreachable anchor — never guess staleness).
2. Act per bucket:
   - `stale` → regenerate each page (page contract, plus: "This page existed; its
     dependencies {changed} changed since the last anchor. Rewrite it to match the
     current code — do not append a changelog.").
   - `missing` → regenerate from the catalog entry.
   - `edited` → **do not touch** (hard rule 2). List them for the user. A page in both
     `stale` and `edited` is reported as "stale but human-edited — needs manual review".
   - `orphaned` → delete `wiki/<id>.md` and its catalog entry; report. Never delete a
     page that is also `edited`.
   - `uncovered` → if the paths form a coherent new module, add planned catalog
     entries (never remove or rewrite entries marked `frozen: true`) and generate them;
     otherwise mention and move on.
3. `verify` → fix → `anchor`.
4. Report per page: what changed, what was done — phrased so it can serve as the body
   of the wiki commit message.

## Flow: status

Run `stale`, summarize buckets in plain language, change nothing.

## Consumption (reading the wiki)

- Architecture / "how does X work" questions in a repo with `.akashic/wiki/`: read
  `wiki/README.md` (TOC), open the relevant pages, and **verify load-bearing claims
  against the cited lines** — the wiki is a map, not the territory; each page's footer
  says which commit it describes.
- Before editing a source file: grep `catalog.json` for its path in `files`/`scope`
  and read the pages that document it — pre-digested context for the change.
