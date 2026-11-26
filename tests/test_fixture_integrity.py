"""Keep downloaded repository samples stable and internally consistent."""

import hashlib
from pathlib import Path
import unittest


FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_SHA256 = {
    "ubuntu-noble-backports.Release":
        "3dc1035de8093c2d64ae5e779edfd2c8d73aa1eaeecc816d81190402871f32a2",
    "ubuntu-noble-backports-multiverse.Packages.xz":
        "e224a0da236577f82600151bef134d7ddb3eebd0fed8f4574f65dc7e88e0bf2d",
    "ubuntu-noble-backports-multiverse.Sources.xz":
        "c036039db40bf6642fe47e43e0469808e82616f3bc28974a13881f04af252bfd",
    "kali-rolling.Release":
        "753180ae3959b550153b26671115a9887e15741618a58e3f2f3e809c0ecd6d13",
}


class FixtureIntegrityTests(unittest.TestCase):
    def test_fixture_hashes_are_pinned(self):
        for name, expected in EXPECTED_SHA256.items():
            with self.subTest(name=name):
                actual = hashlib.sha256(
                    (FIXTURES / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)

    def test_ubuntu_index_hashes_match_release(self):
        release = (FIXTURES / "ubuntu-noble-backports.Release").read_text(
            encoding="utf-8"
        )
        checksum_rows = release.split("SHA256:\n", 1)[1].splitlines()
        checksums = {}
        for row in checksum_rows:
            parts = row.split()
            if len(parts) == 3 and len(parts[0]) == 64:
                checksums[parts[2]] = (parts[0], int(parts[1]))
        for path, name in (
            (
                "multiverse/binary-amd64/Packages.xz",
                "ubuntu-noble-backports-multiverse.Packages.xz",
            ),
            (
                "multiverse/source/Sources.xz",
                "ubuntu-noble-backports-multiverse.Sources.xz",
            ),
        ):
            with self.subTest(path=path):
                data = (FIXTURES / name).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                self.assertEqual(checksums[path], (digest, len(data)))


if __name__ == "__main__":
    unittest.main()
