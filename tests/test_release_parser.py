"""Release, InRelease, translation and DEP-11 metadata tests."""

from pathlib import Path
import unittest

from apt_mirror.errors import ParseError
from apt_mirror.metadata import (
    discover_dep11_files,
    discover_translation_files,
    parse_release,
    parse_translation_index,
)


FIXTURES = Path(__file__).parent / "fixtures"


class ReleaseParserTests(unittest.TestCase):
    def test_real_ubuntu_release(self):
        release = parse_release(
            (FIXTURES / "ubuntu-noble-backports.Release").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(release.suite, "noble-backports")
        self.assertIn("amd64", release.architectures)
        self.assertIn("multiverse", release.components)
        packages = release.files["multiverse/binary-amd64/Packages.xz"]
        self.assertEqual(packages.size, 748)
        self.assertEqual(
            packages.sha256,
            "e224a0da236577f82600151bef134d7ddb3eebd0fed8f4574f65dc7e88e0bf2d",
        )
        self.assertEqual(
            release.files["multiverse/source/Sources.xz"].size,
            1036,
        )

    def test_real_kali_release(self):
        release = parse_release(
            (FIXTURES / "kali-rolling.Release").read_text(encoding="utf-8")
        )
        self.assertEqual(release.suite, "kali-rolling")
        self.assertIn("non-free-firmware", release.components)
        self.assertEqual(
            release.files["main/binary-amd64/Packages.gz"].size,
            21853003,
        )

    def test_inrelease_clearsign_wrapper(self):
        text = (
            "-----BEGIN PGP SIGNED MESSAGE-----\n"
            "Hash: SHA256\n\nSuite: test\nArchitectures: amd64\n"
            "Components: main\nSHA256:\n"
            f" {'a' * 64} 3 main/binary-amd64/Packages.xz\n"
            "-----BEGIN PGP SIGNATURE-----\n"
            "not-a-real-signature\n-----END PGP SIGNATURE-----\n"
        )
        release = parse_release(text)
        self.assertEqual(release.suite, "test")
        self.assertEqual(
            release.files["main/binary-amd64/Packages.xz"].size,
            3,
        )

    def test_translation_index_prefers_sha256_and_deduplicates(self):
        text = (
            f"SHA1:\n {'1' * 40} 12 Translation-en.xz\n"
            "SHA256:\n"
            f" {'a' * 64} 12 Translation-en.xz\n"
            f" {'b' * 64} 0 Translation-fr.bz2\n"
        )
        files = parse_translation_index(text)
        self.assertEqual(
            set(files), {"Translation-en.xz", "Translation-fr.bz2"})
        self.assertEqual(files["Translation-en.xz"].size, 12)
        self.assertEqual(files["Translation-fr.bz2"].size, 0)
        self.assertEqual(files["Translation-en.xz"].sha256, "a" * 64)

    def test_translation_and_dep11_discovery_is_scoped(self):
        release = parse_release(
            "Suite: test\nArchitectures: amd64 arm64\n"
            "Components: main universe\nSHA256:\n"
            f" {'a' * 64} 1 main/i18n/Translation-en.xz\n"
            f" {'b' * 64} 2 universe/i18n/Translation-fr.bz2\n"
            f" {'c' * 64} 3 main/dep11/Components-amd64.yml.xz\n"
            f" {'d' * 64} 4 main/dep11/Components-arm64.yml.xz\n"
            f" {'e' * 64} 5 main/dep11/icons-64x64.tar.gz\n"
        )
        self.assertEqual(
            set(discover_translation_files(release, "main")),
            {"main/i18n/Translation-en.xz"},
        )
        self.assertEqual(
            set(discover_dep11_files(release, "main", "amd64")),
            {
                "main/dep11/Components-amd64.yml.xz",
                "main/dep11/icons-64x64.tar.gz",
            },
        )

    def test_translation_index_rejects_bad_rows(self):
        rows = (
            " broken 12 Translation-en.xz",
            f" {'a' * 64} nope Translation-en.xz",
            f" {'a' * 64} 12 ../Translation-en.xz",
            f" {'a' * 64} 12 Translation-en.xz extra",
        )
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(ParseError):
                    parse_translation_index("SHA256:\n" + row + "\n")

    def test_translation_index_accepts_sha1(self):
        files = parse_translation_index(
            f"SHA1:\n {'1' * 40} 12 Translation-en.xz\n")
        self.assertIsNone(files["Translation-en.xz"].sha256)

    def test_rejects_invalid_checksum_rows_and_paths(self):
        rows = (
            " xyz 12 main/binary-amd64/Packages.xz",
            " " + "a" * 64 + " -1 main/binary-amd64/Packages.xz",
            " " + "a" * 64 + " 12 ../Packages.xz",
            " " + "a" * 64 + " 12 /absolute/Packages.xz",
            " " + "a" * 64 + " 12",
        )
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(ParseError):
                    parse_release("Suite: test\nSHA256:\n" + row + "\n")

    def test_accepts_missing_strong_checksum_and_rejects_conflicting_size(self):
        release = parse_release("Suite: test\nMD5Sum:\n " + "a" * 32 +
                                " 1 x\n")
        self.assertIsNone(release.files["x"].sha256)
        with self.assertRaises(ParseError):
            parse_release(
                "Suite: test\nMD5Sum:\n " + "a" * 32
                + " 1 x\nSHA256:\n " + "b" * 64 + " 2 x\n"
            )


if __name__ == "__main__":
    unittest.main()
