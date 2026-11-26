"""Expected mirror.list behavior for the future Python parser."""

import unittest
from pathlib import Path
import tempfile

from apt_mirror.config import parse_config
from apt_mirror.errors import ParseError


class ConfigParserTests(unittest.TestCase):
    def test_binary_source_and_cleanup_lines(self):
        config = parse_config(
            """# Mirror a small selection
set base_path '/srv/apt mirror'
set _contents 0
deb https://example.org/ubuntu noble main universe
deb-src https://example.org/ubuntu noble main
clean https://example.org/ubuntu/
skip-clean https://example.org/ubuntu/pool/keep
""",
            default_arch="amd64",
        )

        self.assertEqual(config.settings["base_path"], "/srv/apt mirror")
        self.assertEqual(config.settings["_contents"], "0")
        self.assertEqual(len(config.binary_sources), 1)
        binary = config.binary_sources[0]
        self.assertEqual(binary.architecture, "amd64")
        self.assertEqual(binary.uri, "https://example.org/ubuntu")
        self.assertEqual(binary.distribution, "noble")
        self.assertEqual(binary.components, ("main", "universe"))
        self.assertEqual(len(config.source_sources), 1)
        self.assertEqual(config.source_sources[0].components, ("main",))
        self.assertEqual(config.clean_urls, ("https://example.org/ubuntu/",))
        self.assertEqual(
            config.skip_clean_urls,
            ("https://example.org/ubuntu/pool/keep",),
        )

    def test_architecture_forms_and_flat_repository(self):
        config = parse_config(
            "deb-arm64 https://example.org/debian stable main\n"
            "deb [arch=amd64,arm64 signed-by=/keys/repo.gpg] "
            "https://example.org/debian stable main\n"
            "deb https://example.org/flat ./\n",
            default_arch="i386",
        )

        self.assertEqual(
            [source.architecture for source in config.binary_sources],
            ["arm64", "amd64", "arm64", "i386"],
        )
        self.assertEqual(config.binary_sources[-1].distribution, "./")
        self.assertEqual(config.binary_sources[-1].components, ())

    def test_blank_lines_whitespace_and_comments(self):
        config = parse_config(
            " \t# comment\n\n"
            "  deb\thttps://example.org/repo\tstable\tmain  \n",
            default_arch="amd64",
        )
        self.assertEqual(len(config.binary_sources), 1)
        self.assertEqual(config.binary_sources[0].components, ("main",))

    def test_invalid_lines_report_their_line_number(self):
        bad_lines = (
            "deb https://example.org/repo stable",
            "deb [arch=] https://example.org/repo stable main",
            "deb [arch=amd64 https://example.org/repo stable main",
            "deb gopher://example.org/repo stable main",
            "set base_path",
            "clean",
            "unknown https://example.org/repo stable main",
        )
        for bad_line in bad_lines:
            with self.subTest(bad_line=bad_line):
                with self.assertRaises(ParseError) as raised:
                    parse_config(
                        "# header\n" + bad_line + "\n",
                        default_arch="amd64",
                    )
                self.assertIn("line 2", str(raised.exception).lower())

    def test_rejects_unsafe_distribution_and_component_paths(self):
        for line in (
            "deb https://example.org/repo ../private main",
            "deb https://example.org/repo stable ../private",
            "deb https://example.org/repo stable /absolute",
        ):
            with self.subTest(line=line):
                with self.assertRaises(ParseError):
                    parse_config(line, default_arch="amd64")

    def test_include_dir_accepts_deb822_and_legacy_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            directory = root / "mirror.conf.d"
            directory.mkdir()
            (directory / "10-generic.conf").write_text(
                "set base_path /srv/mirror\nset defaultarch arm64\n")
            (directory / "20-repos.sources").write_text(
                "# Deb822 source\nTypes: deb deb-src\n"
                "URIs: https://example.org/a https://example.org/b\n"
                "Suites: stable testing\nComponents: main contrib\n"
                "Architectures: amd64 arm64\nSigned-By: /keys/archive.gpg\n\n"
                "Types: deb\nURIs: https://example.org/disabled\n"
                "Suites: stable\nComponents: main\nEnabled: no\n")
            (directory / "30-extra.list").write_text(
                "deb https://example.org/flat ./\n")
            (directory / "90-clean.conf").write_text(
                "clean https://example.org/a\n")
            (directory / "ignored.txt").write_text("invalid\n")
            config = parse_config("set include_dir=mirror.conf.d\n",
                                  default_arch="arm64",
                                  source_path=root / "mirror.list")
        self.assertEqual(config.settings["base_path"], "/srv/mirror")
        self.assertEqual(len(config.binary_sources), 9)
        self.assertEqual(len(config.source_sources), 4)
        self.assertEqual(config.binary_sources[-1].distribution, "./")
        self.assertEqual(config.binary_sources[0].signed_by,
                         "/keys/archive.gpg")
        self.assertEqual(config.clean_urls, ("https://example.org/a",))

    def test_include_cycle_and_bad_deb822_are_reported(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            directory = Path(temporary)
            (directory / "again.conf").write_text(
                f"set include_dir='{directory}'\n")
            with self.assertRaisesRegex(ParseError, "recursive include_dir"):
                parse_config(f"set include_dir='{directory}'\n")
            (directory / "again.conf").write_text(
                "Types: deb\nURIs: https://example.org/repo\n"
                "Suites: stable\n")
            with self.assertRaisesRegex(ParseError, "components required"):
                parse_config(f"set include_dir='{directory}'\n")


if __name__ == "__main__":
    unittest.main()
