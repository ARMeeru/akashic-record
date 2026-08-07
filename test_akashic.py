#!/usr/bin/env python3
"""Self-checks for the akashic-record deterministic core.

One check per component (DESIGN.md section 9): scan filtering, the diff->stale
mapping (the test that proves the incremental mechanism), citation verification,
and the anchor invariant. Fixtures are throwaway git repos; stdlib unittest only.
"""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
import akashic  # noqa: E402
import akashic_loop  # noqa: E402


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    def make_repo(self):
        tmp = tempfile.mkdtemp(prefix="akashic-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sh(tmp, "git", "init", "-q", "-b", "main")
        sh(tmp, "git", "config", "user.email", "test@test")
        sh(tmp, "git", "config", "user.name", "test")
        return Path(tmp)

    def write(self, repo, rel, content):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit(self, repo, message="c"):
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", message)

    def catalog(self, repo, pages, anchor=None, exclude=None):
        data = {"version": 1, "anchor": anchor, "generated": None,
                "language": "en", "exclude": exclude or [],
                "max_files": 5000, "pages": pages}
        self.write(repo, ".akashic/catalog.json", json.dumps(data, indent=2))

    @staticmethod
    def page(pid, files=(), scope=(), status="done", parent=None, **extra):
        page = {"id": pid, "title": pid.title(), "parent": parent,
                "goal": f"document {pid}", "scope": list(scope),
                "files": list(files), "status": status}
        page.update(extra)
        return page

    def head(self, repo):
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()


class TestScan(RepoCase):
    def test_scan_filters_binaries_lockfiles_and_excludes(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "print(1)\nprint(2)\nprint(3)\n")
        self.write(repo, "img.bin", b"\x00\x01\x02binary")
        self.write(repo, "data/big.snap", "snapshot\n")
        self.write(repo, "package-lock.json", "{}\n")
        self.commit(repo)
        self.catalog(repo, [], exclude=["*.snap"])

        root = akashic.repo_root(repo)
        entries = akashic.scan_repo(root, akashic.load_catalog(root, required=False))
        paths = dict(entries)
        self.assertEqual(paths, {"src/app.py": 3},
                         "scan must keep the source file and drop binary, "
                         "lockfile, excluded glob, and .akashic itself")


class TestStale(RepoCase):
    def test_stale_orphaned_uncovered_mapping(self):
        """The canonical incremental-mechanism test: edit f1, delete f2, add f3
        -> exactly {stale:[a], orphaned:[b], uncovered:[f3]}."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.write(repo, "f2.py", "two\n")
        self.commit(repo)
        anchor = self.head(repo)

        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")
        self.write(repo, ".akashic/wiki/b.md", "# B\n\nSources: [f2](../../f2.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"]),
                            self.page("b", files=["f2.py"], scope=["f2.py"])],
                     anchor=anchor)

        self.write(repo, "f1.py", "one changed\n")
        (repo / "f2.py").unlink()
        self.write(repo, "f3.py", "new\n")
        self.commit(repo, "edit f1, delete f2, add f3")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertTrue(report["anchor_reachable"])
        self.assertEqual([s["id"] for s in report["stale"]], ["a"])
        self.assertEqual(report["stale"][0]["changed"], ["f1.py"])
        self.assertEqual(report["orphaned"], ["b"])
        self.assertEqual(report["uncovered"], ["f3.py"])
        self.assertEqual(report["edited"], [])
        self.assertEqual(report["missing"], [])

    def test_rename_marks_stale_not_orphaned(self):
        repo = self.make_repo()
        self.write(repo, "f1.py", "line\n" * 30)
        self.commit(repo)
        anchor = self.head(repo)
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor=anchor)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")

        sh(repo, "git", "mv", "f1.py", "g1.py")
        self.commit(repo, "rename")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual([s["id"] for s in report["stale"]], ["a"])
        self.assertEqual(report["orphaned"], [],
                         "a rename is stale (citations dangle), never orphaned")

    def test_unreachable_anchor_reports_everything_stale(self):
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.catalog(repo, [self.page("a", files=["f1.py"])],
                     anchor="0" * 40)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertFalse(report["anchor_reachable"])
        self.assertEqual(report["anchor_state"], "anchor_unreachable")
        self.assertEqual([s["id"] for s in report["stale"]], ["a"],
                         "never guess: unreachable anchor means regenerate all")

    def test_never_anchored_is_distinguished_from_unreachable(self):
        """The two need different fixes: a first run just needs `anchor`, a
        vanished commit means a shallow clone or a squashed-away branch."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.catalog(repo, [self.page("a", files=["f1.py"])], anchor=None)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertFalse(report["anchor_reachable"])
        self.assertEqual(report["anchor_state"], "never_anchored")

    def test_planned_page_never_written_is_reported(self):
        """`missing` only inspects done pages, so a page a dead subagent never
        wrote is invisible to it -- and verify/anchor skip non-done pages too.
        Without this bucket, a generate run that mostly failed still reports
        verify ok, a stamped anchor and a clean stale."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        anchor = self.head(repo)
        self.write(repo, ".akashic/wiki/done.md",
                   "# Done\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("done", files=["f1.py"], scope=["f1.py"]),
                            self.page("ghost", scope=["f1.py"],
                                      status="planned")],
                     anchor=anchor)

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual(report["planned"], ["ghost"])
        self.assertEqual(report["missing"], [],
                         "missing is about done pages; planned is its own state")

    def test_check_exit_code_is_the_runners_zero_token_gate(self):
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        anchor = self.head(repo)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor=anchor)
        root = akashic.repo_root(repo)

        self.assertEqual(self.run_stale_check(root), 0,
                         "nothing outstanding must cost the runner nothing")

        self.write(repo, "f1.py", "one changed\n")
        self.commit(repo, "touch a dependency")
        self.assertEqual(self.run_stale_check(root), 1,
                         "a stale page must wake the runner")

    def anchored_repo_with_lost_anchor(self):
        """Anchor properly, then point the catalog at a commit this clone does
        not have -- the shape a squash-merged runner PR or a shallow clone
        produces every cycle."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.write(repo, "f2.py", "two\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md",
                   "# A\n\nSources: [f1](../../f1.py), [f2](../../f2.py)\n")
        self.catalog(repo, [self.page("a", scope=["*.py"])],
                     anchor=self.head(repo))
        self.commit(repo, "wiki")
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        catalog = akashic.load_catalog(root)
        self.assertTrue(catalog["pages"][0]["blobs"],
                        "anchor must record dependency blob shas")
        catalog["anchor"] = "0" * 40
        akashic.save_catalog(root, catalog)
        return repo, root

    def test_unreachable_anchor_proves_freshness_from_blobs(self):
        """Blob shas are content hashes, so equality at HEAD is proof the file
        is byte-identical even though the anchor commit is gone. Without this
        a squash-merging runner regenerates every page, every cycle, forever."""
        repo, root = self.anchored_repo_with_lost_anchor()
        report = akashic.compute_stale(root)
        self.assertFalse(report["anchor_reachable"])
        self.assertEqual(report["anchor_state"], "anchor_unreachable")
        self.assertEqual(report["stale"], [],
                         "unchanged content is provably fresh without the anchor")

        self.write(repo, "f1.py", "one changed\n")
        self.commit(repo, "touch a dependency")
        self.assertEqual([s["id"] for s in akashic.compute_stale(root)["stale"]],
                         ["a"], "a changed blob must still mark the page stale")

    def test_unreachable_anchor_still_catches_new_in_scope_files(self):
        """Blob equality can only speak about paths already recorded, so a new
        file matching the page's scope is invisible to it. The scope-subset
        half of the check is what keeps a new module from being declared
        documented."""
        repo, root = self.anchored_repo_with_lost_anchor()
        self.write(repo, "f3.py", "brand new module\n")
        self.commit(repo, "add a file inside the page's scope")

        report = akashic.compute_stale(root)
        self.assertEqual([s["id"] for s in report["stale"]], ["a"],
                         "a new in-scope file must not be declared fresh")

    def test_fallback_never_trusts_a_page_without_recorded_blobs(self):
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor="0" * 40)
        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual([s["id"] for s in report["stale"]], ["a"],
                         "no recorded blobs means freshness cannot be proven")

    def run_stale_check(self, root):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return akashic.cmd_stale(root, check=True)


class TestVerify(RepoCase):
    def valid_repo(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nIntro prose.\n\n"
                   "Sources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        return repo

    def test_valid_page_passes(self):
        errors = akashic.verify_repo(akashic.repo_root(self.valid_repo()))
        self.assertEqual(errors, [])

    def test_trailing_newline_off_by_one_tolerated(self):
        """Claude's Read tool shows one extra (empty) numbered line for any
        file ending in a trailing newline -- every subagent naturally cites
        up to that number. Tolerate exactly +1; anything more is still an
        error."""
        repo = self.valid_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")  # 3 real lines, trailing \n
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../src/app.py#L1-L4)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [], f"+1 must be tolerated, got: {errors}")

        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../src/app.py#L1-L5)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("exceeds file length" in e for e in errors),
                        "+2 must still be rejected as a real error")

    def test_out_of_range_citation_fails_naming_page_and_line(self):
        repo = self.valid_repo()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [src/app.py](../../src/app.py#L1-L99)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(len(errors), 1)
        self.assertIn("index", errors[0])
        self.assertIn("line 3", errors[0])
        self.assertIn("exceeds file length", errors[0])

    def test_traversal_citation_fails(self):
        repo = self.valid_repo()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../../etc/hosts)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("outside repository" in e for e in errors))

    def test_missing_cited_file_fails(self):
        repo = self.valid_repo()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../src/nope.py)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("not tracked by git" in e for e in errors),
                        f"nonexistent cited file must fail verify, got: {errors}")

    def test_scheme_url_in_sources_fails(self):
        repo = self.valid_repo()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](file:///src/app.py)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("schemes are not allowed" in e for e in errors))


class TestAnchor(RepoCase):
    def test_anchor_invariant_then_edit_detection(self):
        """Invariant: after anchor (and committing its artifacts), stale is
        empty; a manual edit afterwards flips exactly the edited bucket.

        Scope deliberately includes "README.md": the wiki's own derived TOC
        must never basename-match a page scope and become a self-dependency."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.write(repo, "README.md", "hello\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["README.md", "src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        self.commit(repo, "wiki")

        root = akashic.repo_root(repo)
        anchor_commit = self.head(repo)
        result = akashic.anchor_repo(root)
        self.assertEqual(result["pages"], 1)

        catalog = akashic.load_catalog(root)
        self.assertEqual(catalog["anchor"], anchor_commit)
        self.assertIn("src/app.py", catalog["pages"][0]["files"])
        self.assertNotIn(".akashic/wiki/README.md", catalog["pages"][0]["files"],
                         "wiki artifacts must never be page dependencies")
        self.assertTrue((repo / ".akashic/wiki/README.md").is_file(),
                        "anchor renders the derived TOC")

        self.commit(repo, "anchor artifacts")  # the real-world flow
        report = akashic.compute_stale(root)
        for bucket in ("stale", "edited", "orphaned", "uncovered", "missing"):
            self.assertEqual(report[bucket], [],
                             f"post-anchor invariant violated: {bucket} not empty")

        with open(repo / ".akashic/wiki/index.md", "a", encoding="utf-8") as fh:
            fh.write("\nHuman-added correction.\n")
        report = akashic.compute_stale(root)
        self.assertEqual(report["edited"], ["index"])
        self.assertEqual(report["stale"], [])

    def test_anchor_refuses_on_verification_failure(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../src/nope.py)\n")
        with self.assertRaises(SystemExit) as ctx:
            akashic.anchor_repo(akashic.repo_root(repo))
        self.assertEqual(ctx.exception.code, 1)


class TestReviewRegressions(RepoCase):
    """One regression test per confirmed finding of the adversarial review."""

    def test_second_anchor_preserves_edit_protection(self):
        """CRITICAL: re-anchoring while a human edit exists must not launder
        the edited marker away; hash: null is the explicit bless signal."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        self.commit(repo, "wiki")
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        tool_hash = akashic.load_catalog(root)["pages"][0]["hash"]

        with open(repo / ".akashic/wiki/index.md", "a", encoding="utf-8") as fh:
            fh.write("\nHuman correction.\n")
        akashic.anchor_repo(root)  # a routine update run's terminal anchor
        catalog = akashic.load_catalog(root)
        self.assertEqual(catalog["pages"][0]["hash"], tool_hash,
                         "anchor must never bless human-edited text")
        self.assertEqual(akashic.compute_stale(root)["edited"], ["index"],
                         "edit protection must survive re-anchoring")

        akashic.bless_pages(root, ["index"])  # explicit bless (post-regeneration)
        akashic.anchor_repo(root)
        self.assertEqual(akashic.compute_stale(root)["edited"], [])

    def test_bless_refuses_unknown_id_and_unwritten_page(self):
        """bless exists so the orchestrator stops hand-editing catalog.json;
        it has to refuse the two ways that hand-edit went wrong -- a typo'd id,
        and blessing a page whose file was never actually written."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"], hash="sha256:old")])
        root = akashic.repo_root(repo)

        with self.assertRaises(SystemExit) as ctx:
            akashic.bless_pages(root, ["ghost"])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            akashic.bless_pages(root, ["index"])  # no wiki/index.md on disk
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(akashic.load_catalog(root)["pages"][0]["hash"],
                         "sha256:old",
                         "a refused bless must not have mutated the catalog")

    def test_bless_done_flips_planned_page(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"],
                                     status="planned", hash="sha256:old")])
        self.write(repo, ".akashic/wiki/index.md", "# Index\n")
        root = akashic.repo_root(repo)
        akashic.bless_pages(root, ["index"], mark_done=True)
        page = akashic.load_catalog(root)["pages"][0]
        self.assertIsNone(page["hash"])
        self.assertEqual(page["status"], "done")

    def test_untracked_citation_fails_verify(self):
        repo = self.valid_repo_for_verify()
        self.write(repo, "src/untracked.py", "x\n")  # exists on disk, not in git
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [u](../../src/untracked.py)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("not tracked by git" in e for e in errors),
                        f"untracked citation must fail verify, got: {errors}")

    def test_traversal_page_id_rejected_at_load(self):
        repo = self.make_repo()
        self.write(repo, "f.py", "x\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index")])
        catalog = json.loads((repo / ".akashic/catalog.json").read_text())
        catalog["pages"][0]["id"] = "../../../secret"
        self.write(repo, ".akashic/catalog.json", json.dumps(catalog))
        with self.assertRaises(SystemExit) as ctx:
            akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual(ctx.exception.code, 2)

    def test_string_scope_rejected_at_load(self):
        repo = self.make_repo()
        self.write(repo, "f.py", "x\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index")])
        catalog = json.loads((repo / ".akashic/catalog.json").read_text())
        catalog["pages"][0]["scope"] = "src/**"  # string, not list
        self.write(repo, ".akashic/catalog.json", json.dumps(catalog))
        with self.assertRaises(SystemExit) as ctx:
            akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(ctx.exception.code, 2)

    def test_nul_byte_citation_reports_error_not_crash(self):
        repo = self.valid_repo_for_verify()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [x](../../src/app%00.py#L1)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))  # must not raise
        self.assertTrue(any("control characters" in e for e in errors))

    def test_fenced_example_citation_ignored(self):
        repo = self.valid_repo_for_verify()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nReal section.\n\n"
                   "Sources: [src/app.py:1-3](../../src/app.py#L1-L3)\n\n"
                   "```markdown\nSources: [ghost](../../src/ghost.py#L1-L5)\n```\n")
        root = akashic.repo_root(repo)
        self.assertEqual(akashic.verify_repo(root), [],
                         "fenced example citations must not be verified")
        self.commit(repo, "wiki")
        akashic.anchor_repo(root)
        files = akashic.load_catalog(root)["pages"][0]["files"]
        self.assertNotIn("src/ghost.py", files,
                         "fenced example citations must not become dependencies")

    def test_bracketed_dynamic_route_citation_parses(self):
        """Next.js-style '[id]/route.ts' in the link TEXT contains a literal
        ']' -- the parser must not stop there and report zero links."""
        repo = self.make_repo()
        self.write(repo, "app/users/[id]/route.ts", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["app/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: "
                   "[app/users/[id]/route.ts:1-3](../../app/users/[id]/route.ts#L1-L3)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"a bracketed dynamic-route path must parse as a citation, got: {errors}")

    def test_angle_bracket_wrapped_destination_resolves(self):
        """CommonMark allows <...>-wrapped link destinations for targets that
        contain characters like literal '[' ']' -- exactly what citing a
        Next.js '[id]/route.ts' path forces the destination to need."""
        repo = self.make_repo()
        self.write(repo, "app/users/[id]/route.ts", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["app/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: "
                   "[app/users/[id]/route.ts:1-3](<../../app/users/[id]/route.ts#L1-L3>)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"an angle-bracket-wrapped destination must resolve, got: {errors}")

    def test_route_group_paren_path_citation_parses(self):
        """Framework route groups put literal parentheses in a path segment
        (Next.js "api/(cron)/route.ts"). A [^)\\s]+ destination truncates at
        the first ")", and because the truncated prefix still resolves to a
        path, the citation failed as "not tracked by git" -- a real file
        reported as a bogus one."""
        repo = self.make_repo()
        self.write(repo, "app/api/(cron)/route.ts", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["app/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: "
                   "[app/api/(cron)/route.ts:1-3](../../app/api/(cron)/route.ts#L1-L3)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"a route-group path with parentheses must parse, got: {errors}")

    def test_angle_wrapped_paren_path_resolves(self):
        """The <...> escape hatch must work for paren paths too: it was
        defeated by the same truncation, so the documented workaround for
        awkward destinations silently did not work."""
        repo = self.make_repo()
        self.write(repo, "app/api/(cron)/route.ts", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["app/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: "
                   "[app/api/(cron)/route.ts:1-3](<../../app/api/(cron)/route.ts#L1-L3>)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"an angle-wrapped paren path must resolve, got: {errors}")

    def test_empty_sources_block_fails(self):
        repo = self.valid_repo_for_verify()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: see src/app.py lines 1-3\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("no parseable citation links" in e for e in errors))

    def test_wrapped_sources_paragraph_is_verified(self):
        """Citations on a Sources paragraph's continuation lines count."""
        repo = self.valid_repo_for_verify()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [a](../../src/app.py#L1-L2),\n"
                   "[b](../../src/app.py#L1-L99)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("line 4" in e and "exceeds file length" in e
                            for e in errors),
                        f"continuation-line citation must be checked, got: {errors}")

    def test_matches_any_gitignore_affordances(self):
        self.assertTrue(akashic.matches_any("top.snap", ["**/*.snap"]),
                        "**/ prefix must also match at the repo root")
        self.assertTrue(akashic.matches_any("a/b/top.snap", ["**/*.snap"]))
        self.assertTrue(akashic.matches_any("a/b/x.snap", ["*.snap"]),
                        "slash-free patterns match basenames at any depth")
        self.assertTrue(akashic.matches_any("vendor/lib/x.js", ["vendor/**"]))
        self.assertFalse(akashic.matches_any("src/x.py", ["**/*.snap"]))

    def test_matches_any_treats_brackets_as_literal(self):
        """Framework route paths contain literal brackets (Next.js
        "[id]/route.ts"). fnmatch reads "[id]" as a one-character class, so
        the natural scope glob for such a path silently matched nothing."""
        self.assertTrue(akashic.matches_any(
            "src/app/api/users/[id]/route.ts",
            ["src/app/api/users/[id]/route.ts"]),
            "a bracket-literal path must match its own exact pattern")
        self.assertTrue(akashic.matches_any(
            "src/app/api/users/[id]/route.ts", ["src/app/api/users/[id]/*"]))
        self.assertTrue(akashic.matches_any(
            "src/app/api/users/[id]/image/[attachmentId]/route.ts",
            ["src/app/api/users/[id]/image/*"]),
            "nested bracketed segments must match too")
        self.assertFalse(akashic.matches_any(
            "src/app/api/users/i/route.ts",
            ["src/app/api/users/[id]/route.ts"]),
            "the character-class reading must be gone, not just widened")
        # both gitignore affordances still hold with brackets in play
        self.assertTrue(akashic.matches_any("a/[id]/route.ts",
                                           ["**/[id]/route.ts"]))
        self.assertTrue(akashic.matches_any("a/b/[id].md", ["[id].md"]))

    def test_bracket_scope_page_is_not_falsely_orphaned(self):
        """The clause that keeps a rewritten module from being reported
        `orphaned` runs through matches_any. With brackets broken it saw no
        surviving in-scope file and reported orphaned -- and SKILL.md resolves
        orphaned by deleting the page and its catalog entry, so a silent glob
        defect became a data-loss path."""
        repo = self.make_repo()
        self.write(repo, "src/app/api/users/[id]/old.ts", "gone\n")
        self.write(repo, "src/app/api/users/[id]/route.ts", "kept\n")
        self.commit(repo)
        anchor = self.head(repo)
        self.write(repo, ".akashic/wiki/a.md", "# A\n\nSources: "
                   "[route](../../src/app/api/users/[id]/route.ts)\n")
        self.catalog(repo, [self.page("a",
                                     files=["src/app/api/users/[id]/old.ts"],
                                     scope=["src/app/api/users/[id]/*"])],
                     anchor=anchor)
        (repo / "src/app/api/users/[id]/old.ts").unlink()
        self.commit(repo, "drop old.ts, keep route.ts")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual(report["orphaned"], [],
                         "a page whose scope still matches a tracked file is "
                         "stale, never orphaned")
        self.assertEqual([s["id"] for s in report["stale"]], ["a"])

    def test_uncovered_filters_noise_and_exact_files_do_not_shadow(self):
        repo = self.make_repo()
        self.write(repo, "Makefile", "all:\n")
        self.commit(repo)
        anchor = self.head(repo)
        self.catalog(repo, [self.page("index", files=["Makefile"])],
                     anchor=anchor, exclude=["*.snap"])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [m](../../Makefile)\n")
        self.write(repo, "sub/dir/Makefile", "all:\n")   # new, same basename
        self.write(repo, "package-lock.json", "{}\n")    # denylist noise
        self.write(repo, "t.snap", "s\n")                # exclude-glob noise
        self.commit(repo, "add files")

        report = akashic.compute_stale(akashic.repo_root(repo))
        self.assertEqual(report["uncovered"], ["sub/dir/Makefile"],
                         "noise must be filtered; an exact files entry must not "
                         "shadow same-named files at other depths")
        self.assertEqual(report["stale"], [])

    def valid_repo_for_verify(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        return repo

    def test_link_to_planned_sibling_is_not_broken(self):
        """A partial wiki is a valid state (DESIGN.md 5): a done page may
        cross-link a planned sibling without failing verify."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.commit(repo)
        self.catalog(repo, [
            self.page("index", scope=["src/**"]),
            self.page("later", scope=["src/**"], status="planned"),
        ])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSee [Later](./later.md).\n\n"
                   "Sources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"forward link to a planned sibling must not fail verify, got: {errors}")

    def test_prompt_expands_scope_and_flags_untracked_context_docs(self):
        """The mechanical fix for the CLAUDE.md/AGENTS.md bug: rendering must
        list only tracked, scope-matched files as citable, and separately
        surface untracked context docs with the non-citable warning -- no
        human has to remember to type this per subagent."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.write(repo, "src/other.py", "b\n")
        self.commit(repo)
        self.write(repo, "CLAUDE.md", "local-only guidance\n")  # never git-added
        self.catalog(repo, [
            self.page("index", scope=["src/app.py"],
                     goal="Orient a new developer."),
            self.page("later", scope=["src/**"], status="planned"),
        ])
        root = akashic.repo_root(repo)
        catalog = akashic.load_catalog(root)
        out = akashic.render_prompt(root, catalog, "index")

        self.assertIn("src/app.py", out)
        self.assertNotIn("src/other.py", out,
                         "must not list files outside this page's scope")
        self.assertIn("later ->", out, "sibling list must include other pages")
        self.assertIn("CLAUDE.md", out)
        self.assertIn("NOT tracked by git", out)
        self.assertIn("Orient a new developer.", out)

    def test_prompt_omits_context_warning_when_none_present(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/app.py"])])
        root = akashic.repo_root(repo)
        out = akashic.render_prompt(root, akashic.load_catalog(root), "index")
        self.assertNotIn("NOT tracked by git", out)

    def test_prompt_scope_expansion_applies_scan_filters(self):
        """`exclude` globs and built-in noise must not reach a page's citable
        file list. scan and expand_scope share one definition of the citable
        universe -- otherwise a broad `scope` glob quietly re-admits exactly
        what the catalog excluded, and verify accepts citations to it."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.write(repo, "src/bundle.min.js", "x\n")
        self.write(repo, "src/package-lock.json", "{}\n")
        self.write(repo, "src/fixture.snap", "snap\n")
        self.write(repo, "src/logo.bin", b"\x00\x01binary")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])],
                     exclude=["*.snap"])
        root = akashic.repo_root(repo)
        out = akashic.render_prompt(root, akashic.load_catalog(root), "index")

        self.assertIn("src/app.py", out)
        for dropped in ("bundle.min.js", "package-lock.json",
                        "fixture.snap", "logo.bin"):
            self.assertNotIn(dropped, out,
                             f"{dropped} must not be offered as a citable file")

    def test_prompt_unknown_page_id_fails(self):
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/app.py"])])
        root = akashic.repo_root(repo)
        with self.assertRaises(SystemExit) as ctx:
            akashic.render_prompt(root, akashic.load_catalog(root), "ghost")
        self.assertEqual(ctx.exception.code, 2)

    def test_citation_outside_scope_warns_not_errors(self):
        """A page citing a real, correctly-resolved file outside its own
        catalog scope is drift worth surfacing (scope may be too narrow or
        too wide), but the citation itself isn't wrong -- warn, don't block
        anchor."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "a\nb\nc\n")
        self.write(repo, "other/thing.py", "x\ny\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/**"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSources: [src/app.py:1-3](../../src/app.py#L1-L3), "
                   "[other/thing.py:1-2](../../other/thing.py#L1-L2)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertEqual(errors, [],
                         f"an out-of-scope citation must not fail verify, got: {errors}")

    def test_link_to_unknown_page_id_still_fails(self):
        repo = self.valid_repo_for_verify()
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\nSee [Ghost](./ghost-page.md).\n\n"
                   "Sources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        errors = akashic.verify_repo(akashic.repo_root(repo))
        self.assertTrue(any("unknown page id" in e for e in errors),
                        f"a link to a nonexistent catalog id must still fail, got: {errors}")


class TestLoop(RepoCase):
    """The runner's decision layer. The LLM invocation itself is not covered:
    it shells out to `claude -p`, and a test that stubbed it would only assert
    the stub. What is covered is everything that decides whether to spend."""

    def test_repo_list_ignores_comments_blanks_and_expands_home(self):
        text = ("# fleet\n"
                "/srv/one\n"
                "\n"
                "   /srv/two   # trailing note\n"
                "~/three\n")
        repos = akashic_loop.read_repo_list(text)
        self.assertEqual(repos[:2], ["/srv/one", "/srv/two"])
        self.assertTrue(repos[2].endswith("/three"))
        self.assertNotIn("~", repos[2], "~ must be expanded, not passed to git")

    def test_edited_only_is_never_handed_to_an_llm(self):
        """Hard rule 2: a human edited that page, so regenerating is the wrong
        response. It goes to the owner, not to a subagent."""
        report = {"stale": [], "edited": ["index"], "orphaned": [],
                  "uncovered": [], "missing": [], "planned": []}
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.REVIEW_ONLY)

    def test_edited_alongside_real_work_still_updates(self):
        report = {"stale": [{"id": "a", "changed": ["f.py"]}],
                  "edited": ["index"], "orphaned": [], "uncovered": [],
                  "missing": [], "planned": []}
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.NEEDS_UPDATE,
                         "the update flow skips edited pages itself; the "
                         "other buckets still need doing")

    def test_clean_repo_costs_nothing(self):
        report = {"stale": [], "edited": [], "orphaned": [], "uncovered": [],
                  "missing": [], "planned": []}
        self.assertEqual(akashic_loop.classify(report), akashic_loop.CLEAN)

    def test_planned_pages_count_as_work(self):
        """A generate run that died leaves planned pages and nothing else;
        the loop has to notice rather than call the repo clean."""
        report = {"stale": [], "edited": [], "orphaned": [], "uncovered": [],
                  "missing": [], "planned": ["ghost"]}
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.NEEDS_UPDATE)

    def test_gate_runs_against_a_real_repo_and_costs_no_tokens(self):
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md",
                   "# A\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor=self.head(repo))
        report, needs_work = akashic_loop.stale_report(str(repo))
        self.assertFalse(needs_work)
        self.assertEqual(akashic_loop.classify(report), akashic_loop.CLEAN)

        self.write(repo, "f1.py", "changed\n")
        self.commit(repo, "touch it")
        report, needs_work = akashic_loop.stale_report(str(repo))
        self.assertTrue(needs_work)
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.NEEDS_UPDATE)

    def test_a_broken_repo_raises_rather_than_reporting_clean(self):
        """Failure visibility: a repo the gate cannot read must never look
        like a repo with nothing to do."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)  # no .akashic at all -> akashic.py exits 2
        with self.assertRaises(RuntimeError):
            akashic_loop.stale_report(str(repo))


if __name__ == "__main__":
    unittest.main(verbosity=2)
