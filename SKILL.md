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
   validity. Never compute or guess any of those yourself, and **never edit the
   `anchor`, `hash`, `files`, or `generated` fields of `catalog.json` by hand** —
   there is no exception. Immediately after you (re)generate a page, run
   `bless <id>` instead; that nulls its hash, which is the signal that the tool
   wrote the current text. `anchor` records a new hash only for pages whose hash
   is null or unchanged, and preserves the recorded hash of anything else (human
   edits stay protected across anchors). `goal`, `scope`, `title` and `parent`
   are yours to edit; the four fields above are the script's.
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
| `scan` | filtered file list with line counts (planner input) | a `# N files, M lines` header, then `lines<TAB>path` per file |
| `stale` | staleness report | JSON: `stale/edited/orphaned/uncovered/missing/planned/drifted/restated/unblessed` |
| `stale --check` | same report, plus exit 1 when any bucket is non-empty | zero-token gate for a scheduled runner |
| `stale --ids <bucket>` | one page id per line for that bucket | plain text — loop over it directly, no JSON parsing |
| `verify` | check pages, citations, catalog invariants; warn on invented identifiers and on anchored content no citation covers | errors, exit 1 if any |
| `anchor` | record deps + hashes, stamp anchor commit, render TOC | summary |
| `prompt <id> [--update] [--out PATH]` | render the prompt for one catalog page; `--update` adds what changed, `--out` writes it to a file | text — dispatch it verbatim |
| `remap` | shift citations whose lines moved without changing; no LLM | summary |
| `plan-check` | shape checks on the catalog before a fan-out | JSON + warnings; always exit 0 |
| `plan-critic [--out PATH]` | render the adversarial plan-review prompt for the whole catalog | text — dispatch it verbatim |
| `audit extract [--page <id>]` | claim/evidence bundles per H2 section | JSON |
| `audit prompt <id> [--out PATH]` | render the blind refuter prompt for one page; `--out` writes it to a file | text — dispatch it verbatim |
| `bless <id>...` | mark pages as tool-written after regenerating them (`hash` → null); `--done` also flips `status` | summary |

Exit codes: 0 ok, 1 verification failure, 2 precondition/usage error (e.g. no git repo,
no commits, too many files — relay these to the user verbatim; they are actionable).

## Flow: generate (first run)

1. Run `scan`. Relay precondition errors and stop if it fails.
2. Read the repo's README, `.akashic/notes.md` if present (free-text steering — honor
   it during planning), and any pre-existing agent-guidance docs present on disk
   (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.windsurfrules`, `GEMINI.md`,
   `.github/copilot-instructions.md`, and similar) for domain and convention context.
   If a tokensave MCP or `graphify-out/` graph exists for this repo, use them to
   identify modules, hotspots, and entry points; otherwise the scan output plus
   targeted Grep is enough.

   **These context docs inform how you write a page's `goal`; they never belong in
   a page's `scope`.** `scope` is not "files worth reading" — it is the citable
   dependency set, and files like the ones above are routinely kept local-only via
   `.git/info/exclude` or a personal gitignore precisely so one person's AI-alignment
   notes don't leak into the shared repo (this is common and deliberate, not a
   mistake to work around). `scan`'s output already defines the citable universe —
   if a file you read for context doesn't appear there, it cannot be cited by any
   page, full stop; the page contract below enforces this at the point citations
   actually get written, not just here at planning time.
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

   **Split on how many similar things a page must describe at once, not on file count.**
   This is the single strongest lever on whether a page comes out true, and it was
   measured rather than assumed. A page covering three small modules with a handful of
   functions between them produced no over-generalizations, no miscounts and no
   self-contradictions. A page covering **one** file that declared 21 near-identical
   functions produced all three, exactly as the 13-file page it was split out of had.
   File count was a proxy; the count of parallel entities is the thing.

   So when a scope pulls in a family — twenty route handlers, fifteen sibling specs,
   a service exporting twenty senders — assume the page will write "every handler…"
   and get it wrong, and split until each page speaks about a handful. Roughly ten
   parallel entities is where it starts to break down.

   **Know the limit before you plan around it: scope globs cannot split a file.** A
   1,872-line module with 21 exported functions is one indivisible unit, so no split
   reaches inside it. When the entities are crowded into a single file, the rule cannot
   be satisfied and the page's `goal` is the only remaining lever — name the specific
   groups it must distinguish rather than letting it generalize across them.

   **A small scope has its own failure mode, so do not over-shrink.** A page whose
   citable set is too narrow starts making true claims it cannot cite — who calls this,
   what consumes that — because the answer lives in a file outside its scope.
   Cite-or-omit makes those defects even when the statement is correct. Cross-link
   instead, and if a page keeps needing a neighbour's files to explain itself, the split
   was cut in the wrong place.

   **Glob semantics** (`scope`, `exclude`), because getting these wrong is silent:
   patterns are `fnmatch`, not gitignore. `*` **crosses `/`**, so `src/lib/*` claims the
   entire subtree beneath it and an interior `*` is not one path segment. A slash-free
   pattern also matches the basename at any depth (`*.snap`), and a `**/` prefix also
   matches at the repo root. `[` is **literal**, so a framework route path can be written
   as it appears (`src/app/api/users/[id]/route.ts`) — fnmatch character classes are
   consequently not supported. There is **no negation**: when two scopes unavoidably
   overlap (`src/lib/services/users/*` also matches `notification_settings/`), the pages'
   distinct `goal` fields are what separate them, not the scopes. Full normative text:
   DESIGN.md §3.2.
4. Run `plan-check` and read it **before dispatching anything**. It is free and it looks
   at the real fan-out input: a scope matching no tracked file, a scope entirely inside a
   sibling's, a heavy pairwise overlap, a page over the ~200-file split rule, an empty
   goal, a duplicate title, and the expanded line total per page. Fix the catalog and
   re-run rather than dispatching a subagent that has nothing to cite or two that will
   read the same bulk and write the same prose. It is advisory and never blocks — some
   overlap is legitimate — so the judgment is yours; what is not acceptable is not
   looking. Its output is also what a plan-approval checkpoint should show a human.
   Then dispatch `plan-critic --out <tmp>` to **one subagent** ("read that file in full and
   follow it, read nothing else"), and act on what it returns
   before generating anything. `plan-check` answers the shape questions; this one answers
   the questions that need reading the code — whether a goal promises something the
   repo does not contain, whether what it promises is even inside its own scope, and
   whether two goals claim the same subject. It is the only check that can catch those:
   `verify` proves a citation resolves, never that a page wrote what it was asked to, and
   cite-or-omit means an under-scoped page does not fail, it quietly says less. One
   adversarial pass over the whole catalog costs a fraction of one page's generation.
   Fix the goals and scopes it names, re-run both checks, and only then fan out. Treat
   its verdict as **one sample**: a page it calls fine is not proven fine, and rerunning
   on an unchanged catalog is a legitimate way to find more, not a redundancy.
5. Generate every `planned` page with parallel subagents (all in one message), one per
   page. **Get each subagent's prompt by running `prompt <id>` — do not hand-write
   it.** The page contract below documents what that output looks like and why, but
   the script is the one that fills it in (scope-expanded file list, sibling
   cross-links, untracked-context-doc detection); hand-typing this per page is how
   the original CLAUDE.md/AGENTS.md citation bug happened. Append only the target repo path
   to the rendered text: the read-only mandate and the citable-file boundary are
   rendered by the script, precisely so they cannot be forgotten. Run `bless <id> --done` as each page's file
   lands — that flips `status` and nulls the hash in one atomic write.
   Interrupted? Just re-run: generate pages still `planned`.
6. Run `stale` and check the `planned` bucket is empty before going further. A page
   still listed there is one a subagent never wrote: `verify` and `anchor` both skip
   non-done pages, so nothing else in the pipeline will notice. Regenerate it or say
   so explicitly; never anchor a run you have not confirmed finished.
7. Run `verify`. Fix every error (repair citations or regenerate the page) and re-run
   until exit 0.
8. Run `anchor`.
9. Offer to (a) add `Repo wiki: .akashic/wiki/README.md (architecture + module docs with source citations)`
   to the repo's CLAUDE.md, and (b) commit `.akashic/` (`docs: generate repo wiki`).

## Page contract (what `prompt <id>` renders — reference only, do not hand-type)

> Write the wiki page **{title}** for the repository at {repo}.
> Goal: {goal}
> Read these files — this is also the *entire* set of files you may cite: {scope-expanded file list}
> Sibling pages for cross-links: {id → title list}
>
> Write to `.akashic/wiki/{id}.md`, exactly this shape:
> - First line: `# {title}`, then a one-paragraph orientation.
> - H2 sections. **Every H2 section ends with a citation paragraph**:
>   `Sources: [src/auth/jwt.ts:42-88](../../src/auth/jwt.ts#L42-L88), [README.md](../../README.md)`
>   — the literal token `Sources:`, comma-separated markdown links, paths relative to
>   the page (repo root is `../../`), optional `#L<start>-L<end>` with line numbers
>   that are exactly right in the current working tree. **Only cite files from the
>   list above** (verify independently rejects untracked, ignored, or wrong-case
>   paths, but don't rely on that gate — if a fact came from somewhere outside your
>   file list, e.g. something the orchestrator mentioned for background, restate it
>   without a citation rather than inventing one). Never `file://`, never absolute
>   paths, never URLs on Sources lines. **A file ending in a trailing newline shows
>   one extra (empty) numbered line when you Read it** — `verify` tolerates that
>   specific +1, so don't burn effort re-deriving the "true" last line with `wc -l`;
>   just cite what Read shows you.
> - Plain Mermaid (no style directives) only where a diagram genuinely clarifies;
>   put a `Sources:` line directly under each diagram.
> - Cross-reference sibling pages as `[Title](./other-id.md)` in prose.
> - Cite-or-omit: prefer "not documented here" over invention.
> - Last line: `*Generated from commit `{short-sha}` on {YYYY-MM-DD}.*`
> - Prose language: {language}. Structural tokens (`Sources:`, headings syntax) stay
>   as specified regardless of language.

## Flow: update

1. Run `stale`. If `anchor_reachable` is false, `anchor_state` says which of the two
   causes it is — `never_anchored` (a first run; just anchor) or `anchor_unreachable`
   (usually a shallow clone, or an anchor stamped on a commit a squash-merge
   discarded). Tell the user everything regenerates
   and why (unreachable anchor — never guess staleness).
2. Before regenerating anything, run `plan-critic` and read it. Not only when adding
   new entries — the pages about to be regenerated will be written from goals that may
   never have been reviewed. On its first real run against a 28-page repo it found
   defects in 16 of them, almost none related to new paths: a goal promising a "BOL
   public reference" that exists nowhere, a "public endpoint" that requires a token, a
   "house convention" holding in 5 of ~101 files. Regenerating from those ships every
   one of them. Fix the goals and scopes it names.
3. **Its fixes will usually widen a scope, which makes more pages `restated`.** That is
   expected, not a problem to route around. Default: act on findings for the pages you
   are already regenerating, apply the rest to the catalog too, and let the next `stale`
   pick them up — `restated` exists precisely so a corrected brief is not lost. If the
   expanded set is more than you want to spend now, say so explicitly and list what you
   deferred; never silently drop a finding.
4. Act per bucket:
   - `stale` → regenerate each page with `prompt <id> --update --out <tmp>`, which
     renders what changed into the prompt and keeps it out of your context; dispatch
     "read that file in full and follow it". `stale --ids stale` gives you the ids to
     loop over without parsing JSON. Never hand-append that context: `stale` knows the
     changed list and the script is what joins them. Run `bless <id>` on each page you
     regenerate (hard rule 1).
   - `drifted` → **run `remap`, do not regenerate.** The cited lines moved but their
     content did not, so the fix is arithmetic: `remap` shifts each fragment and its
     human-readable text, blesses the page, and costs nothing. Regenerating these would
     spend a subagent to retype prose that was already correct.
   - `restated` → the page's *brief* changed, not its code: a goal was edited or a
     scope was widened onto a file that already existed. Regenerate with
     `prompt <id> --update --out <tmp>`, which names which of the two it was. This is where
     `plan-critic`'s findings land — acting on them used to write into the catalog and
     never reach the page.
   - `missing` → regenerate from the catalog entry.
   - `planned` → a catalog entry with no page file at all: nobody generated it. Generate
     it exactly as the generate flow does, then `bless <id> --done`. The loop routes a
     planned-only repo straight into this flow, so it needs an entry here even though it
     is normally a generate-run state. Contrast `unblessed` below: same non-`done`
     status, opposite remedy, because there the file already exists.
   - `unblessed` → a page file exists but its catalog entry was never accepted, which
     means a generation subagent wrote it and then died before `bless`. **Read the page
     before deciding.** If it is complete, `bless <id> --done` accepts it as written and
     costs nothing. Only regenerate if it is truncated or wrong — regenerating by reflex
     throws away a finished page unread.
   - `edited` → **do not touch** (hard rule 2). List them for the user. A page in both
     `stale` and `edited` is reported as "stale but human-edited — needs manual review".
   - `orphaned` → delete `wiki/<id>.md` and its catalog entry; report. Never delete a
     page that is also `edited`.
   - `uncovered` → three outcomes, and the middle one is the commonest:
     - the paths **belong inside an existing page's module** → widen that page's `scope`
       to include them and mention it. The page becomes `restated` and regenerates on
       that basis. This is what a new file in an already-documented module usually
       needs, and it is what the first external run actually hit.
     - the paths **form a coherent new module** → add planned catalog entries (never
       remove or rewrite entries marked `frozen: true`), run `plan-check` and read it
       before dispatching (a new entry is a new fan-out, and a scope bolted on next to
       existing ones is exactly where an unreachable or duplicated scope appears), then
       `plan-critic` — a goal written for a module nobody has read yet is exactly the
       goal written from filenames — then generate them.
     - neither → mention and move on.
5. `verify` → fix → `anchor`.
6. Report per page: what changed, what was done — phrased so it can serve as the body
   of the wiki commit message.

## Flow: audit (on demand only)

Reach for this when a page smells wrong — never on a schedule, never as a gate. Run
`audit prompt <id> --out <tmp>` and dispatch to **one subagent with no other context**:
"read that file in full and follow it, read nothing else". Without `--out` the whole
prompt passes through your context and then through the judge's, which for a large
page is 200KB paid twice for no benefit. It renders every claim on the page beside the exact bytes that claim cites,
with the files labelled `[E1]`, `[E2]` instead of named, so the judge cannot fill gaps
with what a file of that name usually contains — the failure mode behind every planning
defect this project has recorded.

It answers the one question nothing else can. `verify` proves a citation resolves, the
anchored-content warning proves it still points where it was anchored, the identifier
warning proves a named symbol exists somewhere the section cites; none of them can say
whether the sentence is true.

Translate findings back to paths with `audit extract --page <id>`, which carries the
label→path mapping. Then fix the prose, `bless <id>`, `verify`, `anchor`. Findings are
usually one of three: **contradicted**, **unsupported** (the commonest — a true claim
the page cannot support from its own scope, which means widening the scope or dropping
the claim), or **overstated**.

## Flow: status

Run `stale`, summarize buckets in plain language, change nothing. Add `plan-check` when the question is about the plan rather than freshness — it reads nothing but the catalog and the file tree, and changes nothing either.

## Consumption (reading the wiki)

- Architecture / "how does X work" questions in a repo with `.akashic/wiki/`: read
  `wiki/README.md` (TOC), open the relevant pages, and **verify load-bearing claims
  against the cited lines** — the wiki is a map, not the territory; each page's footer
  says which commit it describes.
- Before editing a source file: grep `catalog.json` for its path in `files`/`scope`
  and read the pages that document it — pre-digested context for the change.
