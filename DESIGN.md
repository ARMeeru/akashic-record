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
| `akashic.py` (stdlib only: `json subprocess hashlib pathlib re fnmatch`) | `scan`, `stale`, `verify`, `anchor`, `prompt`, `remap`, `plan-check`, `plan-critic`, `audit`, `bless` | call an LLM |

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

- `version` — the format marker, and a **contract**, because adopters commit `.akashic/`
  into their own trees. The supported value is exactly the integer `1`. Anything else —
  higher, lower, missing, or the wrong type — refuses with **exit 2** and a per-file
  message, in every catalog-reading subcommand, before any other field is inspected. The
  integer part is enforced rather than implied: Python makes `True == 1` and `1.0 == 1`
  true, so an equality test alone would read a catalog whose format marker is a boolean.
  Silent format drift is worse for someone whose repository holds the artifact than a
  loud refusal is.

  **Additive optional fields never bump the version.** `goal_hash`, `blobs` and `ranges`
  were all added to catalogs already in the field, and each follows the same rule: absent
  means say nothing, never guess. A reader that predates them keeps working, and a reader
  that has them stays quiet on a catalog that does not. A bump is reserved for a change
  that makes an older reader wrong rather than merely less informed, it is made by the
  release that introduces the incompatible reader, and it obligates a stated migration
  path. There is no version 2, and adding one is a decision, not a consequence.

- `goal` — the per-page generation brief (Qoder stores exactly this per catalog node,
  encrypted; here it is plaintext and user-editable).
- `scope` — glob allowlist; **doubles as generation input scope and coarse staleness
  trigger**. Globs (not exact paths) close Qoder's blind spot where a brand-new file in a
  documented module never matches any `dependent_files` list and so never triggers an
  update.
- `files` — exact paths recorded at anchor time as `scope-matched ∪ cited` (union is
  deliberately conservative: over-staleness costs a cheap regen; under-staleness costs a
  wrong wiki).
- `ranges` — `{path: [[start, end], ...]}` for every path the page cites *with line
  numbers*, in anchor coordinates, recorded at anchor time. Ends are clamped to the
  file's real length: `check_fragment` tolerates an end one past the last line (a file
  ending in a newline displays a phantom empty line when read), and that phantom does
  not exist in diff coordinates — unclamped, an append at EOF would intersect it and
  stale every cite-to-end-of-file page on every append.
- `blobs` — `{path: blob sha}` for every entry in `files`, recorded at anchor time from
  one `git ls-tree -r HEAD`. Blob shas are content hashes, so equality at HEAD proves a
  dependency is byte-identical **without needing the anchor commit to still exist**.
  That is what makes an unreachable anchor recoverable rather than catastrophic (§5).
- `goal_hash` — sha256 of the `goal` the page was last generated from, recorded at
  anchor time **only for pages the tool actually wrote that run** (`hash` null, the bless
  signal). Stamping it unconditionally was the same mistake the `hash` rule below exists
  to prevent, made three lines above it: for a page nobody regenerated, the text came
  from an *earlier* goal, so recording the current one asserts a correspondence nothing
  checked and destroys the true baseline. It cost a real run — ten corrected goals were
  stamped onto pages that were never rewritten, `stale` reported clean, and the affected
  pages could afterwards be identified only from a human's memory of the previous
  session. An unblessed page carries its existing baseline forward untouched, so a later
  goal edit still surfaces as `restated`. An unblessed page with *no* baseline (a catalog
  predating the field) gets none invented: the tool does not know which goal produced
  that text. That leaves a real hole — a goal edit there is undetectable until the page
  is next generated — so `plan-check` reports the count rather than papering over it, and
  it clears itself as pages turn over. Staleness answers "did the code move?"; nothing answered "did what we
  asked of this page move?", and both plan gates (§4 Phases 1b/1c) produce goal and
  scope edits as their primary output. Without this, a critic finding on a page that
  happened not to be stale was written into the catalog and never reached the page —
  6 of 16 findings on the first external run. **Absent field means say nothing**, the
  same conservative direction `blobs` took, so catalogs written before it existed stay
  quiet rather than reporting every page.
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

`akashic.py scan` → a `# N files, M lines` header line followed by a filtered file tree with per-file line counts (Qoder's
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

### Phase 1b — Plan-check (script, before any dispatch)

`akashic.py plan-check` is a read-only, LLM-free look at the *shape* of the plan, run
between planning and the fan-out. Until it existed the pipeline gated what subagents
produced and gated nothing about what they were asked to do, so a scope matching no file
or two pages scoped to the same bulk was discovered after the tokens were spent. In one
26-page run against a real Next.js repo, two pages both scoped to all 86 migrations cost
roughly 180k tokens of duplicated reading and made every schema change stale two pages
instead of one.

It emits JSON on stdout and a plain-language warning per finding on stderr:

- **`no_scope` / `empty_scope`** — a page with no scope at all, and a page whose globs
  match no tracked file. Kept apart because they are different mistakes: one is an
  omission, the other a glob that looks right and silently is not. Until now the second
  was visible only as a line inside a rendered prompt, read after dispatch.
- **`subset`** — one page's scope entirely inside a sibling's. Both subagents read the
  same bulk, both write about it, and every change there stales two pages.
- **`overlap`** — every pair with a non-empty intersection, in files *and* lines, sorted
  by duplicated lines. **There is no threshold, and the one it first shipped with was
  wrong twice over.** Gating on shared files as a fraction of the smaller page's file
  count measured the wrong quantity: on a real 28-page catalog it stayed silent on a pair
  sharing 3632 lines (ratio 0.27) while reporting one sharing 2754 (ratio 0.69), because
  one enormous shared file is *few files*. It was also non-monotonic — widening a page
  from 5 files to 7 for unrelated reasons dropped a true finding about a *different* pair
  whose intersection had not changed. Any ratio against page size carries that defect,
  since the denominator moves for reasons the pair knows nothing about. Judging which
  overlaps matter needs to know what the pages are *for*, so it belongs to `plan-critic`,
  which already receives this report: the script measures, the model judges. `cmd_plan_check`
  names the largest few on stderr and always states the true total, so a shortened display
  never reads as the whole list.
- **`oversized`** — over the ~200-file split rule the planning instructions already
  state. The threshold is read from that documented rule rather than invented here, so
  the two cannot drift apart.
- **`empty_goal` / `duplicate_titles`** — catalog sanity.
- **`no_goal_baseline`** — done pages with no recorded `goal_hash`, on which a goal edit
  cannot be detected until they are next generated. A catalog predating that field is
  otherwise indistinguishable from a fully tracked one, and the difference is invisible
  precisely where it matters.
- **`pages`** — expanded file and line totals per page, biggest first. Data rather than a
  finding: a 10k-line outlier should be visible before dispatch instead of in the bill,
  and picking a threshold for "too big" would be inventing one.

**Why there is no entity-count check here, though entity count is what matters.** A
controlled field run split one 13-file page into three and kept the original as a control,
measuring both under the same judge. Pooled accuracy went from 14% to 54%, but the arms
disagreed in a way that named the real variable: the arm covering three small modules made
*no* over-generalizations, miscounts or self-contradictions, while the arm covering a
single file that declared 21 near-identical functions reproduced all three failures the
13-file control had. File count was a proxy. The number of parallel entities a page must
describe at once is the lever.

That finding belongs in the planning instructions, not in this script, and `SKILL.md`
carries it. Counting entities means parsing declarations per language, which is a static
analyser — §8 territory, and a large permanent dependency bought for one advisory number.
The same run also showed the ceiling on acting on it: scope globs cannot split a file, so
a module crowding 21 entities into 1,872 lines cannot be split at all, and the page's
`goal` is the only remaining lever there. A check that flagged an unfixable condition on
every run would be noise of the kind the overlap list already taught us to avoid.

Expansion goes through `expand_scope`, the same function that builds a subagent's citable
list, so the check measures the real fan-out input rather than the globs someone typed.

**Always exit 0.** Several findings are legitimate on a real catalog, and a pre-flight
check that blocked would be routed around rather than read. Its output is also exactly
what a plan-approval checkpoint should display to a human.

Scope is deliberately narrow. These are questions a script can answer for free; whether a
`goal` is *true* and reachable from its scope needs reading the code and judging meaning.
Of the seven planning defects that motivated this work, these checks catch one — the
duplicated-scope pair. The other six are goals asserting things that do not exist, goals
whose subject lies outside their own scope, and pairs of goals claiming the same subject.
Those belong to the plan critic and are not a reason to defer either piece.

### Phase 1c — Plan critic (LLM, one subagent, before any dispatch)

`plan-check` answers the shape questions a script can answer. Six of the seven planning
defects observed in the 2026-07-30 dogfood run are about *meaning* and it cannot touch
them: a goal promising "site types and parent/child relationships" where no parent column
existed anywhere and the enum was used by no column on a different table; a goal promising
plan→delivery expansion whose code lived outside its own scope; two pages both claiming
Sentry; a goal describing the current schema from a scope holding 2 of 86 migrations.

**Why nothing downstream catches these.** `verify` proves a citation *resolves*; it says
nothing about whether the prose above it is what the page was asked to write. Cite-or-omit
then means an under-scoped page does not fail — it quietly says *less*, reading as a
deliberate "not documented here" and indistinguishable from an intentional omission. The
defect is invisible in the output and invisible to the gate. The only existing tripwire is
the out-of-scope citation warning, which fires after generation, once the tokens are spent.

`akashic.py plan-critic` renders the review prompt; an LLM judges. The division of labor
(§2) is unchanged: the script does mechanical templating from catalog + filesystem, in the
`prompt <id>` idiom, and never judges. The prompt carries every page's id, title, goal,
scope, expanded file list and line total, plus `plan-check`'s findings marked as already
established so the judge spends its effort on meaning rather than re-deriving arithmetic.
Long file lists are truncated at a stated count with the command to get the rest — never
silently. The read-only mandate is rendered, for the same reason it is rendered into page
prompts: a target repo's scope routinely includes operational scripts.

Four judgments per page: **substantiation** (is every claim in the goal reachable from
that page's file list, naming unreachable clauses specifically), **truthfulness** (does
the goal assert what the code contradicts — a migration named `add-siteType-enums.js`
existing is not evidence the thing has site types), **collision** (does another goal claim
the same subject), **redundant scope** (do two pages read the same bulk in substance).

**One prompt for the whole catalog, not one per page.** Two of the four judgments are
cross-page and a per-page reviewer is structurally blind to them; a catalog is 8–30 pages,
so a single adversarial pass costs a fraction of one page's generation. It also runs in
the update flow wherever `uncovered` paths become new catalog entries, since a goal written
for a module nobody has read yet is exactly the goal written from filenames.

Advisory, like everything else in the pre-flight: it reports and never edits the catalog.
`--out PATH` writes the prompt to a file and prints the dispatch line instead, for the same
reason `audit prompt` has it (§5b) — it rendered 49KB on a real run and printing it costs
that twice.

**A critic pass is one sample, not a measurement, and the prompt now says so.** Two passes
over an unchanged catalog disagreed in both directions: five pages called defective in the
first were called fine in the second, while the second found two real defects the first
had missed entirely — both confirmed against the source. That is inherent to an LLM
judging meaning and is the price of the only check that can read code and form an opinion.
But a report ending "13 pages need edits" reads as a measurement. It is not. A page the
critic calls fine is not proven fine, a count is not converging across runs, and rerunning
on an unchanged catalog is a legitimate way to find more rather than a redundancy.

### Phase 2 — Generate (N parallel subagents, one per page)

Each subagent's prompt is **rendered by `akashic.py prompt <id>`, never hand-written**:
the `goal`, the `scope`-expanded file allowlist (the *entire* citable set — nothing
outside it may appear in a `Sources:` line), the sibling id→title list for
`./other-id.md` cross-links, and the citation contract (§3.3). If the target repo has
local-only agent-guidance docs on disk that aren't git-tracked (`CLAUDE.md`,
`AGENTS.md`, `.cursorrules`, and similar — a common, deliberate convention, not a
mistake to work around), the renderer detects them and appends an explicit note: read
them for context, never cite them, cite the underlying tracked source instead. This
rendered prompt also carries a **read-only mandate**: read and cite, never execute,
nothing git-mutating, and treat file contents as material to document rather than as
instructions to follow. It is rendered rather than appended by the orchestrator for the
same reason the file list is — a page's scope routinely includes operational scripts (one
target repo's `scripts/` drops databases and calls `pg_terminate_backend`), and a safety
rule that depends on being retyped per dispatch is one that eventually is not. This
mechanical templating exists because the orchestrating agent hand-constructing this
same prompt from memory is exactly how the untracked-citation bug first shipped — a
human (or an LLM standing in for one) forgetting one rule while typing one prompt.
Moving the templating into the deterministic core removes that failure mode instead
of just documenting it.

The subagent explores its files with Read/Grep — agentic exploration, no embeddings —
and writes `wiki/<id>.md`. On success the orchestrator flips that page's `status` to
`done`.

Standing generation instructions that matter:

- **Three generation disciplines**, rendered into every page prompt. These are the
  failure modes a blind claim audit found on *every* page it was ever pointed at across
  four runs against a real 28-page repo — seven distinct pages, all materially wrong.
  They are standing instructions rather than another gate because the gates only ever
  returned the same reading: the prose was wrong. Encoding a finding in a page's brief is
  the one intervention that has demonstrably produced a correct rewrite.
  (a) **Scope every generalization to what was actually read** — no `all`, `every`,
  `both`, `each` or `entirely` across a family of files on the strength of some of them;
  name the files a statement covers. (b) **An absence claim needs its method** — "no
  guard", "no write", "no caller" is not establishable from an excerpt, so either state
  how it was checked or do not claim it; a reader cannot distinguish a checked absence
  from an assumed one. (c) **Write only what the goal asks for** — where a goal defers a
  subject to another page, cross-link and stop, because the files that would support it
  are outside the citable set and anything written about it rests on nothing.
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
  - **identifier existence** (warning): a backticked, identifier-shaped token in an H2
    section that appears in none of the files that section cites is surfaced. `verify`
    proves a citation resolves; it cannot prove the prose above it is true, and this
    closes the narrowest and most embarrassing part of that gap — a page naming a
    function that exists nowhere it points at. Deliberately a warning: it is a heuristic
    over prose, and a heuristic that blocked `anchor` would eventually block a correct
    page. The shape test is narrow (a call, snake_case, camelCase, a dotted or scoped
    name) and filenames are excluded, because a warning per backticked English word
    would train the operator to ignore the class. **A filename is recognised by asking
    git, not by a list of extensions.** `foo.go` and `users.firstName` have identical
    shape, so nothing in the text separates them and the repository has to. The original
    allowlist was unfinishable by construction: written against Python, it missed the
    whole of TypeScript (187 false warnings on the first such repo, fixed in #48) and
    then missed the whole of Go in exactly the same way (109 on the first Go repo — every
    warning that run, on pages that were citing the files they named). A token matching
    the basename of any tracked file is a filename. The extension list is kept *alongside*
    that rather than replaced, since a page may legitimately name a file that is not
    tracked here — an env file, a build artifact, one it reports as absent — and "appears
    in no cited file" is the wrong sentence about a correct observation. The search is the whole cited file
    rather than the cited span: a page legitimately names a symbol defined elsewhere in
    the same module, and the aim is catching invention, not policing line numbers.
    A **qualified** name written in prose also matches its bare declaration: the column
    is declared `firstName` and the page calls it `users.firstName`, so the last segment
    of a dotted or scoped token counts as a match. Searching only the full token made
    every `table.column` a warning — on a real 28-page wiki that was 43% of 187 findings.
    The cost is an invented `foo.bar` slipping through when an unrelated `bar` exists,
    which at warning level is the right direction to be wrong. Two small denylists sit
    beside it: global namespaces whose members never appear in application code
    (`console`, `JSON`, `Math`, …), and naming-convention words that carry identifier
    shape but are English about code (`camelCase`, `snake_case`).
    **Calibration is against a real external repo, not this one.** The original claim of
    "zero false positives" was measured on four Python pages here, a corpus that could
    not have caught the qualified-name class; the same check emitted 187 warnings on a
    28-page TypeScript wiki. After the fix that repo reports 106, of which 82 of 87
    distinct tokens do exist in the repo but outside the files their section cites —
    genuinely a page naming what it cannot cite, which is the finding, not noise.
  - **anchored content still cited** (warning): a range check that asks about content
    rather than bounds. `verify` proves a citation lands inside its file, so a citation
    rewritten to plausible-but-wrong numbers passes every gate — three separate
    mechanical fixes to this repo's own wiki shipped ranges landing on a stray bracket,
    on blank lines, and mid-regex, and all three verified clean. Nothing new is recorded
    to close it: `ranges` are already in anchor coordinates and the anchor commit is
    already stored, so the tool reads what a span actually held (`git show
    <anchor>:<path>`), locates that content in the working tree, and warns when no
    citation on the page *touches* where it landed. Touching, not covering: requiring the
    whole anchored span to sit inside the page's merged citations assumed a correct
    rewrite still cites at least as much as the previous one did, and a good rewrite
    routinely cites **less** — tighter ranges are the improvement. That made the check
    noisiest exactly when it is least useful, immediately after a regeneration, which is
    the only time it runs in the normal flow. Its first live outing on a real repo
    produced 13 warnings, every one a narrowed or split citation and none a dropped
    claim. "Cited nowhere" is the honest question and the one the warning text already
    claims to ask. Relocation matches the **pair** of
    first and last non-blank lines, preferring the recorded span's length; single-line
    matching left a fifth of this repo's spans unresolvable, since boundary lines repeat
    (`}`, `)`, a bare `return`). Uniqueness is required — ambiguity yields no answer,
    because a wrong relocation would produce exactly the confidently-wrong numbers this
    check exists to catch. Reported once per file rather than per span: one edit moves
    every span in a file, and twenty near-identical warnings is how a class gets
    filtered out unread. Every uncovered region is still named on that one line. Summarising
    the tail as `(and 3 more)` gave a reader a number and withheld the only part they could
    act on, and a field run had to reconstruct the hidden regions by hand from the catalog's
    recorded `ranges`; the regions are a few characters each, so naming them costs nothing
    that the summary was buying. Silent when the anchor is unreachable, since every page is
    reported stale in that state anyway.
    Also silent when the page's **goal** has been rewritten since the anchor
    (`goal_hash` recorded and no longer matching). "Did this page stop citing code it
    was anchored to?" has a known answer once the brief changed: yes, deliberately,
    wherever the new goal asks for something the old one did not. Three consecutive
    field reports called the class pure noise on regenerated pages, and the fix that
    suggests itself — skip pages blessed in the current run — would delete the check
    outright, since a regeneration is the only time it runs. Note also that those
    reports diagnosed the noise as narrowing artifacts, which it cannot be: the overlap
    rule above already silences a narrowing, and on the run measured the repo's HEAD
    equalled its anchor, so relocation was exact and the flagged spans were ones the
    page had genuinely stopped citing. Deliberately **not** extended to the rest of
    `restated`: widening a scope adds a file, it does not authorise dropping the
    citations a page already had, so the check keeps its teeth there and for every
    ordinary `stale` regeneration under an unchanged goal. The window is one verify
    cycle — `anchor` stamps the new `goal_hash` for pages it wrote, so the next run
    re-arms. A goal edited but never regenerated is skipped too, which costs nothing:
    `restated` is already reporting that page, and louder.
    Rejected on measurement, recorded so they are not re-proposed: warning when a range
    starts or ends on a **bare closing bracket** fires on 65% of citations in a
    TypeScript repo, `}` being the normal end of a function; checking a section's
    identifiers against its **cited spans** instead of its cited files fires on 5.7%,
    and spot-checks were legitimate cross-references — the exact case the identifier
    check above already excludes on purpose.
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

**The maintenance loop.** `bin/akashic_loop.py` is that poll, made routine: it walks a
repo list, runs `stale --check` per repo, and hands a repo to the LLM only when that
check reports outstanding work. A quiet fleet therefore costs nothing to poll, which is
what makes running it on a schedule reasonable rather than extravagant. Three properties
are deliberate. Only the deterministic half of that loop may merge itself: a remap PR is line numbers
derived by arithmetic from a diff, re-checkable in seconds, and it auto-merges behind the
branch's required status checks. A PR carrying regenerated pages never does — `verify`
proves citations resolve, it says nothing about whether the sentences above them are
true, and that gap is precisely what a reader is for. Requesting auto-merge rather than
merging is deliberate: the required checks are the real gate, so a red run holds the PR
open instead of landing it.

**The loop's routing is derived, never restated.** `BUCKET_ACTIONS` maps every work
bucket to one of `update`, `remap` or `review`, and `WORK_BUCKETS` is derived from its
keys, so a bucket no action covers cannot exist. `classify` reads that table; a repo's
verdict is the most urgent action any non-empty bucket asks for, in the order
update → remap → review. The loop previously kept its own list of buckets that mean work,
and it drifted the same day `restated` and `unblessed` were added: a repo whose only
finding was a corrected brief or an unaccepted page classified as clean, and `summarize`
— a second hardcoded enumeration — printed `clean -> clean` while the work sat there.
An action a consumer does not recognize must fail loudly rather than default to `update`,
because a future bucket carrying edited-like semantics handed to a model would destroy
prose. And because `stale --check` and `classify` are two answers to one question, the
loop treats a CLEAN verdict on a repo the gate flagged as **its own defect**: it notifies
and exits 2. That tripwire is what catches a divergence no table knows about.

**The checkout is the operator's, not the loop's, and every cycle checks that before it
believes anything.** `workspace_problems` refuses a repo whose working tree is dirty, or
whose checkout sits on a branch a previous cycle created, or — where `origin/HEAD` makes
it knowable — on anything other than the default branch. The refusal runs *before* the
verdict is trusted, which is the whole point: a report read from a stranded checkout is
not a wrong answer to handle downstream, it is an answer about a tree the fleet is not
tracking, and `clean` is what it says most often. The three symptoms had one cause. A
cycle ran `git switch -c` and never switched back, so after one PR the checkout sat on
`chore/wiki-refresh` permanently and later polls read a branch upstream merges never
advance; a dirty tree was equally unchecked, so `git add .akashic` would sweep a human's
uncommitted wiki edits into the bot's PR; and `dirty` had been in every `stale` report
from the start with nobody reading it, the same dead-field shape as the loop's
`needs_work`.

Refusing rather than restoring or isolating follows the loop's doctrine — fail loud over
guess — and it invents no state. But a gate alone would have been worse than the bug:
a loop that strands its own checkout on every success and then refuses stranded
checkouts stops working after its first PR. So each cycle also returns the checkout to
the branch it started on, in a `finally`, and that restore never raises: it runs while a
real failure may already be propagating, and a switch that fails leaves the checkout
where the next cycle's gate will name it. Undeterminable is never treated as a problem —
an absent `origin/HEAD` means "do not check", never "assume `main`", and a git query that
cannot run at all abstains rather than raising, because an exception escaping this gate
would abandon every repo listed after it.

It opens a **pull request** instead of committing, because unattended
output should be read before it lands. It never regenerates a repo whose only finding is
`edited` — a human wrote that page and hard rule 2 says leave it alone, so the loop
reports it to the owner instead. And it **never exits 0 on failure**: an unverified
wiki, a failed update flow, an unreadable repo and a missing repo list all exit non-zero
and fire the notification hook, because a set-and-forget loop that fails silently
fossilizes the wiki, which is worse than having no loop at all.

`--dry-run` reports what each repo needs and spends nothing. It fires the notification
hook too, for any repo that is not clean. Under a scheduler stdout is a log file nobody
opens, and report-only is the mode an install is meant to start in, so a dry run that
only printed would make a fleet needing work look exactly like a quiet one. A clean dry
run stays silent: a daily banner that always arrives is a banner that stops being read.

**`unblessed` — a page was written and never accepted.** `planned` reports a page nobody
attempted, and it tests for the *absence* of the page file. Every other bucket iterates
`done` pages. Between the two sat a hole: a non-done page whose file exists belonged to no
bucket at all, so `stale --check` exited 0 on it and a runner saw a clean wiki. That is the
likelier half of the very failure `planned` was added to close — a generation subagent's
`Write` lands, then the session dies before `bless` — and one field run lost 9 of 14
regenerations exactly that way.

Kept separate from `planned` rather than merged, because the remedies differ and one of
them destroys work. `planned` means generate the page. `unblessed` means read what is
already there and run `bless <id> --done`, or regenerate deliberately. Folding the second
into the first would prescribe regeneration for a page that already has a body, discarding
it unread — and the whole point of hash-based edit detection is that the tool never
overwrites prose it did not write.

**`restated` — the brief changed, not the sources.** Orthogonal to every bucket below:
a page can be both stale (its code moved) and restated (what we asked of it moved), and
a reader deciding what to regenerate wants both. Two triggers, and only one needs
anything recorded:

- **A scope widened onto a file that already existed.** `compute_stale` only ever sees a
  scope-matched file through `added_now`, files added *since* the anchor. A file
  predating the anchor that newly falls into scope matched nothing at all, so the page
  silently claimed a file it had never read — verified in a fixture: `stale`, `uncovered`
  and `drifted` all empty. Detecting it needs no new field, because `anchor` already
  records `files`: anything now in scope and absent from it is exactly that file.
  Widening a scope is also the plan critic's commonest prescribed fix, which makes this
  the half that mattered most.
- **The goal was edited**, detected against the recorded `goal_hash`.

**Rejected: reading the old goal from git.** The tempting move is §5's own anchor trick —
the anchor commit is recorded, so read the prior catalog from it and store nothing. It
cannot work, for a structural reason worth recording so it is not re-proposed: `anchor`
stamps `anchor = HEAD` and *then* writes the catalog, so the catalog committed at the
anchor commit can never contain that anchor; it is always the previous generation's
state. In the normal flow the goal is edited in the working tree before regenerating, so
`git show <anchor>:.akashic/catalog.json` returns the *old* goal and every page would
report as restated immediately after anchoring — breaking the tested post-anchor
invariant.

`--out PATH` applies to `prompt` too, for the same cost reason as the two judge prompts.
This is the most-dispatched command — once per regenerated page, twelve times in one
real run, where reading and re-pasting cost roughly 40k tokens. Unlike the audit there
is no blindness to protect: a generation subagent needs repo access anyway. That is
precisely why the flag belongs in the tool rather than in an orchestrator's judgment,
since otherwise "handing over a path is safe here" has to be re-derived at every call
site, and a rule that depends on being re-derived is a rule that eventually is not.

**Regeneration context is rendered, never hand-appended.** `prompt <id> --update` adds
what changed — the modified dependencies for a `stale` page, and for a `restated` one
whether the goal was rewritten or files newly fell into scope. The update flow always
required that sentence and `prompt` never emitted it, so an orchestrator had to join
`stale`'s JSON to the rendered prompt itself; on the first external run that meant a
throwaway script across ten pages, which is exactly the hand-assembly §4 Phase 2 warns
causes bugs. A rule that depends on being retyped is a rule that eventually is not.

**`--update` never degrades silently to a generate prompt.** When the page sits in no
bucket the context is empty, and the flag used to render byte-identically to plain
`prompt <id>` with nothing marking the difference. A subagent then told to *write* a page
that already exists reads it, finds its citations correct, and changes one line — the
commit stamp. That restamp breaks the recorded hash, so the page lands in `edited`: a
wasted regeneration plus a false "a human touched this" marker on a page no human touched.
An empty context now renders an explicit statement instead, saying the dependencies have
not moved, that no goal change was detected, that a catalog with no recorded goal baseline
looks the same, and that re-verifying and restamping is not the job. Refusing outright
would be worse: an absent baseline is exactly the state a long-lived pre-`goal_hash`
catalog is in, and that is when a goal-driven rewrite is most needed.

`--ids <bucket>` prints one page id per line and nothing else. Acting on a report means
iterating ids, and without it every run hand-wrote a JSON-to-shell adapter — three so
far, one of which hit zsh's refusal to word-split an unquoted expansion and passed twelve
ids as a single string. An unknown bucket name exits 2 rather than printing nothing,
because a typo that yields an empty loop is indistinguishable from "nothing to do".

`akashic.py stale` (read-only, prints JSON):

1. `git cat-file -e <anchor>` — if the anchor is unreachable (force-push, shallow
   clone), say so and report **all pages stale** rather than guessing. The system may
   waste a regeneration; it must never mark stale content fresh. `anchor_state` splits
   the two causes, because they need different fixes: `never_anchored` is a first run,
   `anchor_unreachable` is a vanished commit.

   **Blob fallback.** Reporting *all* pages stale is the honest answer only when nothing
   is known, and the recorded `blobs` change that: a page is provably fresh when every
   recorded dependency still hashes to the same blob at HEAD **and** its scope expanded
   against HEAD adds nothing beyond the files already recorded. The second half is not
   redundant — blob equality can only speak about paths already recorded, so a new file
   inside the page's scope is invisible to it. Byte-equality is proof rather than a
   guess, so this narrows the blast radius without weakening "never mark stale content
   fresh"; a page with no recorded blobs is never provably fresh. It matters because the
   anchor an automated run stamps lives on a branch commit, and squash-merging that
   run's own PR discards it — without the fallback the loop regenerates everything on
   every cycle, permanently. Coverage in this mode is computed over the whole HEAD tree
   rather than a diff, since there is no "added since" to work from.
2. `git diff --name-status -M <anchor> HEAD`. Renames count as both old and new path.
3. Intersect changed/deleted paths against each page's `files` (plus `scope` globs for
   added files) →

   **Range-level staleness.** For a *modified* file the page cites with line numbers, the
   question is sharper than "did this file change": `git diff -U0` hunks are already in
   anchor coordinates, so overlap against the recorded `ranges` is integer math with no
   fuzzy matching. A page cited at lines 10-40 no longer regenerates because line 900
   changed. A zero-length hunk is an insertion after old line N and counts as touching
   `[s, e]` only when `s <= N < e`, so an insertion immediately after the last cited line
   leaves the page describing exactly what it described before.

   Everything else stays file-level, deliberately: a file in scope but never cited has no
   recorded range, a citation without line numbers claims the whole file, a pure rename
   emits no hunks at all under `-M` (and its citations now dangle), a deletion's hunk
   covers everything, and the blob fallback has no diff to intersect. The refinement only
   ever *removes* a false positive; it never invents freshness.

   **Drift is the cost of that precision, and `remap` is the payment.** Narrowing
   staleness to "did the change touch my lines" means a change *above* them no longer
   marks the page stale — while still moving the code the page points at. The recorded
   numbers stay in bounds, so `verify` cannot catch it. `stale` therefore reports those
   pages as `drifted`, and `remap` fixes them as arithmetic: shift each fragment by the
   cumulative line delta above it, rewrite the `path:start-end` text to agree, rename
   paths whose file moved, and bless. Fenced blocks are never rewritten — an example
   citation is documentation, not a dependency — and a page in `edited` is never
   rewritten either, because hard rule 2 covers line numbers too. Each page is blessed
   in the same step as its rewrite, since a rewritten body under its old hash would read
   as a human edit.

   **One diff snapshot per remap run, and it is a correctness rule rather than a
   performance one.** `remap` used to call `compute_stale`, which takes the two
   anchor→HEAD diffs, and then re-run the byte-identical pair to redo arithmetic the
   report had discarded. Two snapshots of a moving repository are two *instants*, not a
   duplicated computation: a commit landing between them is visible to the second and
   not the first, so a page the newer diff has made stale can still be sitting in the
   older report's `drifted` list, get its line numbers shifted, and get blessed —
   `remap` re-checked `page_drift` but never `touches_page`. `anchor` then stamps HEAD
   unconditionally and the page is recorded fresh while describing code that moved.
   Stale content marked fresh is the one outcome this design calls unacceptable, and it
   needs no arithmetic bug to occur, only two answers to "what changed" taken at
   different times. So the parsed diff — hunks, renames, and the per-page span mapping —
   is built once by `diff_context` and consumed by both the classification and the
   shift. The window is milliseconds and needs an external committer to exploit, which
   makes it a latent defect rather than an observed one; the class is the same as a
   fleet loop reporting clean while work exists.

   That context travels **beside** the report and never inside it. `cmd_stale`
   serializes the report dict as the public JSON contract, so `compute_stale` returns
   only the report and `compute_stale_with_context` returns both; `remap` is the sole
   caller of the second, and it needs the context precisely so it does not take a look
   of its own. This does not close the wider window between a remap run and the `anchor`
   that follows it, which is the loop's to hold, not this function's.

| Bucket | Meaning | Action (agent-side) |
|---|---|---|
| `stale` | a file the page depends on changed | regenerate (same Phase-2 contract); regenerated citations refresh `files` at anchor time, so renames and dependency drift self-heal |
| `edited` | current page hash ≠ recorded `hash` → a human touched it | **never auto-overwrite**; skip and warn by default; regenerate only on explicit request, passing the human text as immutable context to preserve |
| `orphaned` | every file the page documented is gone | auto-delete page + catalog entry (git is the backstop) — **unless also `edited`: then flag only; human text is never destroyed by automation** |
| `planned` | a catalog page with no generated file on disk | regenerate it, or say the run is deliberately partial. Reported rather than an error because a partly generated wiki is a valid resume state — but `verify` and `anchor` both skip non-done pages, so without this bucket a generate run whose subagents died reports `verify: ok`, stamps an anchor and shows a clean `stale` |
| `drifted` | nothing the page cites changed, but something above it did, so the recorded line numbers now point elsewhere | run `remap`: shift each fragment by the cumulative delta above it, rewrite the human-readable text to match, bless. Zero LLM. This bucket exists because range-level staleness would otherwise leave the citation silently wrong — in bounds, so `verify` cannot see it |
| `uncovered` | added files matching no page's scope∪files | if a coherent new module appeared, propose new catalog entries (visible as a catalog diff in review); otherwise note and ignore |
| `missing` | a `done` page whose file is gone from disk | regenerate from the catalog entry. Distinct from `orphaned`, where the *sources* went and the page should follow; here the catalog still claims a page that no longer exists, which is a deletion nobody recorded |
| `restated` | the page's brief changed rather than its sources: its `goal` was rewritten, or its `scope` widened onto a file that already existed | regenerate with `prompt <id> --update`, which names which of the two it was. Orthogonal to every other row — a page can be both `stale` and `restated`, and a reader deciding what to regenerate wants both |
| `unblessed` | a non-`done` page whose file *is* on disk: written, never accepted | read it, then `bless <id> --done` if it is complete. Kept apart from `planned` because the remedies differ and one of them destroys work: `planned` means generate the page, and prescribing that for a page that already has a body discards it unread |

4. The update run ends: regenerate stale pages → `verify` → `anchor` → print a
   structured report (per page: reason, files, action) that becomes the commit message.

**Invariant (tested): immediately after `anchor`, `stale` returns empty.**

**No in-page "Update Summary" sections.** Qoder appends changelog bullets into each
updated page; they accumulate forever, pollute agent context, and drift. Pages here are
timeless; temporal data belongs to git (`git log -- .akashic/wiki/<id>.md` is the
changelog, the update report is the commit message).

```
ponytail: (a) shipped — staleness is now range-level where a page cites line numbers,
see "Range-level staleness" above. Remaining: (b) per-H2-section hashes so regeneration
can preserve human-edited sections byte-for-byte instead of skipping the whole page.
Note the earlier claim that the on-disk format already records everything both upgrades
need was wrong in both directions: (a) needed a `ranges` field, and (b) needs per-section
hashes. Neither existed.
```

## 5b. Claim audit (on-demand, never a gate)

Owner decision (2026-07-29): the semantic claim audit is **on-demand only**. It is not
part of the standing update flow, never gates `anchor`, and never runs on a schedule.
Reach for it when a page smells wrong.

Everything else in this design checks the *scaffolding* of a claim. `verify` proves a
citation resolves; the anchored-content warning proves it still points where it was
anchored; the identifier warning proves a named symbol exists somewhere the section cites.
None of them can say whether the sentence above the citation is true. The audit is the one
thing that asks.

**`audit extract [--page <id>]`** emits deterministic JSON: per H2 section, the claim
prose with its heading and its `Sources:` paragraphs removed, plus the exact bytes of
every cited span (`citation_span` clamping the +1 trailing-newline phantom). It extracts
and never judges. Sections are cut with the same `h2_sections` splitter the identifier
check uses — one definition, not three.

**`audit prompt <id>`** renders the blind refuter prompt in the `render_prompt` idiom. The
judge gets the claims and the evidence and nothing else: no repository access, and
**evidence is labelled `[E1]`, `[E2]` rather than by filename**. That is the load-bearing
choice. Every planning defect that motivated this work came from reasoning off a name — a
page promised "site types and parent/child relationships" because a migration was called
`add-siteType-enums.js`, where no parent column existed anywhere and the enum was used by
no column, on a different table. A judge shown `services/auth/index.ts` fills gaps with
what an auth service usually does; shown `[E1]` it can only read what is in front of it.
The label→path mapping travels in the extract JSON, so a finding stays actionable: the
orchestrator translates, not the judge.

The prompt instructs the judge to **default to refuting** and to separate **contradicted**
(the evidence shows otherwise), **unsupported** (the evidence is silent), and
**overstated** (broader than what is shown). A section citing nothing is rendered with
`EVIDENCE: none` rather than dropped, because prose resting on nothing at all is the
strongest available finding.

**The denominator comes from the extract, never from the judge.** The prompt closes by
enumerating every section title and requiring one `sound`/`not sound` verdict each, in
order, with the total written against a fixed count. Asking instead for "the count of
sections you found sound" let the judge pick what it was counting: one field run reported
`1 of 8` for a page `extract` says has nine sections, having folded one section's evidence
into its neighbours' reasoning without saying so. Two passes over the same page were then
not comparable, which is fatal for the single number this whole exercise tracks — and it
had been that way for every score on record. Fixing it is pure templating, so it belongs on
the deterministic side of §2. The sampling caveat stays and is now sharper: a fixed
denominator makes two samples comparable, it does not promote either one to a measurement.

**The blindness is a contract, not a sandbox — say so plainly.** The judge is a subagent
with tools. Nothing prevents it opening the repository; it is *told* it has no access and
told not to ask for more evidence, exactly as the page prompts are told to be read-only.
Pretending otherwise costs real tokens for no added guarantee, because it makes handing
over a path look unsafe when it is not. So `audit prompt <id> --out PATH` writes the
prompt to a file and prints a dispatch line instead. Evidence spans are verbatim source,
so these prompts are large — one page in this repo renders 224KB — and printing it costs
that twice: once into the orchestrator's context, once into the judge's, with the
orchestrator gaining nothing from having read it. Telling a subagent to read one named
file is the same kind of instruction as telling it not to browse, at half the price. It
refuses to write inside `.akashic/`, where a scratch file of that size would surface as
`uncovered` and be committed with the wiki.

**An excerpt cannot prove absence, and the judge is told how much it is missing.** Each
label now states its file's total length and how many lines are hidden — `[E4] lines 47-57
of a 210-line file`. Without that, any claim a page makes about a file *lacking* something
(no name guard, no environment check, strictly read-only, no companion script) is
unfalsifiable from an excerpt and lands as **unsupported** regardless of truth. It hit a
page about operational scripts hardest, scoring it **1 of 13 sections sound**, because
such a page is largely *about* safety properties and safety properties are absence claims.
A line count is not a filename, so this leaks nothing the labelling exists to withhold.
The judge is also told to name what *would* settle an absence claim rather than counting
it against the page, and to grade a stated verification method — "confirmed read-only by
grepping for every write verb" — on whether the method would establish the claim rather
than on whether the excerpt does.

**The judge must not grade attribution, and the prompt says so.** Withholding filenames
has a cost that only showed up in the field: a page saying a fact comes from the README is
making a claim the judge is structurally unable to check, and it reported every one of them
as unsupported. On one real page that turned a roughly 3-of-9 result into 1-of-9, which is
distortion enough to make the output hard to read. The fix is one instruction, not a design
change — **handing the judge the `Sources:` paths was considered and rejected**, because a
filename is precisely the input the labelling exists to withhold, and the citation gate
already proves every cited path resolves to a real tracked file. Its section counts carry
the same sampling caveat as the plan critic's.

The division of labor (§2) holds exactly: the script renders prompts and extracts bytes,
and never calls an LLM; the LLM judges, and never computes a range.

On its first run against this repo's own `maintenance-loop` page it found one: the claim
"a test pins the direction" in a page scoped to `bin/*`, which cannot cite `test_akashic.py`
at all. True, and resting on nothing the page offers.

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
| Watchers, webhooks, daemons | no shipped system triggers per-push; anchor + polling is the convergent answer. A **scheduled poll** (`bin/akashic_loop.py`, §5) is not the excluded thing: no long-lived process, no subscription, no state of its own, and every cycle gated by a zero-token `stale --check` |
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

One class is deliberately not of that shape: `TestDocBucketEnumerations` reads this
repository's own prose and checks every hand-maintained list of the stale buckets against
`WORK_BUCKETS`. It exists because four of them rotted simultaneously — README listed eight
of nine, `CLAUDE.md` six, this document's own bucket table six with `missing` defined
nowhere, and `SKILL.md`'s update flow had no `planned` bullet despite the loop routing
planned-only repos straight into it. The only enumeration that stayed current was the
generated wiki page. Prose cannot be *derived* from the table the way `WORK_BUCKETS` is,
so failing loudly on disagreement is the nearest available equivalent, and without it a
tenth bucket silently rots four files again.

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
