# Skill Orchestration

The akashic-record Claude Code skill is the conductor of the wiki pipeline: it decides when pages are (re)generated, dispatches one subagent per page under a strict, script-rendered page contract, and finishes every mutating run with the deterministic `verify` → `anchor` sequence. The skill itself never computes hashes, staleness, or subagent prompts, and no longer writes catalog metadata at all — that authority belongs to the helper script documented in [Deterministic Core](./deterministic-core.md); the skill's job is planning, dispatching, and obeying the script's reports. The resulting wiki lives under `.akashic/wiki/`, starting from [Project Overview](./index.md).

## Role and triggers

The skill generates a repo-committed wiki under `.akashic/wiki/` — architecture pages with plain Mermaid diagrams and per-section line-range citations into source — and keeps it fresh by regenerating only pages whose underlying files changed. It triggers on `/akashic-record` and phrases like "repo wiki", "generate wiki", "update the wiki", or "wiki status", and also when a repo already has `.akashic/` whose architecture docs could answer the question. The format spec is `DESIGN.md`, kept next to the skill.

Sources: [SKILL.md:1-11](../../SKILL.md#L1-L11)

## Hard rules

Four rules bind every flow:

1. **Script authority.** The script is the only authority for hashes, diffs, staleness, and citation validity. The agent never computes or guesses those, and never hand-edits the `anchor`, `hash`, `files`, or `generated` fields of `catalog.json` — with **no exception**. Immediately after (re)generating a page it runs `bless <id>` instead, which nulls that page's hash: the signal that the tool wrote the current text. `anchor` records a new hash only for pages whose hash is null or unchanged, and preserves the recorded hash of anything else, so human edits stay protected across anchors. The split of ownership is explicit — `goal`, `scope`, `title` and `parent` belong to the agent and the user; the four fields above belong to the script.
2. **Edit protection.** A page the script reports as `edited` is detected human work and is never overwritten. It is regenerated only when the user explicitly asks, and then the current human text is passed into the generation prompt as immutable context to preserve.
3. **Cite-or-omit.** Every claim a reader would act on carries a `Sources:` citation; anything that cannot be cited is not written.
4. **Always anchored.** Every mutating flow ends `verify` → fix until clean → `anchor`. The wiki is never left un-anchored.

Sources: [SKILL.md:13-30](../../SKILL.md#L13-L30)

## The helper script

All deterministic operations go through the helper, invoked as `python3 "<skill's base directory>/akashic.py" -C <target-repo> <command>`. Six commands exist: `scan` (filtered file list with line counts, the planner's input, as `lines<TAB>path`), `stale` (JSON staleness report with `stale/edited/orphaned/uncovered/missing` buckets), `verify` (checks pages, citations, and catalog invariants, exiting 1 on any error), `anchor` (records deps and hashes, stamps the anchor commit, renders the TOC), `prompt <id>` (renders the exact subagent prompt for one catalog page, as text meant to be dispatched verbatim), and `bless <id>...` (marks pages as tool-written by nulling their hash, with `--done` also flipping `status`). Exit codes are 0 for ok, 1 for verification failure, and 2 for precondition or usage errors (no git repo, no commits, too many files) — precondition errors are relayed to the user verbatim because they are actionable. The internals of these commands are covered in [Deterministic Core](./deterministic-core.md).

Sources: [SKILL.md:32-48](../../SKILL.md#L32-L48)

## Flow: generate (first run)

The first run starts with `scan` (stopping on precondition errors), then reads the repo's README, `.akashic/notes.md` if present, and any pre-existing agent-guidance docs on disk (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.windsurfrules`, `GEMINI.md`, `.github/copilot-instructions.md`, and similar) for domain and convention context — plus a tokensave MCP or `graphify-out/` graph when available, otherwise scan output and targeted Grep. Those context docs inform how a page's `goal` is written but never belong in a page's `scope`: `scope` is the citable dependency set, and local-only guidance files are routinely excluded from the shared repo on purpose, not by mistake. `scan`'s output defines the entire citable universe — a file read only for context that doesn't appear in scan cannot be cited by any page.

The skill then writes `.akashic/catalog.json` with planned page entries under fixed planning rules: 8–30 pages scaled to repo size; `index` first; parents before children in array order; kebab-case ids that are frozen forever (retitle freely, never re-id); every page a distinct `goal` and non-empty `scope`; C/C++ header/impl pairs share a scope; build files belong to the index page's scope; a scope matching more than ~200 files means the page should be split.

Every `planned` page is then generated by parallel subagents (all launched in one message), one per page. Each subagent's prompt is obtained by running `prompt <id>` — never hand-written — because the script is what fills in the scope-expanded file list, the sibling cross-links, and untracked-context-doc detection; hand-typing this per page is how the original CLAUDE.md/AGENTS.md citation bug happened. Only operational reminders (target repo path, "don't run git-mutating commands" if applicable) are appended to the rendered text. As each page's file lands, `bless <id> --done` flips its status and nulls its hash in one atomic write, so an interrupted run simply resumes by generating pages still `planned`. Then `verify` runs and every error is fixed (repair citations or regenerate the page) until exit 0, followed by `anchor`. Finally the skill offers to add a wiki pointer to the repo's CLAUDE.md and to commit `.akashic/` as `docs: generate repo wiki`.

```mermaid
sequenceDiagram
    participant O as Orchestrator (skill)
    participant A as akashic.py
    participant P as Page subagents
    O->>A: scan
    A-->>O: lines and paths (or precondition error)
    O->>O: plan catalog.json (8-30 pages)
    loop for each planned page
        O->>A: prompt <id>
        A-->>O: rendered page-contract prompt
        O->>P: dispatch prompt verbatim
    end
    P-->>O: wiki pages written
    loop for each page that landed
        O->>A: bless <id> --done
        A-->>O: status flipped, hash nulled
    end
    loop until exit 0
        O->>A: verify
        A-->>O: errors
        O->>O: repair citations or regenerate page
    end
    O->>A: anchor
    A-->>O: deps and hashes recorded, TOC rendered
```

Sources: [SKILL.md:50-105](../../SKILL.md#L50-L105)

## Page contract

The page contract is what `prompt <id>` renders — it is documented in `SKILL.md` for reference only, and the orchestrator must never hand-type it. Each subagent receives a fixed prompt: the page title and goal, the scope-expanded file list as its only citable sources, and the sibling id → title list for cross-links. The required output shape is exact: first line `# {title}` followed by a one-paragraph orientation; H2 sections, each ending with a citation paragraph — the literal token `Sources:` followed by comma-separated markdown links whose paths are relative to the page (repo root is `../../`), with optional `#L<start>-L<end>` fragments whose line numbers are exactly right in the current working tree.

The contract states a hard rule of its own: **only cite files from the given list.** `verify` independently rejects untracked, ignored, or wrong-case paths, but a subagent should not rely on that gate — if a fact came from somewhere outside its file list (for example something the orchestrator mentioned only for background), it must be restated without a citation rather than invented. `file://`, absolute paths, and URLs are forbidden on `Sources:` lines. A separate note covers a citation-accuracy trap: a file ending in a trailing newline shows one extra, empty numbered line when read, and `verify` tolerates that specific +1 — so a subagent should cite what its read of the file shows rather than burning effort re-deriving the "true" last line with `wc -l`.

Diagrams are plain Mermaid (no style directives), used only where they genuinely clarify, each with a `Sources:` line directly beneath. Sibling pages are cross-referenced as inline markdown links pointing at a sibling page's `<id>.md` file, cite-or-omit applies ("not documented here" beats invention), and the last line stamps the generating commit and date. Prose language is configurable, but structural tokens like `Sources:` and heading syntax stay as specified regardless of language.

Sources: [SKILL.md:107-135](../../SKILL.md#L107-L135)

## Flow: update

An update begins with `stale`. If the report says `anchor_reachable` is false, the user is told that everything regenerates and why (unreachable anchor — staleness is never guessed). Then each bucket is handled:

- `stale` — regenerate via the page contract, with an added instruction naming the changed dependencies and demanding a rewrite to match current code rather than an appended changelog; `bless <id>` is run on each regenerated page (the bless signal of hard rule 1).
- `missing` — regenerate from the catalog entry.
- `edited` — never touched (hard rule 2); listed for the user. A page in both `stale` and `edited` is reported as "stale but human-edited — needs manual review".
- `orphaned` — delete the wiki file and its catalog entry and report; an orphaned page that is also `edited` is never deleted.
- `uncovered` — if the paths form a coherent new module, add planned catalog entries (never removing or rewriting entries marked `frozen: true`) and generate them; otherwise mention and move on.

The flow closes with `verify` → fix → `anchor`, and a per-page report of what changed and what was done, phrased so it can serve as the body of the wiki commit message.

Sources: [SKILL.md:137-156](../../SKILL.md#L137-L156)

## Flow: status

Status is read-only: run `stale`, summarize the buckets in plain language, and change nothing.

Sources: [SKILL.md:158-160](../../SKILL.md#L158-L160)

## How agents consume the wiki

For architecture or "how does X work" questions in a repo with `.akashic/wiki/`, an agent reads `wiki/README.md` (the TOC), opens the relevant pages, and verifies load-bearing claims against the cited lines — the wiki is a map, not the territory, and each page's footer says which commit it describes. Before editing a source file, the agent greps `catalog.json` for that path in `files`/`scope` and reads the pages documenting it, gaining pre-digested context for the change.

Sources: [SKILL.md:162-169](../../SKILL.md#L162-L169)

*Generated from commit `56cb97a4` on 2026-08-07.*
