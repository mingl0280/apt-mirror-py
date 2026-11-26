"""Parse Release metadata and identify optional metadata paths."""

from dataclasses import dataclass
import re

from ._validation import checksum, relative_path, size_value
from .errors import ParseError


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class Release:
    fields: dict[str, str]
    files: dict[str, FileEntry]

    @property
    def suite(self):
        return self.fields.get("Suite", "")

    @property
    def architectures(self):
        return tuple(self.fields.get("Architectures", "").split())

    @property
    def components(self):
        return tuple(self.fields.get("Components", "").split())


_HASH_LENGTHS = {"MD5Sum": 32, "SHA1": 40, "SHA256": 64, "SHA512": 128}


def _unwrapped(text):
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        return text
    lines = text.splitlines()
    try:
        start = lines.index("") + 1
        end = lines.index("-----BEGIN PGP SIGNATURE-----", start)
    except ValueError as exc:
        raise ParseError("malformed InRelease clearsign wrapper") from exc
    return "\n".join(line[2:] if line.startswith("- ") else line
                     for line in lines[start:end])


def _fields(text):
    fields = {}
    current = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line[0] in " \t":
            if current is None:
                raise ParseError(f"line {number}: unexpected continuation")
            fields[current] += "\n" + line.strip()
        else:
            key, separator, value = line.partition(":")
            if (not separator or
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key)):
                raise ParseError(f"line {number}: malformed field")
            if key in fields:
                raise ParseError(f"line {number}: duplicate field {key}")
            fields[key] = value.strip()
            current = key
    return fields


def _hash_rows(value, name):
    rows = {}
    for row in value.splitlines():
        if not row.strip():
            continue
        parts = row.split()
        if len(parts) != 3:
            raise ParseError(f"invalid {name} row: {row!r}")
        digest = checksum(parts[0], _HASH_LENGTHS[name], context=name)
        size = size_value(parts[1])
        path = relative_path(parts[2])
        if path in rows and rows[path] != (size, digest):
            raise ParseError(f"conflicting {name} entry: {path}")
        rows[path] = (size, digest)
    return rows


def _files(fields):
    tables = {name: _hash_rows(fields[name], name)
              for name in _HASH_LENGTHS if name in fields}
    if not tables:
        return {}
    result = {}
    base = next(tables[name] for name in
                ("SHA256", "SHA512", "SHA1", "MD5Sum") if name in tables)
    for path, (size, _) in base.items():
        for name, rows in tables.items():
            if path in rows and rows[path][0] != size:
                raise ParseError(f"conflicting {name} size for {path}")
        result[path] = FileEntry(path, size,
                                 tables.get("SHA256", {}).get(path, (None, None))[1])
    return result


def parse_release(text: str) -> Release:
    """Parse Release or InRelease; signature verification is separate."""
    fields = _fields(_unwrapped(text))
    return Release(fields, _files(fields))


def parse_translation_index(text: str) -> dict[str, FileEntry]:
    """Parse a Translation Index checksum table."""
    return _files(_fields(text))


def discover_translation_files(release: Release, component: str):
    relative_path(component, context="component")
    prefix = f"{component}/i18n/Translation-"
    return tuple(path for path in release.files if path.startswith(prefix)
                 and path.endswith((".gz", ".bz2", ".xz", ".lzma")))


def discover_dep11_files(release: Release, component: str,
                         architecture: str):
    relative_path(component, context="component")
    relative_path(architecture, context="architecture")
    prefix = f"{component}/dep11/"
    return tuple(path for path in release.files if path.startswith(prefix)
                 and (re.fullmatch(rf"Components-{re.escape(architecture)}"
                                   r"\.yml\.(?:gz|bz2|xz)",
                                   path[len(prefix):])
                      or re.fullmatch(r"icons-[^/]+\.tar\.(?:gz|bz2|xz)",
                                      path[len(prefix):])))
