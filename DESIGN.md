# akashic-record — Design

A repo wiki generator for Claude Code: it plans a page catalog, generates repo-committed
markdown with line-range citations into source, and incrementally updates only the pages
whose underlying files changed — while never overwriting human edits.

One sentence: **Qoder Repo Wiki class output, built as a Claude Code skill plus one
deterministic Python helper, keeping only the four patterns every surveyed system
converged on and deleting everything else.**

This document is the normative design. Research grounding: Qoder's on-disk format was
reverse-engineered from ~2,880 committed `.qoder/repowiki` files across ~180+ public
GitHub repos; the comparative study covered Cognition DeepWiki/Devin Wiki, DeepWiki-Open,
OpenDeepWiki, CodeWiki (ACL 2026), Mutable.ai Auto Wiki, and Google Code Wiki. Findings
were adversarially verified (3-vote). Key citations inline below.

---

## 1. Positioning and form factor

**Form factor: a Claude Code skill (`SKILL.md`) + one stdlib-only Python helper
(`akashic.py`).** Not a standalone CLI, not an MCP server, not a plugin.

Why the research forces this:

- The 14–17-point quality gap between the open-source clones and the closed pipelines
  (CodeWikiBench: closed DeepWiki 64.06% vs deepwiki-open 50.05% / OpenDeepWiki 47.13%)
  came from **pipeline structure** — catalog-first planning, per-page scoped prompts,
  agentic exploration — not from model access. A skill encodes exactly that structure as
  instructions; Claude Code supplies the model, Read/Grep/Bash tools, parallel subagents,
  and resumable orchestration for free.
- OpenDeepWiki's entire ASP.NET + polling-workers + EF Core apparatus exists only because
  it must orchestrate its own LLM calls. Inside Claude Code, that apparatus *is* the
  harness. A standalone CLI means API keys, retries, rate limits, provider abstraction,
  and a queue — hundreds of lines before the first page renders.
- The consumption layer is free (§6): DeepWiki's whole MCP surface is 3 tools
  (structure / contents / ask). For a local repo-committed wiki read by Claude Code,
  those degenerate to an index file, `Read`, and the session itself.
- CI still works headless: `claude -p "/akashic-record update"`.

Two audiences, one artifact:

1. **Humans** — onboarding/architecture docs with Mermaid diagrams, browsable on GitHub.
2. **Agents** — pre-digested context: "how does X work / what documents the file I'm
   about to edit," answered without re-exploring the codebase.

## 2. Division of labor (core invariant)

| Who | Does | Never does |
|---|---|---|
| LLM (skill-orchestrated) | overview, catalog planning, page prose, update triage | compute a hash, diff, or line range |
| `akashic.py` (stdlib only: `json subprocess hashlib pathlib re fnmatch`) | `scan`, `stale`, `verify`, `anchor` | call an LLM |

Everything that can lose human work or mark stale content as fresh is deterministic code.
Everything stochastic passes through a deterministic verification gate before it is
anchored. This split is the design's one non-negotiable.

## 3. On-disk format (written into the *target* repo, committed)

```
.akashic/
  catalog.json   # AUTHORITATIVE: settings + TOC plan + per-page state. The one metadata file.
  wiki/
    README.md    # DERIVED nested TOC, rendered by `anchor` from the catalog — never
                 # hand-edited, can never drift; the prose overview page is index.md
    <id>.md      # every page, FLAT — hierarchy lives only in catalog parent fields
  notes.md       # OPTIONAL free-text steering, injected verbatim into the planning prompt
```

Repo-committed markdown is deliberate: Qoder launched with an internal-index-only wiki
and was forced by users into in-repo markdown; OpenDeepWiki's DB storage (markdown in a
`DocFile.Content` column) is its most-criticized property — server-bound, not diffable,
not PR-reviewable. Git is the sync layer; teammates get the wiki via `git pull`.

### 3.1 Page identity

`id` = kebab-case slug derived from the page's **initial** title, uniquified, **frozen at
creation**. The id is a name, not a description — the title may drift; the id and
filename never do. This plus the flat directory fixes Qoder's verified rename problem
(their page identity is the localized human-readable title used as the filename inside a
mirrored folder tree, so retitling or reparenting breaks git history and inbound links).
Reparenting here is a one-line catalog edit; no file moves, ever.

### 3.2 catalog.json

```json
{
  "version": 1,
  "anchor": "<full 40-char sha or null before first anchor>",
  "generated": "<ISO-8601 UTC>",
  "language": "en",
  "exclude": ["vendor/**", "**/*.snap"],
  "max_files": 5000,
  "pages": [
    {
      "id": "auth-flow",
      "title": "Authentication Flow",
      "parent": "architecture",
      "goal": "Explain end-to-end auth: middleware chain, token issuance, refresh. Sequence diagram of the login path.",
      "scope": ["src/auth/**", "src/middleware/auth.ts"],
      "files": ["src/auth/jwt.ts", "src/middleware/auth.ts"],
      "status": "done",
      "hash": "sha256:…",
      "frozen": false
    }
  ]
}
```

- `goal` — the per-page generation brief (Qoder stores exactly this per catalog node,
  encrypted; here it is plaintext and user-editable).
- `scope` — glob allowlist; **doubles as generation input scope and coarse staleness
  trigger**. Globs (not exact paths) close Qoder's blind spot where a brand-new file in a
  documented module never matches any `dependent_files` list and so never triggers an
  update.
- `files` — exact paths recorded at anchor time as `scope-matched ∪ cited` (union is
  deliberately conservative: over-staleness costs a cheap regen; under-staleness costs a
  wrong wiki).
- `status: planned | done` — the resume checkpoint. An interrupted generation resumes by
  generating pages still `planned`. No separate checkpoint state machine (Qoder's
  `recovery_checkpoint` reduced to one enum).
- `hash` — sha256 of the page body **as the tool last wrote it** (normalization: strip
  frontmatter-free file as-is, CRLF→LF, strip trailing whitespace per line, strip
  trailing newlines, UTF-8 bytes). Never updated to match human text — its meaning is
  permanently "what the tool wrote," so edit detection is a pure comparison.
  **`null` is the bless signal**, set by `bless <id>` immediately after (re)writing a
  page; `anchor` records a new hash only for pages whose hash is null or whose content
  is unchanged, and preserves the recorded hash otherwise — a changed body under a
  non-null hash is a human edit, and re-hashing it would launder the `edited` marker
  away. Blessing is a subcommand rather than an instruction to edit `catalog.json`
  because it was the one metadata mutation left to the stochastic side, and it was
  two steps with no atomicity between them: a session dying after writing the page
  but before editing the JSON leaves the tool's own fresh output under the previous
  hash, which `stale` then reports as `edited` — the tool's work protected from the
  tool as if it were human work. `bless` validates the id and the page file, then
  writes once.
- `frozen: true` — user-pinned entry: re-planning must never remove or rewrite it
  (DeepWiki `pages`-list semantics).
- Glob semantics (`scope`, `exclude`): fnmatch with two gitignore-flavored
  affordances — a slash-free pattern matches basenames at any depth (`*.snap`), and a
  `**/` prefix also matches at the repo root (`**/*.snap` matches `top.snap`).
  `files` entries are exact paths, never patterns. `exclude` plus the built-in noise
  filter define the citable universe **once**: they filter `scan`, per-page `scope`
  expansion, and `uncovered` alike, so a broad `scope` glob cannot re-admit a file
  the catalog excluded. **Literal `[` is escaped before matching, so fnmatch
  character classes are not supported** — framework routing conventions (Next.js
  `[id]/route.ts`) put literal brackets in path segments far more often than a
  catalog wants a class, and reading `[id]` as a class made the natural glob for
  such a path match nothing. Every symptom was silent: files dropped from a page's
  citable set, files added under the scope never marking the page stale, and an
  `orphaned` false positive whose documented remediation deletes the page. `*` and
  `?` keep their glob meaning; note `*` still crosses `/` (making it
  single-segment would break existing catalogs and needs its own format change).
- One key per line, stable page ordering (array order = display order within a parent) →
  clean git diffs. A merge conflict in this file is a genuine semantic conflict about
  what the wiki should contain, small enough to resolve by hand. A 144-page Qoder wiki
  carries a 2.1 MB metadata blob with encrypted fields; the equivalent here is ~2 KB of
  reviewable JSON — of Qoder's five serialized relation types, the only one its update
  loop actually consumes is page→source-file, and that is the `files` array.

### 3.3 Page format

```markdown
# Authentication Flow

One-paragraph orientation.

## Login sequence

Prose. Plain Mermaid where a diagram earns its place (no style directives — matches all
sampled Qoder output; styling is the renderer's job).

Sources: [src/auth/jwt.ts:42-88](../../src/auth/jwt.ts#L42-L88), [src/middleware/auth.ts](../../src/middleware/auth.ts)

## Session storage

…

Sources: [src/auth/session.ts:1-40](../../src/auth/session.ts#L1-L40)

*Generated from commit `3f9c2e1` on 2026-07-16.*
```

Citation grammar (fixed, machine-parsed by `verify`):

- A paragraph starting with the literal token `Sources:` followed by comma-separated
  markdown links. One per H2 section; a `Sources:` line directly under a Mermaid block
  cites that diagram.
- Link target = a path relative to the page (repo root is `../../` from
  `.akashic/wiki/`), optional `#Lstart-Lend` fragment. These render as working,
  line-highlighting links on GitHub — unlike Qoder's non-standard `file://` scheme,
  which renders dead. When the link **text** is itself a file path containing a
  literal `[`/`]` (routing frameworks that name path segments this way — Next.js
  `[id]/route.ts` is the case that surfaced it), the destination may be wrapped in
  angle brackets per CommonMark (`(<../../app/[id]/route.ts#L1-L10>)`) instead of
  percent-encoding the brackets; the parser strips the wrapper before resolving.
  Destinations containing **balanced parentheses** — framework route groups such as
  Next.js `api/(cron)/route.ts` — are parsed natively, bare or angle-wrapped (one
  nesting level, which is all a real path needs); percent-encoding them is accepted
  but unnecessary. A destination parser that stops at the first `)` is not merely
  incomplete: the truncated prefix still resolves, so a real cited file gets
  reported as untracked instead of failing to parse.
- Line numbers are coordinates **in the anchor commit**.
- **A citation's end line may exceed the file's real line count by exactly one.**
  Any file ending in a trailing newline (nearly all of them) displays one extra,
  empty numbered line when read via Claude Code's Read tool — every subagent
  independently trusts that number, since it's the one they're looking at. Rejecting
  it would be pedantically correct (GitHub's own `#L<n>` anchors follow the `wc -l`
  convention, so the phantom line technically doesn't exist) but costs nothing to
  tolerate: the extra line is empty, so citing it highlights nothing extra on GitHub
  either way. `check_fragment` accepts `end <= real_lines + 1`; anything further out
  is still rejected as a real error.
- **`Sources:` stays in English regardless of wiki language.** Qoder localizes its
  structural markers in zh wikis, which breaks any parser; here machine-read tokens are
  locale-invariant and only prose localizes.
- **No `<cite>` header block.** The per-section lines are the single citation authority;
  Qoder's top-of-page cite block demonstrably drifts from its section footers (verified:
  pages exist whose footers cite files the header omits). A fact stored twice is a fact
  that diverges.

## 4. Pipeline (catalog-first — the universal pattern)

Every surveyed system plans the TOC before writing any page: DeepWiki's cluster-based
planning, OpenDeepWiki's `GenerateCatalogAsync` → `GenerateDocumentsAsync`, Qoder's
`wiki_catalogs` (each node carrying its own generation prompt + file allowlist),
CodeWiki's module-tree decomposition. So does this.

### Phase 0 — Scan (script)

`akashic.py scan` → filtered file tree with per-file line counts (Qoder's
`optimized_catalog`, unencrypted). Source of truth is `git ls-files` (gitignore respected
for free) minus binaries (null-byte sniff), lockfiles/vendored dirs (small built-in
denylist), and `exclude` globs. Caps at `max_files` (default 5,000) with a clear error
telling the user to add excludes.

**Hard precondition: git repo with ≥1 commit.** Otherwise fail with instructions, not
degraded behavior — the anchor model (§5) depends on it, and a wiki that cannot do
incremental updates honestly should not pretend it can. (Qoder has the same
precondition.)

### Phase 1 — Plan (LLM, main session)

Claude reads: scan output, README, `notes.md` if present, and — **opportunistically,
never as a dependency** — tokensave (`module_api`, `rank`, `hotspots`) or
`graphify-out/graph.json` if either exists, to identify modules, god files, and entry
points. This buys CodeWiki's static-analysis-first advantage (the benchmark leader) at
zero build cost; absent both, tree + README + targeted Grep suffices.

Output: the `pages` array in `catalog.json`. Guidance encoded in SKILL.md: 8–30 pages
scaled to repo size (DeepWiki caps at 30); every page a distinct `goal` and non-empty
`scope`; an `index` page always present; parents before children in array order. All
pages start `status: "planned"`.

Re-planning invariants (enforced by instruction + verify, not trusted to memory):
existing entries are matched by `id` and ids are immutable; titles/goals/scopes may
evolve; `frozen` entries pass through untouched; removals are reported, not silent.

### Phase 2 — Generate (N parallel subagents, one per page)

Each subagent's prompt is **rendered by `akashic.py prompt <id>`, never hand-written**:
the `goal`, the `scope`-expanded file allowlist (the *entire* citable set — nothing
outside it may appear in a `Sources:` line), the sibling id→title list for
`./other-id.md` cross-links, and the citation contract (§3.3). If the target repo has
local-only agent-guidance docs on disk that aren't git-tracked (`CLAUDE.md`,
`AGENTS.md`, `.cursorrules`, and similar — a common, deliberate convention, not a
mistake to work around), the renderer detects them and appends an explicit note: read
them for context, never cite them, cite the underlying tracked source instead. This
mechanical templating exists because the orchestrating agent hand-constructing this
same prompt from memory is exactly how the untracked-citation bug first shipped — a
human (or an LLM standing in for one) forgetting one rule while typing one prompt.
Moving the templating into the deterministic core removes that failure mode instead
of just documenting it.

The subagent explores its files with Read/Grep — agentic exploration, no embeddings —
and writes `wiki/<id>.md`. On success the orchestrator flips that page's `status` to
`done`.

Standing generation instructions that matter:

- **Cite-or-omit**: prefer "not documented here" over invention; every non-obvious claim
  carries a `Sources:` entry. (Mitigation for the verified cross-system failure mode:
  all benchmarked generators degrade sharply on C/C++ — ~53–56% vs ~79% on
  Python/JS/TS.)
- Header/impl pairs (`foo.h` + `foo.c`/`foo.cpp`) are treated as single scope units;
  build files (CMakeLists.txt, Makefile, package.json, …) always sit in the overview
  page's scope — project structure lives there, not in the file tree.
- Generation order: parents before children, so an interrupted run leaves a coherent,
  shallower wiki rather than orphaned leaves.
- **Scope, and only scope, is citable** — the rendered prompt states this as a hard
  boundary, but nothing enforces it at generation time; a subagent can still cite a
  real, correctly-resolved file outside its assigned scope (observed in practice: an
  overview page whose 5-file scope couldn't substantiate its own "orient to the whole
  package" goal, so the subagent grounded claims in five more files it read for
  context). `verify` now warns (not errors — the citation itself isn't wrong) whenever
  this happens, so the drift is visible instead of silent. Read it as a planning
  signal in both directions: scope that's too *wide* produces duplicated content
  (§7's admin-panel lesson); scope that's too *narrow* produces this.

### Phase 3 — Verify + anchor (script)

- `akashic.py verify` — the deterministic QA gate for stochastic output:
  - every `done` page exists at `wiki/<id>.md` with an H1;
  - every `Sources:` paragraph parses (wrapped continuation lines included; fenced
    code blocks ignored; a Sources block with zero parseable links is an error);
  - every citation resolves — **the path is tracked by git with exactly that
    spelling** (untracked/ignored/wrong-case paths can never appear in an
    anchor-to-HEAD diff, so they would make the page permanently fresh), the
    realpath stays inside the repo root (traversal guard), and the line range is
    within file bounds;
  - every internal `./<id>.md` link resolves to a page **id that exists in the
    catalog** — not to a file that exists on disk. A link to a page whose status
    is still `planned` is a valid forward reference (warned, not an error — a
    partial wiki is a valid state, per §5, and pages routinely cross-link the
    full sibling list before every sibling is generated); a link to an id absent
    from the catalog entirely is the real bug and still fails verify.
  Page ids are validated as slugs at catalog load in every command — an id is a
  path component, so a non-slug id is a traversal vector, rejected at the trust
  boundary.
  Failures print exact page/line and exit non-zero; the agent fixes or regenerates and
  re-verifies. The LLM phases have no unit tests — they have this gate.
- `akashic.py anchor` — the only metadata mutation point: parse citations from each
  `done` page, set `files = scope-matched ∪ cited`, record body hashes, stamp
  `anchor = HEAD` and `generated`, and render `wiki/README.md` (the derived TOC).
  Warns if the working tree is dirty (the wiki may describe uncommitted code).
  `.akashic/` paths are never recorded as dependencies and never count as staleness
  inputs — the wiki depending on itself would break the post-anchor invariant the
  moment its artifacts are committed.

Finally, the skill offers to add one line to the target repo's CLAUDE.md pointing at
`.akashic/wiki/README.md`. That single line is the entire "wiki as agent context"
integration.

## 5. Incremental update

The convergent mechanism across every shipped system — stored commit anchor + diff +
per-page file-dependency map; polling or manual trigger, never per-push webhooks
(OpenDeepWiki polls; Qoder anchors on `last_commit_id`; nobody watches pushes).

`akashic.py stale` (read-only, prints JSON):

1. `git cat-file -e <anchor>` — if the anchor is unreachable (force-push, shallow
   clone), say so and report **all pages stale** rather than guessing. The system may
   waste a regeneration; it must never mark stale content fresh.
2. `git diff --name-status -M <anchor> HEAD`. Renames count as both old and new path.
3. Intersect changed/deleted paths against each page's `files` (plus `scope` globs for
   added files) →

| Bucket | Meaning | Action (agent-side) |
|---|---|---|
| `stale` | a file the page depends on changed | regenerate (same Phase-2 contract); regenerated citations refresh `files` at anchor time, so renames and dependency drift self-heal |
| `edited` | current page hash ≠ recorded `hash` → a human touched it | **never auto-overwrite**; skip and warn by default; regenerate only on explicit request, passing the human text as immutable context to preserve |
| `orphaned` | every file the page documented is gone | auto-delete page + catalog entry (git is the backstop) — **unless also `edited`: then flag only; human text is never destroyed by automation** |
| `uncovered` | added files matching no page's scope∪files | if a coherent new module appeared, propose new catalog entries (visible as a catalog diff in review); otherwise note and ignore |

4. The update run ends: regenerate stale pages → `verify` → `anchor` → print a
   structured report (per page: reason, files, action) that becomes the commit message.

**Invariant (tested): immediately after `anchor`, `stale` returns empty.**

**No in-page "Update Summary" sections.** Qoder appends changelog bullets into each
updated page; they accumulate forever, pollute agent context, and drift. Pages here are
timeless; temporal data belongs to git (`git log -- .akashic/wiki/<id>.md` is the
changelog, the update report is the commit message).

```
ponytail: v1 staleness is file-level (any change to a dependent file = stale) and edit
detection is whole-page (one hash). Upgrade path when false-positive regens get
annoying: (a) interval-intersect diff hunks against cited #Lstart-Lend ranges — the
old-side hunk coordinates of `git diff anchor..HEAD` are already in anchor coordinates,
so overlap is integer math, no fuzzy matching; (b) per-H2-section hashes so regeneration
can preserve human-edited sections byte-for-byte instead of skipping the whole page.
The on-disk format already records everything both upgrades need.
```

## 6. Agent consumption (no MCP server)

DeepWiki's programmatic surface is exactly three MCP tools. For a repo-committed wiki
consumed by Claude Code, each maps to something that already exists:

| DeepWiki tool | akashic-record equivalent |
|---|---|
| `read_wiki_structure` | Read `.akashic/wiki/README.md` (rendered TOC) or `catalog.json` (machine form) |
| `read_wiki_contents` | Read `.akashic/wiki/<id>.md` |
| `ask_question` | the session itself: SKILL.md encodes "consult the wiki before exploring raw source; verify load-bearing claims against the cited lines" |

The wiki is a map, not the territory — each page's trailing anchor line says exactly how
stale it can be. An MCP server later is a thin stateless adapter over these three reads:
deferred, not foreclosed.

## 7. Steering

Verified adoption of steering files in the wild is ~1.6% (3 committed `wiki_plan.yaml`
against ~180+ wiki-committing repos). Defaults must carry the product; steering gets the
cheapest possible mechanism:

- **Ship:** `notes.md` (free-text, injected verbatim into planning — prose *is* the
  config format when the planner is an LLM; equivalent of DeepWiki `repo_notes` / Qoder
  `notes:` at zero schema cost) · `exclude` globs · `frozen` catalog entries · direct
  editing of `goal`/`scope`/`title` in catalog.json (it is plaintext by design).
- **Defer until asked:** templates (`architecture` | `product_requirement`), explicit
  page-allowlist schema, multi-language trees, per-phase model bindings, a
  `/knowledge`-style verb surface ("modify this page" is just a chat request that ends
  with `verify` + `anchor`).

## 8. Explicitly not building (YAGNI, with reasons from evidence)

| Not building | Why |
|---|---|
| Embeddings / RAG / vector store | embedding-first deepwiki-open scored bottom-tier (50.05%); agentic + structure wins; Grep exists |
| Database storage | OpenDeepWiki's biggest liability; Qoder was forced out of internal-index storage by users |
| Serialized knowledge graph (snippets, edge lists) | only 1 of Qoder's 5 edge types feeds its update loop — that one is the `files` array |
| Own AST / static analysis | tokensave + graphify already exist as optional planner signal; generator agents have Read/Grep |
| MCP server | adapter to ourselves (§6) |
| Watchers, webhooks, daemons | no shipped system triggers per-push; anchor + manual trigger is the convergent answer |
| Web UI / hosted rendering | GitHub renders the markdown and the Mermaid |
| Multi-language wikis | one `language` field reserved; parallel trees only when someone asks |
| Knowledge cards / conversation memory | Qoder's other two pillars; CLAUDE.md and Claude Code memory already fill these roles |
| Encryption of metadata | `WikiEncrypted:` is vendor lock-in, not architecture |
| Mermaid syntax validation | needs a node toolchain for a cosmetic failure; not a data-loss path |
| Cost tracking | the harness reports usage |
| Solving C/C++ degradation | every system fails there; documented limitation + contained mitigations (§4 Phase 2), not an engineering program |

**Not lazy about** (the standing exceptions): citation verification with repo-root
containment, anchor-reachability handling (fail loud, never guess), hash-based edit
protection (the tool's only data-loss vector), and the ≥1-commit precondition.

## 9. Tests

One `test_akashic.py` (stdlib `unittest`; fixtures are throwaway git repos built in
`tempfile`). The deterministic components each get the smallest check that fails if the
logic breaks:

| Component | Check |
|---|---|
| `scan` | fixture with a source file, a binary, an excluded glob → output contains the first, omits the rest |
| `stale` | pages A(f1) and B(f2); edit f1, delete f2, add uncovered f3 → exactly `{stale:[A], orphaned:[B], uncovered:[f3]}` — **the test that proves the incremental mechanism** |
| `verify` | valid citation passes; out-of-range line fails naming page+line; traversal path fails; missing path fails |
| `anchor` | invariant: post-anchor `stale` is empty; a manual page edit then flips it to `edited` |
| pipeline | acceptance (manual): run end-to-end on one small real repo; `verify` exits 0; wiki renders on GitHub |

LLM phases are covered by the runtime `verify` gate, not unit tests.

## 10. Build order

1. `akashic.py` — `scan`, `stale`, `verify`, `anchor` (~300 lines, stdlib only)
2. `test_akashic.py`
3. `SKILL.md` — the pipeline as instructions: phase prompts, per-page subagent contract,
   parallel-dispatch discipline, edit-protection rules, update flow, consumption recipe
4. `README.md`

This repo is the source of truth; install = copy/symlink into
`~/.claude/skills/akashic-record/`.

## 11. Open questions (carried, non-blocking)

- What DeepWiki's "cluster-based planning" actually clusters, and whether the closed
  pipeline uses embeddings at all.
- Contents of Qoder's `WikiEncrypted:` blobs; their exact deletion/rename/protected-edit
  reconciliation.
- Real-user complaint data (hallucinated architecture, staleness, cost) — research
  surfaced only benchmark-based failure modes; revisit after dogfooding.
