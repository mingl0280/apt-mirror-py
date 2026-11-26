"""Signature verification requires explicit keys and a valid gpgv result."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from apt_mirror.signatures import SignatureError, verify_release


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "trusted.gpg"
        self.key.write_bytes(b"test keyring")

    def test_inrelease_invokes_gpgv_with_only_given_keyring(self):
        status = b"[GNUPG:] VALIDSIG " + b"A" * 40 + b" 0 0\n"
        with patch("apt_mirror.signatures.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                (), 0, status, b"")
            result = verify_release(
                inrelease=b"signed metadata", keyrings=(self.key,),
                work_dir=self.root)
        self.assertEqual(result.fingerprint, "A" * 40)
        self.assertEqual(result.signature_files,
                         {"InRelease": b"signed metadata"})
        command = run.call_args.args[0]
        self.assertEqual(command[:4],
                         ["gpgv", "--status-fd=1", "--keyring",
                          str(self.key)])

    def test_rejects_missing_keyring_and_bad_signature(self):
        with self.assertRaises(SignatureError):
            verify_release(inrelease=b"data")
        with patch("apt_mirror.signatures.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                (), 1, b"", b"bad signature")
            with self.assertRaises(SignatureError):
                verify_release(inrelease=b"data", keyrings=(self.key,),
                               work_dir=self.root)


if __name__ == "__main__":
    unittest.main()
