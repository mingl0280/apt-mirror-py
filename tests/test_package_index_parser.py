"""Binary Packages parsing, including real and malformed compressed data."""

import bz2
import gzip
import lzma
from pathlib import Path
import unittest

from apt_mirror.errors import ParseError
from apt_mirror.package_index import parse_packages


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = b"""Package: first
Version: 1.0
Filename: pool/main/f/first/first_1.0_amd64.deb
Size: 42
SHA256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Description: First line
 second line

Package: second
Filename: pool/main/s/second/second_2.0_all.deb
Size: 0
SHA256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
"""


class PackageIndexParserTests(unittest.TestCase):
    def test_plain_and_all_supported_compressions(self):
        variants = (
            ("Packages", SAMPLE),
            ("Packages.gz", gzip.compress(SAMPLE)),
            ("Packages.bz2", bz2.compress(SAMPLE)),
            ("Packages.xz", lzma.compress(SAMPLE)),
        )
        for filename, data in variants:
            with self.subTest(filename=filename):
                records = parse_packages(data, filename=filename)
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0].package, "first")
                self.assertEqual(
                    records[0].filename,
                    "pool/main/f/first/first_1.0_amd64.deb",
                )
                self.assertEqual(records[0].size, 42)
                self.assertEqual(records[0].sha256, "a" * 64)
                self.assertEqual(records[1].size, 0)

    def test_real_ubuntu_backports_packages(self):
        data = (
            FIXTURES / "ubuntu-noble-backports-multiverse.Packages.xz"
        ).read_bytes()
        records = parse_packages(data, filename="Packages.xz")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].package, "linux-entra-sso")
        self.assertEqual(
            records[0].filename,
            "pool/multiverse/l/linux-entra-sso/"
            "linux-entra-sso_1.8.0-1~bpo24.04.2_all.deb",
        )
        self.assertEqual(records[0].size, 7088)
        self.assertEqual(
            records[0].sha256,
            "26a3e85a3f4e19caf8ce5b049c143042062446eba8079cce7c9ce00f7f9b9eb2",
        )

    def test_empty_index_is_valid(self):
        self.assertEqual(parse_packages(b"", filename="Packages"), ())

    def test_missing_fields_and_invalid_values(self):
        bad_records = (
            SAMPLE.split(b"\n\n", 1)[0].replace(b"Size: 42\n", b""),
            SAMPLE.split(b"\n\n", 1)[0].replace(b"Size: 42", b"Size: -1"),
            SAMPLE.split(b"\n\n", 1)[0].replace(b"Size: 42", b"Size: huge"),
            SAMPLE.split(b"\n\n", 1)[0].replace(b"SHA256: " + b"a" * 64, b""),
            SAMPLE.split(b"\n\n", 1)[0].replace(b"a" * 64, b"not-a-hash"),
            SAMPLE.split(b"\n\n", 1)[0].replace(
                b"pool/main/f/first/first_1.0_amd64.deb",
                b"../outside.deb",
            ),
        )
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(ParseError):
                    parse_packages(record, filename="Packages")

    def test_corrupt_compressed_data_and_invalid_utf8(self):
        with self.assertRaises(ParseError):
            parse_packages(b"not xz", filename="Packages.xz")
        with self.assertRaises(ParseError):
            parse_packages(b"Package: \xff\n", filename="Packages")


if __name__ == "__main__":
    unittest.main()
