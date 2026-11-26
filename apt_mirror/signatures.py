"""Verify APT Release signatures against explicit trusted keyrings."""

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile


class SignatureError(RuntimeError):
    """Release metadata did not pass the configured trust check."""


@dataclass(frozen=True)
class VerifiedRelease:
    payload: bytes
    signature_files: dict[str, bytes]
    fingerprint: str | None


def verify_release(*, inrelease=None, release=None, signature=None,
                   keyrings=(), work_dir=None, timeout=30):
    """Run gpgv without shell expansion or implicit system keyrings.

    Exactly one signed format is required. The returned payload is suitable
    for ``parse_release`` only after this function succeeds.
    """
    keys = tuple(Path(path).resolve() for path in keyrings)
    if not keys or any(not path.is_file() for path in keys):
        raise SignatureError("explicit existing keyring files are required")
    if inrelease is not None:
        if release is not None or signature is not None:
            raise SignatureError(
                "choose InRelease or Release plus Release.gpg")
        payload = inrelease
        files = {"InRelease": inrelease}
    elif release is not None and signature is not None:
        payload = release
        files = {"Release": release, "Release.gpg": signature}
    else:
        raise SignatureError("missing signed Release metadata")
    if not payload or any(not value for value in files.values()):
        raise SignatureError("empty Release or signature data")
    base = Path(work_dir) if work_dir is not None else None
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as directory:
        directory = Path(directory)
        for name, content in files.items():
            (directory / name).write_bytes(content)
        command = ["gpgv", "--status-fd=1"]
        for key in keys:
            command.extend(("--keyring", str(key)))
        if inrelease is not None:
            command.append(str(directory / "InRelease"))
        else:
            command.extend((str(directory / "Release.gpg"),
                            str(directory / "Release")))
        try:
            result = subprocess.run(command, capture_output=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SignatureError(
                f"gpgv could not verify Release: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SignatureError(f"Release signature rejected: {detail}")
    match = re.search(rb"\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]+)", result.stdout)
    if match is None:
        raise SignatureError("gpgv succeeded without a VALIDSIG status")
    return VerifiedRelease(payload, files, match.group(1).decode("ascii"))
