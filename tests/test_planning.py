"""Offline URL and target planning checks."""

from pathlib import Path
import unittest

from apt_mirror.config import parse_config
from apt_mirror.errors import ParseError
from apt_mirror.metadata import parse_release
from apt_mirror.package_index import BinaryPackage
from apt_mirror.planning import plan_downloads


class PlanningTests(unittest.TestCase):
    def test_pinned_ubuntu_release_selection(self):
        fixture = (Path(__file__).parent / "fixtures" /
                   "ubuntu-noble-backports.Release")
        release = parse_release(fixture.read_text(encoding="utf-8"))
        config = parse_config(
            "deb https://archive.ubuntu.com/ubuntu "
            "noble-backports multiverse\n"
            "deb-src https://archive.ubuntu.com/ubuntu "
            "noble-backports multiverse\n")
        plan = plan_downloads(config, {
            ("https://archive.ubuntu.com/ubuntu", "noble-backports"):
            release,
        })
        paths = {download.local_path for download in plan.indexes}
        self.assertIn(
            "archive.ubuntu.com/ubuntu/dists/noble-backports/"
            "multiverse/binary-amd64/Packages.xz", paths)
        self.assertIn(
            "archive.ubuntu.com/ubuntu/dists/noble-backports/"
            "multiverse/source/Sources.xz", paths)
        self.assertNotIn(
            "archive.ubuntu.com/ubuntu/dists/noble-backports/"
            "main/binary-amd64/Packages.xz", paths)

    def test_standard_selection_deduplication_and_scope(self):
        config = parse_config(
            "deb [arch=amd64,amd64] https://example.org/debian stable main\n"
            "clean https://example.org/debian/\n"
            "skip-clean https://example.org/debian/pool/keep\n")
        release = parse_release(
            "Suite: stable\nArchitectures: amd64\nComponents: main\nSHA256:\n"
            f" {'a' * 64} 10 main/binary-amd64/Packages.gz\n"
            f" {'b' * 64} 8 main/binary-amd64/Packages.xz\n"
            f" {'c' * 64} 4 main/i18n/Translation-en.xz\n")
        package = BinaryPackage("example", "pool/main/e/example.deb", 3,
                                "d" * 64)
        plan = plan_downloads(config, {
            ("https://example.org/debian", "stable"): release,
        }, {("https://example.org/debian", "stable", "main", "amd64"):
            (package,)})
        self.assertEqual(len(plan.indexes), 3)
        self.assertEqual(len(plan.archives), 1)
        self.assertEqual(plan.indexes[0].url,
                         "https://example.org/debian/dists/stable/"
                         "main/binary-amd64/Packages.gz")
        self.assertEqual(plan.archives[0].local_path,
                         "example.org/debian/pool/main/e/example.deb")
        self.assertEqual(plan.clean_prefixes,
                         ("example.org/debian/",))
        self.assertEqual(plan.skip_clean_prefixes,
                         ("example.org/debian/pool/keep/",))

    def test_flat_repository(self):
        config = parse_config("deb https://example.org/flat ./\n")
        release = parse_release("SHA256:\n" +
                                f" {'a' * 64} 0 Packages.gz\n")
        plan = plan_downloads(config,
                              {("https://example.org/flat", "./"): release})
        self.assertEqual(plan.indexes[0].url,
                         "https://example.org/flat/Packages.gz")

    def test_missing_release_listed_index_uses_candidate_fallback(self):
        config = parse_config("deb https://example.org/repo stable main\n")
        release = parse_release("SHA256:\n" + f" {'a' * 64} 1 other\n")
        plan = plan_downloads(config,
                              {("https://example.org/repo", "stable"): release})
        self.assertEqual(plan.indexes, ())

    def test_contents_selection_includes_all_architecture(self):
        config = parse_config(
            "set _contents 1\n"
            "deb https://example.org/repo stable contrib\n")
        release = parse_release(
            "SHA256:\n"
            f" {'a' * 64} 1 contrib/binary-amd64/Packages.xz\n"
            f" {'b' * 64} 2 contrib/Contents-amd64.gz\n"
            f" {'c' * 64} 3 contrib/Contents-all.gz\n")
        plan = plan_downloads(
            config, {("https://example.org/repo", "stable"): release})
        self.assertEqual({item.local_path.rsplit("/", 1)[-1]
                          for item in plan.indexes},
                         {"Packages.xz", "Contents-amd64.gz",
                          "Contents-all.gz"})


if __name__ == "__main__":
    unittest.main()
