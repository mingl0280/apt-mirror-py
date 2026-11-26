"""Migration preserves the effective legacy source and cleanup set."""

from pathlib import Path
import tempfile
import unittest

from apt_mirror.config import parse_config
from apt_mirror.migrate_config import merge_suites, migrate, restore_stub


class MigrationTests(unittest.TestCase):
    def test_restore_stub_after_legacy_file_returns(self):
        old = ("set base_path /sources\n"
               "deb-amd64 https://example.org/repo stable main\n"
               "deb-amd64 https://example.org/repo stable main\n"
               "clean https://example.org/repo\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_bytes()
            config_path.write_text(old)
            preview = restore_stub(config_path, directory)
            self.assertTrue(preview["changed"])
            self.assertFalse(preview["applied"])
            result = restore_stub(config_path, directory, apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual(Path(result["backup"]).read_text(), old)
            self.assertIn("set include_dir=", config_path.read_text())
            self.assertEqual((directory / "20-repositories.sources").read_bytes(),
                             sources)
            self.assertFalse(restore_stub(config_path, directory,
                                          apply=True)["changed"])

    def test_restore_stub_when_root_file_is_missing(self):
        old = "deb-amd64 https://example.org/repo stable main\n"
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            config_path.unlink()
            result = restore_stub(config_path, directory, apply=True)
            self.assertTrue(result["applied"])
            self.assertNotIn("backup", result)
            self.assertEqual(len(parse_config(config_path.read_text(),
                                              source_path=config_path)
                                 .binary_sources), 1)

    def test_restore_stub_rejects_different_legacy_sources(self):
        old = "deb-amd64 https://example.org/repo stable main\n"
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            config_path.write_text(
                "deb-amd64 https://example.org/other stable main\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                restore_stub(config_path, directory, apply=True)

    def test_matching_binary_and_source_stanzas_share_types(self):
        old = ("deb-i386 https://example.org/kali rolling main contrib\n"
               "deb-amd64 https://example.org/kali rolling main contrib\n"
               "deb-src https://example.org/kali rolling main contrib\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_text()
            self.assertEqual(sources.count("Types: "), 1)
            self.assertIn("Types: deb deb-src\n", sources)
            self.assertIn("Architectures: i386 amd64", sources)
            parsed = parse_config(config_path.read_text(),
                                  source_path=config_path)
            self.assertEqual(len(parsed.binary_sources), 2)
            self.assertEqual(len(parsed.source_sources), 1)

    def test_partial_source_suites_do_not_expand(self):
        old = ("deb-amd64 https://example.org/kali rolling main\n"
               "deb-amd64 https://example.org/kali testing main\n"
               "deb-src https://example.org/kali rolling main\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_text()
            self.assertEqual(sources.count("Types: "), 2)
            self.assertNotIn("Types: deb deb-src", sources)

    def test_suites_with_matching_options_share_a_stanza(self):
        old = ("set base_path /sources\n"
               "deb-amd64 https://example.org/repo stable main\n"
               "deb-amd64 https://example.org/repo stable-updates main\n"
               "deb-amd64 https://example.org/repo stable main\n"
               "deb-arm64 https://example.org/repo stable main\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_text()
            self.assertIn("Suites: stable stable-updates\n"
                          "Components: main\nArchitectures: amd64",
                          sources)
            self.assertIn("Suites: stable\n"
                          "Components: main\nArchitectures: arm64", sources)
            self.assertEqual(sources.count("Types: deb\n"), 2)
            self.assertEqual(len(parse_config(config_path.read_text(),
                                              source_path=config_path)
                                 .binary_sources), 3)

    def test_bookworm_and_bullseye_do_not_add_i386_bookworm(self):
        old = ("deb-i386 http://deb.debian.org/debian bullseye main\n"
               "deb-amd64 http://deb.debian.org/debian bullseye main\n"
               "deb-i386 http://deb.debian.org/debian bullseye-updates main\n"
               "deb-amd64 http://deb.debian.org/debian bullseye-updates main\n"
               "deb-amd64 http://deb.debian.org/debian bookworm main\n"
               "deb-amd64 http://deb.debian.org/debian bookworm-updates main\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_text()
            self.assertEqual(sources.count("Types: deb\n"), 2)
            self.assertIn("Suites: bullseye bullseye-updates "
                          "bookworm bookworm-updates\n"
                          "Components: main\nArchitectures: amd64", sources)
            self.assertIn("Suites: bullseye bullseye-updates\n"
                          "Components: main\nArchitectures: i386", sources)
            parsed = parse_config(config_path.read_text(),
                                  source_path=config_path)
            self.assertEqual(len(set(parsed.binary_sources)), 6)

    def test_full_suite_architecture_rectangle_becomes_one_stanza(self):
        old = ("deb-amd64 https://example.org/repo stable main\n"
               "deb-i386 https://example.org/repo stable main\n"
               "deb-amd64 https://example.org/repo stable-updates main\n"
               "deb-i386 https://example.org/repo stable-updates main\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            migrate(config_path, directory, apply=True)
            sources = (directory / "20-repositories.sources").read_text()
            self.assertEqual(sources.count("Types: deb\n"), 1)
            self.assertIn("Suites: stable stable-updates\n"
                          "Components: main\nArchitectures: amd64 i386",
                          sources)

    def test_regroup_existing_migration_keeps_source_set(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            directory = root / "mirror.conf.d"
            directory.mkdir()
            config_path = root / "mirror.list"
            config_path.write_text(f"set include_dir='{directory}'\n")
            source_file = directory / "20-repositories.sources"
            original = ("Types: deb\nURIs: https://example.org/repo\n"
                        "Suites: stable\nComponents: main\n"
                        "Architectures: amd64\n\n"
                        "Types: deb\nURIs: https://example.org/repo\n"
                        "Suites: stable-updates\nComponents: main\n"
                        "Architectures: amd64\n")
            source_file.write_text(original)
            preview = merge_suites(config_path, directory)
            self.assertTrue(preview["changed"])
            self.assertFalse(preview["applied"])
            result = merge_suites(config_path, directory, apply=True)
            self.assertEqual(result["stanzas"], 1)
            self.assertEqual(Path(result["backup"]).read_text(), original)
            self.assertIn("Suites: stable stable-updates",
                          source_file.read_text())

    def test_preview_and_apply_preserve_effective_configuration(self):
        old = ("set base_path /sources\n"
               "set defaultarch amd64\n"
               "set nthreads 64\n"
               "deb-amd64 https://example.org/repo stable main contrib\n"
               "deb-src https://example.org/repo stable main\n"
               "deb https://example.org/flat ./\n"
               "clean https://example.org/repo\n"
               "skip-clean https://example.org/repo/pool/keep\n")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            config_path = root / "mirror.list"
            directory = root / "mirror.conf.d"
            config_path.write_text(old)
            preview = migrate(config_path, directory)
            self.assertFalse(preview["applied"])
            self.assertFalse(directory.exists())
            applied = migrate(config_path, directory, apply=True)
            self.assertTrue(applied["applied"])
            self.assertEqual(Path(applied["backup"]).read_text(), old)
            self.assertIn("set include_dir=", config_path.read_text())
            generic = (directory / "10-generic.conf").read_text()
            self.assertIn("set nthreads 64", generic)
            self.assertIn("# set limit_rate", generic)
            self.assertIn("# set proxy_password", generic)
            self.assertEqual(len((directory / "20-repositories.sources")
                                 .read_text().split("Types: deb\n")), 3)
            before = parse_config(old)
            after = parse_config(config_path.read_text(),
                                 source_path=config_path)
            self.assertEqual(before.binary_sources, after.binary_sources)
            self.assertEqual(before.source_sources, after.source_sources)
            self.assertEqual(before.clean_urls, after.clean_urls)
            self.assertEqual(before.skip_clean_urls, after.skip_clean_urls)
            self.assertEqual(before.settings,
                             {key: value for key, value in after.settings.items()
                              if key != "include_dir"})


if __name__ == "__main__":
    unittest.main()
