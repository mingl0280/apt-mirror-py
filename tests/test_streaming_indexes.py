"""Compressed indexes can be read without an uncompressed disk copy."""

import bz2
import gzip
import lzma
from pathlib import Path
import tempfile
import unittest

from apt_mirror.errors import ParseError
from apt_mirror.package_index import (
    iter_packages_file, iter_sources_file, parse_packages, parse_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


class StreamingIndexTests(unittest.TestCase):
    def test_saved_ubuntu_indexes_match_byte_parser(self):
        packages_path = (FIXTURES /
                         "ubuntu-noble-backports-multiverse.Packages.xz")
        sources_path = (FIXTURES /
                        "ubuntu-noble-backports-multiverse.Sources.xz")
        self.assertEqual(tuple(iter_packages_file(packages_path)),
                         parse_packages(packages_path.read_bytes(),
                                        filename="Packages.xz"))
        self.assertEqual(tuple(iter_sources_file(sources_path)),
                         parse_sources(sources_path.read_bytes(),
                                       filename="Sources.xz"))

    def test_plain_and_compressed_files(self):
        sample = (b"Package: one\nFilename: pool/one.deb\nSize: 0\n"
                  + b"SHA256: " + b"a" * 64 + b"\n")
        variants = (("", sample), (".gz", gzip.compress(sample)),
                    (".bz2", bz2.compress(sample)),
                    (".xz", lzma.compress(sample)))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            for suffix, content in variants:
                with self.subTest(suffix=suffix):
                    path = Path(directory) / ("Packages" + suffix)
                    path.write_bytes(content)
                    self.assertEqual(len(tuple(iter_packages_file(path))), 1)

    def test_corrupt_compressed_file(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "Packages.xz"
            path.write_bytes(b"broken")
            with self.assertRaises(ParseError):
                tuple(iter_packages_file(path))


if __name__ == "__main__":
    unittest.main()
