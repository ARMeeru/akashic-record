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

    def ranged_repo(self):
        """A page citing lines 5-10 of a 40-line file, properly anchored."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "".join(f"line {n}\n" for n in range(1, 41)))
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md",
                   "# A\n\nSources: "
                   "[src/app.py:5-10](../../src/app.py#L5-L10)\n")
        self.catalog(repo, [self.page("a", scope=["src/*"])],
                     anchor=self.head(repo))
        self.commit(repo, "wiki")
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        self.assertEqual(
            akashic.load_catalog(root)["pages"][0]["ranges"],
            {"src/app.py": [[5, 10]]},
            "anchor must record the cited span in anchor coordinates")
        return repo, root

    def test_change_outside_the_cited_lines_is_not_stale(self):
        """The false positive this mechanism exists to remove: a page citing
        lines 5-10 should not regenerate because line 35 changed."""
        repo, root = self.ranged_repo()
        self.write(repo, "src/app.py",
                   "".join(("line 35 rewritten\n" if n == 35 else f"line {n}\n")
                           for n in range(1, 41)))
        self.commit(repo, "edit a line the page does not cite")
        self.assertEqual(akashic.compute_stale(root)["stale"], [])

    def test_change_inside_the_cited_lines_is_stale(self):
        repo, root = self.ranged_repo()
        self.write(repo, "src/app.py",
                   "".join(("line 7 rewritten\n" if n == 7 else f"line {n}\n")
                           for n in range(1, 41)))
        self.commit(repo, "edit a cited line")
        self.assertEqual([s["id"] for s in akashic.compute_stale(root)["stale"]],
                         ["a"])

    def test_append_at_eof_does_not_stale_a_page(self):
        """Recorded ends are routinely one past the last line, because a file
        ending in a newline displays a phantom empty line. Unclamped, an append
        at EOF would intersect it and stale every cite-to-end page."""
        repo, root = self.ranged_repo()
        with open(repo / "src/app.py", "a", encoding="utf-8") as fh:
            fh.write("line 41\n")
        self.commit(repo, "append")
        self.assertEqual(akashic.compute_stale(root)["stale"], [])

    def test_pure_rename_stays_file_level_stale(self):
        """A 100%-similarity rename emits no hunks at all, so range logic can
        say nothing about it and the page must stay stale: its citations now
        point at a path that no longer exists."""
        repo, root = self.ranged_repo()
        sh(repo, "git", "mv", "src/app.py", "src/renamed.py")
        self.commit(repo, "rename")
        self.assertEqual([s["id"] for s in akashic.compute_stale(root)["stale"]],
                         ["a"])

    def test_uncited_file_in_scope_keeps_file_level_staleness(self):
        repo, root = self.ranged_repo()
        self.write(repo, "src/helper.py", "helper\n")
        self.commit(repo, "add an in-scope file the page never cites")
        akashic.anchor_repo(root)
        self.assertNotIn("src/helper.py",
                         akashic.load_catalog(root)["pages"][0]["ranges"],
                         "a file with no cited fragment records no range")
        self.write(repo, "src/helper.py", "helper changed\n")
        self.commit(repo, "change it")
        self.assertEqual([s["id"] for s in akashic.compute_stale(root)["stale"]],
                         ["a"], "no recorded range means file-level staleness")

    def test_content_shifted_above_a_citation_is_reported_as_drift(self):
        """The hole range-level staleness opens: a change above the cited lines
        touches no cited span, so the page is not stale -- but its line numbers
        now point at different code, in bounds, so verify cannot see it."""
        repo, root = self.ranged_repo()
        original = (repo / "src/app.py").read_text(encoding="utf-8")
        (repo / "src/app.py").write_text("import new\n" * 5 + original,
                                         encoding="utf-8")
        self.commit(repo, "insert five lines above the cited block")

        report = akashic.compute_stale(root)
        self.assertEqual(report["stale"], [], "cited lines were not touched")
        self.assertEqual([d["id"] for d in report["drifted"]], ["a"],
                         "but they moved, and that has to be visible")

    def test_remap_shifts_citations_without_an_llm(self):
        repo, root = self.ranged_repo()
        original = (repo / "src/app.py").read_text(encoding="utf-8")
        (repo / "src/app.py").write_text("import new\n" * 5 + original,
                                         encoding="utf-8")
        self.commit(repo, "insert five lines above the cited block")

        remapped, skipped = akashic.remap_repo(root)
        self.assertEqual((remapped, skipped), (["a"], []))
        body = (repo / ".akashic/wiki/a.md").read_text(encoding="utf-8")
        self.assertIn("[src/app.py:10-15](../../src/app.py#L10-L15)", body,
                      "both the destination and the human-readable text shift")
        self.assertNotIn("L5-L10", body)
        self.assertIsNone(akashic.load_catalog(root)["pages"][0]["hash"],
                          "a remapped page is blessed as tool-written")

        self.commit(repo, "remapped")
        akashic.anchor_repo(root)
        self.assertEqual(akashic.compute_stale(root)["drifted"], [],
                         "anchoring re-records the corrected ranges")

    def test_remap_leaves_fenced_examples_alone(self):
        repo, root = self.ranged_repo()
        page = repo / ".akashic/wiki/a.md"
        page.write_text(
            "# A\n\nSources: [src/app.py:5-10](../../src/app.py#L5-L10)\n\n"
            "```markdown\nSources: [src/app.py:5-10](../../src/app.py#L5-L10)\n```\n",
            encoding="utf-8")
        akashic.bless_pages(root, ["a"])
        self.commit(repo, "page with a fenced example")
        akashic.anchor_repo(root)
        original = (repo / "src/app.py").read_text(encoding="utf-8")
        (repo / "src/app.py").write_text("import new\n" * 5 + original,
                                         encoding="utf-8")
        self.commit(repo, "shift")

        akashic.remap_repo(root)
        body = page.read_text(encoding="utf-8")
        fenced = body.split("```markdown\n")[1]
        self.assertIn("L5-L10", fenced,
                      "an example citation is documentation, not a dependency")
        self.assertIn("#L10-L15", body.split("```markdown")[0],
                      "the real citation still shifts")

    def test_remap_never_rewrites_a_human_edited_page(self):
        repo, root = self.ranged_repo()
        with open(repo / ".akashic/wiki/a.md", "a", encoding="utf-8") as fh:
            fh.write("\nHuman correction.\n")
        original = (repo / "src/app.py").read_text(encoding="utf-8")
        (repo / "src/app.py").write_text("import new\n" * 5 + original,
                                         encoding="utf-8")
        self.commit(repo, "shift under a hand-edited page")

        remapped, skipped = akashic.remap_repo(root)
        self.assertEqual((remapped, skipped), ([], ["a"]))
        self.assertIn("L5-L10",
                      (repo / ".akashic/wiki/a.md").read_text(encoding="utf-8"),
                      "hard rule 2 covers line numbers too")

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


class TestRestated(RepoCase):
    """A page's *brief* changing, as opposed to its sources. Both plan gates
    produce goal and scope edits as their primary output, so without this
    every finding on a page that happened not to be stale went nowhere."""

    def brief_repo(self, scope=("src/a.py",), goal="document a"):
        repo = self.make_repo()
        self.write(repo, "src/a.py", "def a():\n    return 1\n")
        self.write(repo, "src/b.py", "def b():\n    return 2\n")
        self.write(repo, ".akashic/wiki/p.md",
                   "# P\n\n## S\n\nDocs a.\n\n"
                   "Sources: [src/a.py:1-2](../../src/a.py#L1-L2)\n")
        self.catalog(repo, [self.page("p", scope=list(scope), goal=goal)])
        self.commit(repo)
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        self.commit(repo, "anchor")
        return repo, root

    def edit_catalog(self, repo, **fields):
        path = repo / ".akashic" / "catalog.json"
        data = json.loads(path.read_text())
        data["pages"][0].update(fields)
        path.write_text(json.dumps(data, indent=2))
        self.commit(repo, "edit brief")

    def test_a_widened_scope_onto_an_existing_file_is_reported(self):
        """The half that shipped invisible. `compute_stale` only sees a
        scope-matched file via `added_now`; a file predating the anchor that
        newly falls into scope matched nothing at all, so the page silently
        claimed a file it had never read. Widening a scope is also the plan
        critic's commonest prescribed fix."""
        repo, root = self.brief_repo()
        self.assertEqual(akashic.compute_stale(root)["restated"], [])
        self.edit_catalog(repo, scope=["src/*"])
        report = akashic.compute_stale(root)
        self.assertEqual(report["restated"],
                         [{"id": "p", "new_in_scope": ["src/b.py"]}])
        self.assertEqual(report["stale"], [],
                         "nothing in the code moved; only the ask did")

    def test_a_goal_edit_is_reported(self):
        repo, root = self.brief_repo()
        self.edit_catalog(repo, goal="document a and its callers")
        self.assertEqual(akashic.compute_stale(root)["restated"],
                         [{"id": "p", "goal_changed": True}])

    def test_whitespace_only_goal_changes_are_not_a_restatement(self):
        repo, root = self.brief_repo()
        self.edit_catalog(repo, goal="  document a\n")
        self.assertEqual(akashic.compute_stale(root)["restated"], [])

    def test_a_catalog_with_no_recorded_goal_hash_stays_quiet(self):
        """Migration: every catalog written before this existed lacks the
        field. Absent means unknown, and unknown says nothing -- the same
        direction `blobs` took."""
        repo, root = self.brief_repo()
        path = repo / ".akashic" / "catalog.json"
        data = json.loads(path.read_text())
        data["pages"][0].pop("goal_hash")
        data["pages"][0]["goal"] = "something else entirely"
        path.write_text(json.dumps(data, indent=2))
        self.commit(repo, "strip")
        self.assertEqual(akashic.compute_stale(root)["restated"], [])

    def test_regenerating_and_re_anchoring_clears_it(self):
        repo, root = self.brief_repo()
        self.edit_catalog(repo, goal="document a and its callers",
                          scope=["src/*"])
        self.assertEqual(len(akashic.compute_stale(root)["restated"]), 1)
        self.write(repo, ".akashic/wiki/p.md",
                   "# P\n\n## S\n\nDocs a and b.\n\n"
                   "Sources: [src/a.py:1-2](../../src/a.py#L1-L2), "
                   "[src/b.py:1-2](../../src/b.py#L1-L2)\n")
        akashic.bless_pages(root, ["p"])
        akashic.anchor_repo(root)
        self.commit(repo, "regen")
        report = akashic.compute_stale(root)
        self.assertEqual(report["restated"], [])
        self.assertTrue(all(not report[b] for b in akashic.WORK_BUCKETS),
                        f"post-anchor invariant must still hold: {report}")

    def test_anchor_records_a_goal_baseline_only_for_pages_it_wrote(self):
        """The bug that cost a real run. Stamping `goal_hash` on every done
        page asserts a correspondence nothing checked: for a page nobody
        regenerated, the text came from an *earlier* goal. Ten corrected goals
        were stamped onto pages that were never rewritten, `stale` reported
        clean, and the affected pages could only be found from memory."""
        repo, root = self.brief_repo()
        first = json.loads((repo / ".akashic" / "catalog.json").read_text())
        recorded = first["pages"][0]["goal_hash"]
        self.assertTrue(recorded, "a freshly generated page gets a baseline")

        # Goal corrected, page deliberately NOT regenerated, then anchored.
        self.edit_catalog(repo, goal="document a and its callers")
        akashic.anchor_repo(root)
        self.commit(repo, "anchor without regenerating")

        after = json.loads((repo / ".akashic" / "catalog.json").read_text())
        self.assertEqual(after["pages"][0]["goal_hash"], recorded,
                         "the old baseline must survive: the text still came "
                         "from the old goal")
        self.assertEqual(akashic.compute_stale(root)["restated"],
                         [{"id": "p", "goal_changed": True}],
                         "and the page must stay reported, not be blessed "
                         "into agreement by the anchor")

    def test_a_page_with_no_baseline_is_reported_rather_than_invented(self):
        """A catalog predating `goal_hash`. The tool does not know which goal
        produced the text, so it records nothing -- and says so, because an
        untracked page is otherwise indistinguishable from a tracked one."""
        repo, root = self.brief_repo()
        path = repo / ".akashic" / "catalog.json"
        data = json.loads(path.read_text())
        data["pages"][0].pop("goal_hash")
        path.write_text(json.dumps(data, indent=2))
        self.commit(repo, "strip baseline")

        akashic.anchor_repo(root)
        self.commit(repo, "anchor")
        after = json.loads(path.read_text())
        self.assertNotIn("goal_hash", after["pages"][0],
                         "anchor must not invent a baseline for text it did "
                         "not write")
        self.assertEqual(akashic.plan_check(root)["no_goal_baseline"], ["p"])

    def test_regenerating_clears_a_missing_baseline(self):
        """The hole is self-clearing: any page written by the tool gains a
        true baseline, so the migration completes as pages turn over."""
        repo, root = self.brief_repo()
        path = repo / ".akashic" / "catalog.json"
        data = json.loads(path.read_text())
        data["pages"][0].pop("goal_hash")
        path.write_text(json.dumps(data, indent=2))
        self.commit(repo, "strip baseline")
        self.assertEqual(akashic.plan_check(root)["no_goal_baseline"], ["p"])

        akashic.bless_pages(root, ["p"])
        akashic.anchor_repo(root)
        self.commit(repo, "regen")
        self.assertEqual(akashic.plan_check(root)["no_goal_baseline"], [])

    def test_the_runner_gate_sees_it(self):
        repo, root = self.brief_repo()
        self.edit_catalog(repo, scope=["src/*"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(buf):
            code = akashic.cmd_stale(root, check=True)
        self.assertEqual(code, 1, "a changed brief is outstanding work")
        self.assertIn("restated", buf.getvalue())


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

    def identifier_repo(self, page_body):
        repo = self.make_repo()
        self.write(repo, "src/app.py",
                   "def real_function():\n    return 1\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/*"])])
        self.write(repo, ".akashic/wiki/index.md", page_body)
        return akashic.repo_root(repo)

    def warnings_from_verify(self, root):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            errors = akashic.verify_repo(root)
        return errors, buf.getvalue()

    def test_invented_identifier_warns_but_never_blocks(self):
        """verify proves a citation resolves; it cannot prove the prose above
        it is true. This closes the narrowest part of that gap -- a page naming
        a function that exists in none of the files it points at -- and stays a
        warning, because a heuristic over prose that blocked anchor would
        eventually block a correct page."""
        root = self.identifier_repo(
            "# Index\n\n## Behaviour\n\nThe `imaginary_helper()` does the work.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [], "must not block anchor")
        self.assertIn("imaginary_helper()", stderr)

    def test_real_identifier_and_filenames_do_not_warn(self):
        """A filename carries a dot and so matches the identifier shape, but
        whether it exists is the citation gate's question, not this one's. A
        false warning per backticked filename would train the operator to
        ignore the whole class."""
        root = self.identifier_repo(
            "# Index\n\n## Behaviour\n\n`real_function()` lives in `src/app.py`, "
            "described in `DESIGN.md`.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("appears in no file", stderr)

    def test_fenced_example_identifiers_are_not_checked(self):
        root = self.identifier_repo(
            "# Index\n\n## Behaviour\n\nReal prose.\n\n"
            "```python\nimaginary_helper()\n```\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("imaginary_helper", stderr)

    def anchored_range_repo(self, page_cites, prelude=""):
        """A repo anchored with `body()` cited at 1-3, then `prelude` lines
        inserted above it so the function moves. The page ends up citing
        whatever `page_cites` says, which is the variable under test."""
        repo = self.make_repo()
        original = "def body():\n    x = 1\n    return x\n"
        self.write(repo, "src/app.py", original)
        self.commit(repo)
        page = ("# Index\n\n## Behaviour\n\nDescribes the body.\n\n"
                "Sources: [src/app.py:1-3](../../src/app.py#L1-L3)\n")
        self.write(repo, ".akashic/wiki/index.md", page)
        self.catalog(repo, [self.page("index", files=["src/app.py"],
                                      scope=["src/*"])])
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)  # records ranges in these coordinates
        self.commit(repo, "wiki")

        self.write(repo, "src/app.py", prelude + original)
        self.commit(repo, "shift it")
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\n## Behaviour\n\nDescribes the body.\n\n"
                   f"Sources: {page_cites}\n")
        return root

    def test_a_citation_moved_to_the_wrong_lines_is_warned_about(self):
        """The gap this closes. `verify` proves a range is inside the file, so
        a citation rewritten to plausible-but-wrong numbers passes every gate:
        three mechanical fixes to this repo's own wiki shipped exactly that.
        `ranges` are in anchor coordinates and the anchor commit is recorded,
        so the tool can read what a span held and find where it went."""
        root = self.anchored_range_repo(
            "[src/app.py:1-3](../../src/app.py#L1-L3)",  # stale numbers
            prelude="# a\n# b\n# c\n# d\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [], "must never block anchor")
        self.assertIn("anchored to in src/app.py", stderr)
        self.assertIn("5-7", stderr, f"must name where it went: {stderr!r}")

    def test_a_correctly_rederived_citation_is_silent(self):
        """The other direction. If this ever warns on a correct refresh, the
        whole class gets filtered out unread."""
        root = self.anchored_range_repo(
            "[src/app.py:5-7](../../src/app.py#L5-L7)",  # right numbers
            prelude="# a\n# b\n# c\n# d\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr)

    def test_a_narrowed_citation_is_not_a_dropped_claim(self):
        """The whole of this check's first live outing: 13 warnings, every one
        a rewrite that tightened a range. Requiring full containment assumed a
        correct rewrite cites at least as much as the last one did, and a good
        rewrite routinely cites less."""
        root = self.anchored_range_repo(
            "[src/app.py:6-7](../../src/app.py#L6-L7)",  # was 1-3, now 5-7
            prelude="# a\n# b\n# c\n# d\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr,
                         "citing part of the anchored content is not losing it")

    def test_a_citation_split_around_real_content_is_not_a_dropped_claim(self):
        """`merge_spans` deliberately will not bridge a gap holding real code,
        so a page citing two halves of a region has two intervals. Under
        containment that read as uncovered."""
        root = self.anchored_range_repo(
            "[src/app.py:5-5](../../src/app.py#L5-L5), "
            "[src/app.py:7-7](../../src/app.py#L7-L7)",
            prelude="# a\n# b\n# c\n# d\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr)

    def test_repeated_boundary_lines_produce_no_guess(self):
        """A wrong relocation would produce exactly the confidently-wrong line
        numbers this check exists to catch, so ambiguity yields no answer."""
        root = self.anchored_range_repo(
            "[src/app.py:1-3](../../src/app.py#L1-L3)",  # stale numbers
            prelude="def body():\n    x = 1\n    return x\n\n# pad\n\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr)

    def test_content_split_across_two_citations_is_still_covered(self):
        """Found by running this check against this repo's own wiki. A page
        that grows splits one region across two citations with a blank line
        between them; testing each citation on its own called that a hole and
        warned about a correct page. Coverage is their union."""
        root = self.anchored_range_repo(
            "[src/app.py:5-6](../../src/app.py#L5-L6), "
            "[src/app.py:7-7](../../src/app.py#L7-L7)",
            prelude="# a\n# b\n# c\n# d\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr)

    def test_merge_spans_bridges_whitespace_only_gaps(self):
        lines = ["a", "", "b", "   ", "c", "x", "d"]
        self.assertEqual(akashic.merge_spans([(1, 1), (3, 3)], lines),
                         [(1, 3)], "a blank-only gap is not a hole")
        self.assertEqual(akashic.merge_spans([(1, 1), (7, 7)], lines),
                         [(1, 1), (7, 7)], "real content between them is")

    def test_unreachable_anchor_says_nothing(self):
        """Every page is already reported stale in that state, so a warning per
        span would be noise on every shallow clone."""
        root = self.anchored_range_repo(
            "[src/app.py:1-3](../../src/app.py#L1-L3)",
            prelude="# a\n# b\n# c\n# d\n")
        catalog = akashic.load_catalog(root)
        catalog["anchor"] = "0" * 40
        akashic.save_catalog(root, catalog)
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("anchored to", stderr)

    def test_a_qualified_prose_name_matches_its_bare_declaration(self):
        """The dominant false positive on a real repo. A page writes
        `users.firstName`; the column is declared `firstName` and the
        qualified string appears nowhere. 43% of 187 warnings were this."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "firstName = 1\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/*"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\n## S\n\nThe `users.firstName` column.\n\n"
                   "Sources: [src/app.py:1-1](../../src/app.py#L1-L1)\n")
        errors, stderr = self.warnings_from_verify(akashic.repo_root(repo))
        self.assertEqual(errors, [])
        self.assertNotIn("users.firstName", stderr)

    def test_an_invented_name_still_warns_when_no_segment_matches(self):
        """The fallback must not swallow the case the check exists for."""
        repo = self.make_repo()
        self.write(repo, "src/app.py", "firstName = 1\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["src/*"])])
        self.write(repo, ".akashic/wiki/index.md",
                   "# Index\n\n## S\n\nThe `users.lastName` column.\n\n"
                   "Sources: [src/app.py:1-1](../../src/app.py#L1-L1)\n")
        errors, stderr = self.warnings_from_verify(akashic.repo_root(repo))
        self.assertEqual(errors, [])
        self.assertIn("users.lastName", stderr)

    def test_scoped_names_fall_back_on_their_last_segment_too(self):
        root = self.identifier_repo(
            "# Index\n\n## S\n\nCast it with `$n::real_function`.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertNotIn("real_function", stderr)

    def test_builtin_namespaces_and_convention_words_are_not_identifiers(self):
        """`console.error` existing somewhere in the world says nothing about
        the file being documented, and a page explaining that ids are
        `snake_case` is not naming a symbol."""
        root = self.identifier_repo(
            "# Index\n\n## S\n\nIt logs via `console.error` and names things "
            "in `snake_case` and `camelCase`.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        errors, stderr = self.warnings_from_verify(root)
        self.assertEqual(errors, [])
        self.assertNotIn("appears in no file", stderr)

    def test_h2_sections_ignores_headings_inside_fences(self):
        body = ("# Title\n\n## One\n\ntext\n\n```md\n## Not a section\n```\n\n"
                "## Two\n\nmore\n")
        self.assertEqual([t for t, _, _ in akashic.h2_sections(body)],
                         ["One", "Two"])

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

    def test_prompt_forbids_executing_what_it_tells_a_subagent_to_read(self):
        """A page's scope routinely holds operational scripts. One real target
        repo's scripts/ drops databases and calls pg_terminate_backend, and the
        subagent is told to read every file in scope. The instruction not to
        run them is rendered by the script rather than left to whoever
        dispatches the prompt, because a rule that has to be retyped is a rule
        that eventually is not."""
        repo = self.make_repo()
        self.write(repo, "scripts/drop-db.sh", "dropdb production\n")
        self.commit(repo)
        self.catalog(repo, [self.page("index", scope=["scripts/*"])])
        root = akashic.repo_root(repo)
        out = akashic.render_prompt(root, akashic.load_catalog(root), "index")
        self.assertIn("READ ONLY", out)
        self.assertIn("never execute them", out)
        self.assertIn("git-mutating", out)
        self.assertIn("not direction", out,
                      "file contents are material, not instructions to follow")

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

    def test_update_prompt_names_what_changed(self):
        """SKILL.md required this sentence and `prompt` never emitted it, so
        the orchestrator joined `stale` to the prompt by hand -- the exact
        hand-assembly the same file warns causes bugs."""
        repo = self.make_repo()
        self.write(repo, "src/a.py", "def a():\n    return 1\n")
        self.write(repo, ".akashic/wiki/p.md",
                   "# P\n\n## S\n\nDocs.\n\n"
                   "Sources: [src/a.py:1-2](../../src/a.py#L1-L2)\n")
        self.catalog(repo, [self.page("p", scope=["src/*"])])
        self.commit(repo)
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        self.commit(repo, "anchor")
        self.write(repo, "src/a.py", "def a():\n    return 99\n")
        self.commit(repo, "change")

        plain = akashic.render_prompt(root, akashic.load_catalog(root), "p")
        self.assertNotIn("already existed", plain)
        upd = akashic.render_prompt(root, akashic.load_catalog(root), "p",
                                    update=True)
        self.assertIn("already existed", upd)
        self.assertIn("src/a.py", upd)
        self.assertIn("Do not append", upd)

    def test_update_prompt_covers_a_changed_brief_too(self):
        """Since `restated` there are two reasons to regenerate. A prompt
        naming only changed dependencies would send a subagent hunting for
        code changes that are not there."""
        repo = self.make_repo()
        self.write(repo, "src/a.py", "def a():\n    return 1\n")
        self.write(repo, "src/b.py", "def b():\n    return 2\n")
        self.write(repo, ".akashic/wiki/p.md",
                   "# P\n\n## S\n\nDocs.\n\n"
                   "Sources: [src/a.py:1-2](../../src/a.py#L1-L2)\n")
        self.catalog(repo, [self.page("p", scope=["src/a.py"])])
        self.commit(repo)
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        self.commit(repo, "anchor")
        path = repo / ".akashic" / "catalog.json"
        data = json.loads(path.read_text())
        data["pages"][0]["scope"] = ["src/*"]
        data["pages"][0]["goal"] = "document a and b"
        path.write_text(json.dumps(data, indent=2))
        self.commit(repo, "restate")

        upd = akashic.render_prompt(root, akashic.load_catalog(root), "p",
                                    update=True)
        self.assertIn("goal was rewritten", upd)
        self.assertIn("newly fall inside this page's scope", upd)
        self.assertIn("src/b.py", upd)

    def test_update_prompt_is_silent_for_a_fresh_page(self):
        root = self.prompt_repo() if hasattr(self, "prompt_repo") else None
        repo = self.make_repo()
        self.write(repo, "src/a.py", "def a():\n    return 1\n")
        self.write(repo, ".akashic/wiki/p.md",
                   "# P\n\n## S\n\nDocs.\n\n"
                   "Sources: [src/a.py:1-2](../../src/a.py#L1-L2)\n")
        self.catalog(repo, [self.page("p", scope=["src/*"])])
        self.commit(repo)
        root = akashic.repo_root(repo)
        akashic.anchor_repo(root)
        self.commit(repo, "anchor")
        upd = akashic.render_prompt(root, akashic.load_catalog(root), "p",
                                    update=True)
        self.assertNotIn("already existed", upd)
        self.assertNotIn("Do not append", upd)

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


class TestPlanCheck(RepoCase):
    """Shape checks on a catalog before any subagent is dispatched. Every page
    here is `planned`, because pre-flight is the whole point."""

    def plan_repo(self, pages, tree=None):
        repo = self.make_repo()
        for rel, body in (tree or {"src/a.py": "one\ntwo\n",
                                   "src/b.py": "three\n"}).items():
            self.write(repo, rel, body)
        self.commit(repo)
        self.catalog(repo, pages)
        return akashic.plan_check(akashic.repo_root(repo))

    def test_scope_matching_nothing_is_surfaced_before_the_fan_out(self):
        """Today this is visible only inside a rendered prompt, which nobody
        reads until a subagent has been dispatched with nothing to cite."""
        report = self.plan_repo([
            self.page("ghost", scope=["docs/*"], status="planned"),
            self.page("real", scope=["src/*"], status="planned")])
        self.assertEqual(report["empty_scope"], ["ghost"])
        self.assertEqual(report["no_scope"], [])

    def test_a_page_with_no_scope_at_all_is_a_separate_finding(self):
        report = self.plan_repo([self.page("bare", status="planned")])
        self.assertEqual(report["no_scope"], ["bare"])
        self.assertEqual(report["empty_scope"], [],
                         "no scope and an unmatched scope are different bugs")

    def test_a_scope_inside_a_siblings_is_reported_as_a_subset(self):
        """The defect this was built for: two pages scoped to the same bulk
        means both subagents read it, both write about it, and every change
        there marks two pages stale instead of one."""
        report = self.plan_repo([
            self.page("narrow", scope=["src/a.py"], status="planned"),
            self.page("wide", scope=["src/*"], status="planned")])
        self.assertEqual(report["subset"],
                         [{"page": "narrow", "of": "wide", "files": 1}])
        self.assertEqual(report["overlap"], [],
                         "a pair is reported once, under the sharper heading")

    def test_partial_overlap_is_reported_with_files_and_lines(self):
        tree = {"src/a.py": "1\n2\n", "src/b.py": "3\n",
                "src/c.py": "4\n", "src/d.py": "5\n"}
        report = self.plan_repo([
            self.page("one", scope=["src/a.py", "src/b.py", "src/c.py"],
                      status="planned"),
            self.page("two", scope=["src/a.py", "src/b.py", "src/d.py"],
                      status="planned")], tree=tree)
        self.assertEqual(len(report["overlap"]), 1)
        found = report["overlap"][0]
        self.assertEqual(found["pages"], ["one", "two"])
        self.assertEqual(found["files"], 2)
        self.assertEqual(found["lines"], 3, "2 lines in a.py plus 1 in b.py")

    def test_overlap_is_ranked_by_duplicated_lines_not_file_count(self):
        """The defect that shipped: gating on shared files as a fraction of
        the smaller page missed a pair sharing one enormous file. Cost is
        measured in lines a second subagent re-reads, so lines order it."""
        tree = {"src/huge.py": "x\n" * 400, "src/a.py": "x\n",
                "src/b.py": "x\n", "src/c.py": "x\n", "src/d.py": "x\n"}
        report = self.plan_repo([
            self.page("big", scope=["src/huge.py", "src/a.py", "src/b.py"],
                      status="planned"),
            self.page("one-huge-file", scope=["src/huge.py", "src/c.py"],
                      status="planned"),
            self.page("many-tiny", scope=["src/a.py", "src/b.py", "src/d.py"],
                      status="planned")], tree=tree)
        pairs = [(o["pages"], o["files"], o["lines"])
                 for o in report["overlap"]]
        self.assertEqual(
            pairs[0][0], ["big", "one-huge-file"],
            f"one 400-line shared file must outrank two 1-line ones: {pairs}")
        self.assertEqual(pairs[0][1], 1, "and it shares fewer files")
        self.assertEqual(pairs[1][0], ["big", "many-tiny"])
        self.assertEqual(pairs[1][1], 2)

    def test_growing_one_page_never_silences_an_unrelated_pair(self):
        """Monotonicity, and the exact regression. Widening a page from 5 to 7
        files for unrelated reasons dropped a true warning about a different
        pair whose intersection had not changed. Any ratio against page size
        has this defect: the denominator moves for reasons the pair knows
        nothing about."""
        tree = {f"src/f{i}.py": "x\n" for i in range(9)}
        shared = ["src/f0.py", "src/f1.py"]
        before = self.plan_repo([
            self.page("a", scope=shared + ["src/f2.py"], status="planned"),
            self.page("b", scope=shared + ["src/f3.py", "src/f4.py"],
                      status="planned")], tree=tree)
        after = self.plan_repo([
            self.page("a", scope=shared + ["src/f2.py", "src/f5.py",
                                           "src/f6.py", "src/f7.py",
                                           "src/f8.py"], status="planned"),
            self.page("b", scope=shared + ["src/f3.py", "src/f4.py"],
                      status="planned")], tree=tree)

        def found(report):
            return [o for o in report["overlap"] if o["pages"] == ["a", "b"]]
        self.assertEqual(len(found(before)), 1)
        self.assertEqual(len(found(after)), 1,
                         "growing page a must not drop the a/b finding")
        self.assertEqual(found(before)[0]["files"], found(after)[0]["files"],
                         "the intersection itself never changed")

    def test_every_overlapping_pair_is_listed(self):
        """No threshold. Deciding which overlaps matter needs judgement about
        what the pages are for, which is plan-critic's job -- and it receives
        this list. A script that pre-filtered would hide the case it got
        wrong, which is exactly what happened."""
        tree = {f"src/f{i}.py": "x\n" for i in range(6)}
        report = self.plan_repo([
            self.page("one", scope=[f"src/f{i}.py" for i in range(4)],
                      status="planned"),
            self.page("two", scope=["src/f3.py", "src/f4.py", "src/f5.py"],
                      status="planned")], tree=tree)
        self.assertEqual([o["pages"] for o in report["overlap"]],
                         [["one", "two"]])
        self.assertEqual(report["overlap"][0]["files"], 1)

    def test_oversized_scope_cites_the_documented_split_rule(self):
        tree = {f"src/f{i}.py": "x\n"
                for i in range(akashic.SPLIT_THRESHOLD + 1)}
        report = self.plan_repo(
            [self.page("huge", scope=["src/*"], status="planned")], tree=tree)
        self.assertEqual(report["oversized"],
                         [{"id": "huge", "files": akashic.SPLIT_THRESHOLD + 1}])

    def test_empty_goals_and_duplicate_titles(self):
        report = self.plan_repo([
            self.page("a", scope=["src/a.py"], status="planned", goal="  ",
                      title="Same"),
            self.page("b", scope=["src/b.py"], status="planned",
                      title="same")])
        self.assertEqual(report["empty_goal"], ["a"])
        self.assertEqual(report["duplicate_titles"],
                         [{"title": "same", "ids": ["a", "b"]}])

    def test_line_totals_are_reported_biggest_first(self):
        """Data, not a finding: a 10k-line outlier should be visible before
        dispatch rather than discovered in the bill."""
        report = self.plan_repo([
            self.page("small", scope=["src/b.py"], status="planned"),
            self.page("big", scope=["src/a.py"], status="planned")])
        self.assertEqual([p["id"] for p in report["pages"]], ["big", "small"])
        self.assertEqual(report["pages"][0]["lines"], 2)

    def test_scope_expansion_matches_what_a_subagent_would_be_given(self):
        """One definition of the citable universe. If plan-check counted files
        the prompt would not offer, it would be measuring a different plan."""
        tree = {"src/a.py": "1\n", "src/bundle.min.js": "x\n",
                "package-lock.json": "{}\n"}
        report = self.plan_repo(
            [self.page("p", scope=["src/*", "package-lock.json"],
                       status="planned")], tree=tree)
        self.assertEqual(report["pages"][0]["files"], 1)


class TestPlanCritic(RepoCase):
    """The rendered critic prompt. Only the rendering is testable -- the
    judgment is an LLM's, and a stubbed test would assert the stub. What is
    covered is that everything a judge needs is actually in the prompt, since
    an omission there is silent and produces a confident, uninformed review."""

    def critic_repo(self, pages, tree=None):
        repo = self.make_repo()
        for rel, body in (tree or {"src/a.py": "one\n",
                                   "src/b.py": "two\n"}).items():
            self.write(repo, rel, body)
        self.commit(repo)
        self.catalog(repo, pages)
        root = akashic.repo_root(repo)
        return akashic.render_plan_critic(root, akashic.load_catalog(root))

    def test_every_goal_and_scope_reaches_the_judge(self):
        text = self.critic_repo([
            self.page("one", scope=["src/a.py"], status="planned",
                      goal="explain the widget pipeline"),
            self.page("two", scope=["src/b.py"], status="planned",
                      goal="explain the gadget cache")])
        for needle in ("explain the widget pipeline", "explain the gadget cache",
                       "src/a.py", "src/b.py"):
            self.assertIn(needle, text)

    def test_all_four_judgments_are_stated(self):
        """The four questions are the issue's substance. If templating drops
        one, the review silently stops covering that class."""
        text = self.critic_repo(
            [self.page("one", scope=["src/*"], status="planned")])
        for needle in ("Substantiation", "Truthfulness", "Collision",
                       "Redundant scope"):
            self.assertIn(needle, text)

    def test_the_scope_is_named_as_the_citable_boundary(self):
        """Without this the judge grades goals on truth alone and misses the
        commonest defect: a true claim about code the page may not cite."""
        text = self.critic_repo(
            [self.page("one", scope=["src/*"], status="planned")])
        self.assertIn("ENTIRE set of files its subagent may cite", text)

    def test_deterministic_findings_are_handed_over_not_re_derived(self):
        text = self.critic_repo([
            self.page("narrow", scope=["src/a.py"], status="planned"),
            self.page("wide", scope=["src/*"], status="planned")])
        self.assertIn("do not re-derive", text)
        self.assertIn("subset", text)

    def test_a_clean_plan_omits_the_findings_block_entirely(self):
        text = self.critic_repo(
            [self.page("one", scope=["src/a.py"], status="planned")])
        self.assertNotIn("do not re-derive", text,
                         "an empty findings block is boilerplate that teaches "
                         "the judge to skim")

    def test_an_unmatched_scope_is_called_out_inline(self):
        text = self.critic_repo(
            [self.page("ghost", scope=["docs/*"], status="planned")])
        self.assertIn("scope matches no tracked file", text)

    def test_long_file_lists_are_truncated_but_never_silently(self):
        n = akashic.CRITIC_FILE_SAMPLE + 5
        tree = {f"src/f{i:03d}.py": "x\n" for i in range(n)}
        text = self.critic_repo(
            [self.page("big", scope=["src/*"], status="planned")], tree=tree)
        self.assertIn(f"and {n - akashic.CRITIC_FILE_SAMPLE} more", text)
        self.assertIn("prompt big", text, "must say how to get the full list")
        self.assertIn(f"files: {n}", text, "the true count is still stated")

    def test_the_read_only_mandate_is_rendered(self):
        """A target repo's scope routinely includes operational scripts, and a
        reviewer that starts running things is worse than no reviewer."""
        text = self.critic_repo(
            [self.page("one", scope=["src/*"], status="planned")])
        self.assertIn("READ ONLY", text)
        self.assertIn("do not edit the catalog", text)

    def test_the_critic_is_told_its_verdict_is_one_sample(self):
        """Two passes over an unchanged catalog disagreed in both directions.
        That is inherent to an LLM judging meaning, but a report ending "13
        pages need edits" reads as a measurement, and nothing said otherwise."""
        text = self.critic_repo(
            [self.page("one", scope=["src/*"], status="planned")])
        self.assertIn("one sample, not a measurement", text)
        self.assertIn("stated as a sample", text)

    def test_critic_out_writes_the_prompt_and_prints_a_dispatch_line(self):
        """Same cost and same shape as `audit prompt`, which got `--out` first;
        this one rendered 49KB on a real run and was read then pasted."""
        repo = self.make_repo()
        self.write(repo, "src/a.py", "one\n")
        self.commit(repo)
        self.catalog(repo, [self.page("p", scope=["src/*"], status="planned")])
        root = akashic.repo_root(repo)
        out = Path(tempfile.mkdtemp(prefix="akashic-critic-")) / "c.md"
        self.addCleanup(shutil.rmtree, out.parent, ignore_errors=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = akashic.cmd_plan_critic(root, out=str(out))
        self.assertEqual(code, 0)
        self.assertIn("Judge each page on four questions", out.read_text())
        self.assertNotIn("four questions", buf.getvalue(),
                         "the prompt body must not also go to stdout")
        self.assertIn("Read nothing else", buf.getvalue())

    def test_critic_out_refuses_to_write_inside_the_wiki_directory(self):
        repo = self.make_repo()
        self.write(repo, "src/a.py", "one\n")
        self.commit(repo)
        self.catalog(repo, [self.page("p", scope=["src/*"], status="planned")])
        root = akashic.repo_root(repo)
        with self.assertRaises(SystemExit) as ctx:
            akashic.cmd_plan_critic(
                root, out=str(root / ".akashic" / "scratch.md"))
        self.assertEqual(ctx.exception.code, 2)

    def test_an_empty_catalog_fails_rather_than_rendering_nothing(self):
        repo = self.make_repo()
        self.write(repo, "src/a.py", "one\n")
        self.commit(repo)
        self.catalog(repo, [])
        root = akashic.repo_root(repo)
        with self.assertRaises(SystemExit) as ctx:
            akashic.render_plan_critic(root, akashic.load_catalog(root))
        self.assertEqual(ctx.exception.code, 2)


class TestAudit(RepoCase):
    """The on-demand claim audit. Deterministic halves only: what gets
    extracted, and what the rendered judge prompt does and does not contain.
    The judgment is an LLM's and is not stubbed."""

    def audit_repo(self, page_body, source=None):
        repo = self.make_repo()
        self.write(repo, "src/app.py",
                   source or "def alpha():\n    return 1\n\n\ndef beta():\n"
                             "    return 2\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/index.md", page_body)
        self.catalog(repo, [self.page("index", files=["src/app.py"],
                                      scope=["src/*"])])
        return akashic.repo_root(repo)

    def test_evidence_is_the_exact_cited_bytes(self):
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        section = akashic.audit_extract(root)["pages"][0]["sections"][0]
        self.assertEqual(section["evidence"][0]["text"],
                         "def alpha():\n    return 1")

    def test_the_claim_drops_its_heading_and_its_sources_paragraph(self):
        """Neither is a claim. The Sources paragraph is also where the file
        paths live, which this check deliberately withholds."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        section = akashic.audit_extract(root)["pages"][0]["sections"][0]
        self.assertEqual(section["claim"], "It returns one.")
        self.assertEqual(section["title"], "Alpha")

    def test_the_rendered_prompt_never_names_a_file(self):
        """The design invariant. Every planning defect that motivated the
        audit work came from reasoning off a name; a judge shown the path
        fills gaps with what a file by that name usually contains."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        text = akashic.render_audit_prompt(root, "index")
        self.assertNotIn("src/app.py", text)
        self.assertIn("[E1]", text)
        self.assertIn("def alpha():", text)

    def test_the_path_mapping_still_travels_in_the_extract(self):
        """Blind for the judge, not for the orchestrator -- otherwise a
        finding names a label nobody can act on."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        item = akashic.audit_extract(root)["pages"][0]["sections"][0]["evidence"][0]
        self.assertEqual((item["label"], item["path"], item["start"], item["end"]),
                         ("E1", "src/app.py", 1, 2))

    def test_labels_are_unique_across_a_page(self):
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nOne.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n\n"
            "## Beta\n\nTwo.\n\n"
            "Sources: [src/app.py:5-6](../../src/app.py#L5-L6)\n")
        page = akashic.audit_extract(root)["pages"][0]
        labels = [e["label"] for s in page["sections"] for e in s["evidence"]]
        self.assertEqual(labels, ["E1", "E2"])
        self.assertEqual(len(set(labels)), len(labels))

    def test_the_prompt_tells_the_judge_to_default_to_refuting(self):
        """A judge that only reports what it can disprove reports almost
        nothing; the useful verdict is 'the evidence is silent on this'."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        text = akashic.render_audit_prompt(root, "index")
        self.assertIn("Default to refuting", text)
        for verdict in ("contradicted", "unsupported", "overstated"):
            self.assertIn(verdict, text)

    def test_an_uncited_section_says_so_rather_than_vanishing(self):
        """Dropping it would hide the strongest possible finding: prose
        resting on nothing at all."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n\n"
            "## Bare\n\nAsserted with no source whatsoever.\n")
        text = akashic.render_audit_prompt(root, "index")
        self.assertIn("Section: Bare", text)
        self.assertIn("EVIDENCE: none", text)

    def test_out_writes_the_prompt_and_prints_only_a_dispatch_line(self):
        """Evidence spans are verbatim source, so these prompts are large --
        one page in this repo renders 224KB. Printing it costs that twice:
        once into the orchestrator's context, once into the judge's, and the
        orchestrator gains nothing from having read it."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        out = Path(tempfile.mkdtemp(prefix="akashic-out-")) / "p.md"
        self.addCleanup(shutil.rmtree, out.parent, ignore_errors=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = akashic.cmd_audit_prompt(root, "index", out=str(out))
        self.assertEqual(code, 0)
        self.assertIn("def alpha():", out.read_text())
        self.assertNotIn("def alpha():", buf.getvalue(),
                         "the prompt body must not also go to stdout")
        self.assertIn(str(out), buf.getvalue())

    def test_out_refuses_to_write_inside_the_wiki_directory(self):
        """A 200KB scratch file there is picked up as `uncovered` and
        committed with the wiki."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        with self.assertRaises(SystemExit) as ctx:
            akashic.cmd_audit_prompt(
                root, "index", out=str(root / ".akashic" / "scratch.md"))
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((root / ".akashic" / "scratch.md").exists())

    def test_the_judge_is_told_not_to_grade_attribution(self):
        """It cannot see filenames by construction, so it cannot tell a
        correctly remembered one from an invention -- and reported every
        correct attribution as unsupported, which distorted the headline
        count enough to make the output hard to read."""
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nIt returns one.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        text = akashic.render_audit_prompt(root, "index")
        self.assertIn("Do not judge attribution", text)
        self.assertIn("one sample rather than a score", text)

    def test_unknown_page_ids_fail_rather_than_auditing_nothing(self):
        root = self.audit_repo(
            "# Index\n\n## Alpha\n\nOne.\n\n"
            "Sources: [src/app.py:1-2](../../src/app.py#L1-L2)\n")
        for call in (lambda: akashic.audit_extract(root, "ghost"),
                     lambda: akashic.render_audit_prompt(root, "ghost")):
            with self.assertRaises(SystemExit) as ctx:
                call()
            self.assertEqual(ctx.exception.code, 2)


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

    def test_drift_only_never_reaches_a_model(self):
        """Cited lines that moved without changing are arithmetic. That PR
        contains no generated prose at all, which is what makes it the one
        safe candidate for auto-merge later."""
        report = {"stale": [], "edited": [], "orphaned": [], "uncovered": [],
                  "missing": [], "planned": [], "drifted": [{"id": "a"}]}
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.REMAP_ONLY)

    def test_drift_alongside_real_work_still_updates(self):
        report = {"stale": [{"id": "a", "changed": ["f.py"]}], "edited": [],
                  "orphaned": [], "uncovered": [], "missing": [],
                  "planned": [], "drifted": [{"id": "b"}]}
        self.assertEqual(akashic_loop.classify(report),
                         akashic_loop.NEEDS_UPDATE)

    def test_only_deterministic_prs_may_merge_themselves(self):
        """The whole auto-merge policy in one assertion. A remap PR is integer
        arithmetic a reviewer can re-derive; a regeneration PR contains prose
        that `verify` cannot judge, and that difference is what a human is
        for. If this ever inverts, unread generated text starts landing on the
        default branch by itself."""
        self.assertTrue(akashic_loop.may_auto_merge(akashic_loop.REMAP_ONLY))
        for verdict in (akashic_loop.NEEDS_UPDATE, akashic_loop.REVIEW_ONLY,
                        akashic_loop.CLEAN):
            self.assertFalse(akashic_loop.may_auto_merge(verdict),
                             f"{verdict} must never merge itself")

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

    def test_a_dry_run_with_work_to_do_reaches_the_owner(self):
        """Under a scheduler, stdout is a log file nobody opens. Report-only
        mode is the mode you are told to start in, so if it only prints, a
        fleet needing work is indistinguishable from a clean one."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md",
                   "# A\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor=self.head(repo))
        self.write(repo, "f1.py", "changed\n")
        self.commit(repo, "touch it")

        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(buf):
            code = akashic_loop.process(str(repo), dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("akashic-loop:", buf.getvalue(),
                      "a dry run with outstanding work must notify, not only "
                      f"print; got: {buf.getvalue()!r}")
        self.assertIn(akashic_loop.NEEDS_UPDATE, buf.getvalue())

    def test_a_clean_dry_run_stays_silent(self):
        """The other half: a quiet fleet must not produce a daily banner, or
        the notification stops meaning anything."""
        repo = self.make_repo()
        self.write(repo, "f1.py", "one\n")
        self.commit(repo)
        self.write(repo, ".akashic/wiki/a.md",
                   "# A\n\nSources: [f1](../../f1.py)\n")
        self.catalog(repo, [self.page("a", files=["f1.py"], scope=["f1.py"])],
                     anchor=self.head(repo))

        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(buf):
            code = akashic_loop.process(str(repo), dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
