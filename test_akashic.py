#!/usr/bin/env python3
"""Self-checks for the akashic-record deterministic core.

One check per component (DESIGN.md section 9): scan filtering, the diff->stale
mapping (the test that proves the incremental mechanism), citation verification,
and the anchor invariant. Fixtures are throwaway git repos; stdlib unittest only.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import akashic  # noqa: E402


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
        self.assertEqual([s["id"] for s in report["stale"]], ["a"],
                         "never guess: unreachable anchor means regenerate all")


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
        self.assertTrue(any("cited file not found" in e for e in errors))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
