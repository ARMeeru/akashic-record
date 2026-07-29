# Contributing

Thanks for your interest. This project has some unusual, deliberate constraints — please read this before opening a PR.

## Ground rules

- **Stdlib only.** `akashic.py` must run on a plain Python 3.9+ interpreter. No third-party imports, ever.
- **DESIGN.md is normative.** Any change to behavior or the on-disk format must update DESIGN.md in the same PR. §8 lists things deliberately not built (embeddings/RAG, DB storage, MCP server, watchers, Mermaid validation, …) — reintroducing one needs a design discussion in an issue first, not a PR.
- **Open an issue before anything bigger than a small fix.** The roadmap ([milestones](https://github.com/ARMeeru/akashic-record/milestones)) is maintainer-driven.

## Invariants you must not break

- Page ids are frozen forever and slug-validated at catalog load — an id is a path component, so a non-slug id is a traversal vector.
- `hash` permanently means "what the tool last wrote"; `null` is the bless signal. Never re-hash a changed body under a non-null hash — that launders the human-edit marker.
- Citations resolve against git's tracked-path set, not the filesystem, and resolved paths must realpath inside the repo root.
- `.akashic/` paths are never staleness inputs or recorded dependencies — the wiki must not depend on itself.

## Workflow

- The default branch is `develop`. Branch from it; PRs target it.
- Commit messages and PR titles follow [Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/).

## Tests

```sh
python3 test_akashic.py                                  # full suite
python3 test_akashic.py TestStale                        # one class
python3 test_akashic.py TestStale.test_rename_marks_stale_not_orphaned   # one test
```

New deterministic behavior needs at least one smallest-possible `unittest` check (DESIGN.md §9 style; fixtures are throwaway git repos — see `RepoCase`). The LLM phases are covered by the runtime `verify` gate, not unit tests.

## Dogfooding

This repo carries its own generated wiki in `.akashic/`. If your change touches source files, refresh it (`stale` → regenerate → `verify` → `anchor`) and commit the refresh as `chore: refresh self-dogfooded wiki`.

## Conduct

Be respectful in issues and reviews. That's the whole policy for now.
