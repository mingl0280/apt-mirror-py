"""Source index parsing and checksum reconciliation."""

import bz2
import gzip
import lzma
from pathlib import Path
import unittest

from apt_mirror.errors import ParseError
from apt_mirror.package_index import parse_sources


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = (
    "Package: example\nVersion: 1.0\n"
    "Directory: pool/main/e/example\nFiles:\n"
    f" {'1' * 32} 4 example_1.0.dsc\n"
    f" {'2' * 32} 8 example_1.0.orig.tar.xz\n"
    "Checksums-Sha256:\n"
    f" {'a' * 64} 4 example_1.0.dsc\n"
    f" {'b' * 64} 8 example_1.0.orig.tar.xz\n"
).encode("ascii")


class SourceIndexParserTests(unittest.TestCase):
    def test_plain_and_all_supported_compressions(self):
        variants = (
            ("Sources", SAMPLE),
            ("Sources.gz", gzip.compress(SAMPLE)),
            ("Sources.bz2", bz2.compress(SAMPLE)),
            ("Sources.xz", lzma.compress(SAMPLE)),
        )
        for filename, data in variants:
            with self.subTest(filename=filename):
                records = parse_sources(data, filename=filename)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].package, "example")
                self.assertEqual(records[0].directory, "pool/main/e/example")
                self.assertEqual(len(records[0].files), 2)
                self.assertEqual(
                    records[0].files[0].path,
                    "pool/main/e/example/example_1.0.dsc",
                )
                self.assertEqual(records[0].files[0].size, 4)
                self.assertEqual(records[0].files[0].sha256, "a" * 64)

    def test_real_ubuntu_backports_sources(self):
        data = (
            FIXTURES / "ubuntu-noble-backports-multiverse.Sources.xz"
        ).read_bytes()
        records = parse_sources(data, filename="Sources.xz")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].package, "linux-entra-sso")
        self.assertEqual(len(records[0].files), 3)
        self.assertEqual(
            records[0].files[0].path,
            "pool/multiverse/l/linux-entra-sso/"
            "linux-entra-sso_1.8.0.orig.tar.gz",
        )
        self.assertEqual(records[0].files[0].size, 48229)
        self.assertEqual(
            records[0].files[0].sha256,
            "9ebc74628ef6ec4885e65ec376cc9c129e292cd7bdcce2c5fca86d4c05ec2bd1",
        )

    def test_rejects_malformed_or_conflicting_file_lists(self):
        bad_records = (
            SAMPLE.replace(b"Directory: pool/main/e/example\n", b""),
            SAMPLE.replace(
                b" 11111111111111111111111111111111 4", b" bad row"),
            SAMPLE.replace(b" 11111111111111111111111111111111 4",
                           b" " + b"1" * 32 + b" -1"),
            SAMPLE.replace(b"example_1.0.dsc", b"../outside.dsc"),
            SAMPLE.replace(
                b" " + b"a" * 64 + b" 4 example_1.0.dsc\n",
                b"",
            ),
            SAMPLE.replace(
                b" " + b"a" * 64 + b" 4 example_1.0.dsc",
                b" " + b"a" * 64 + b" 5 example_1.0.dsc",
            ),
        )
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(ParseError):
                    parse_sources(record, filename="Sources")

    def test_rejects_corrupt_compression_and_accepts_missing_sha256(self):
        with self.assertRaises(ParseError):
            parse_sources(b"not gzip", filename="Sources.gz")
        records = parse_sources(
            SAMPLE.split(b"Checksums-Sha256:", 1)[0], filename="Sources")
        self.assertIsNone(records[0].files[0].sha256)


if __name__ == "__main__":
    unittest.main()
