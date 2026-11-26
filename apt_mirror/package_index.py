"""Parse compressed Packages and Sources indexes into archive records."""

import bz2
from dataclasses import dataclass
import gzip
import lzma
from pathlib import Path

from ._validation import checksum, relative_path, size_value
from .errors import ParseError


@dataclass(frozen=True)
class BinaryPackage:
    package: str
    filename: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class SourcePackage:
    package: str
    directory: str
    files: tuple[SourceFile, ...]


def _decode(data, filename):
    try:
        if filename.endswith(".gz"):
            data = gzip.decompress(data)
        elif filename.endswith(".bz2"):
            data = bz2.decompress(data)
        elif filename.endswith(".xz"):
            data = lzma.decompress(data)
        return data.decode("utf-8")
    except (OSError, EOFError, ValueError, lzma.LZMAError,
            UnicodeError) as exc:
        raise ParseError(f"cannot decode {filename}: {exc}") from exc


def _stanzas(text):
    fields = {}
    current = None
    for number, line in enumerate(text, 1):
        if not line.strip():
            if fields:
                yield fields
                fields = {}
                current = None
            continue
        if line[0] in " \t":
            if current is None:
                raise ParseError(f"line {number}: unexpected continuation")
            fields[current] += "\n" + line.strip()
        else:
            key, separator, value = line.partition(":")
            if not separator or not key or key in fields:
                raise ParseError(
                    f"line {number}: malformed or duplicate field")
            fields[key] = value.strip()
            current = key
    if fields:
        yield fields


def _required(fields, key):
    value = fields.get(key)
    if not value:
        raise ParseError(f"missing {key}")
    return value


def parse_packages(data: bytes, *, filename="Packages"):
    return tuple(_package_records(_stanzas(
        _decode(data, filename).splitlines())))


def _package_records(stanzas):
    for fields in stanzas:
        yield BinaryPackage(
            _required(fields, "Package"),
            relative_path(_required(fields, "Filename")),
            size_value(_required(fields, "Size")),
            (checksum(fields["SHA256"], 64, context="SHA256")
             if fields.get("SHA256") else None))


def _file_rows(value, length, label):
    rows = {}
    for row in value.splitlines():
        if not row.strip():
            continue
        parts = row.split()
        if len(parts) != 3:
            raise ParseError(f"invalid {label} row: {row!r}")
        digest = checksum(parts[0], length, context=label)
        size = size_value(parts[1])
        path = relative_path(parts[2], context="source filename")
        if "/" in path:
            raise ParseError(f"source filename must be a basename: {path}")
        if path in rows and rows[path] != (size, digest):
            raise ParseError(f"conflicting {label} entry: {path}")
        rows[path] = (size, digest)
    return rows


def parse_sources(data: bytes, *, filename="Sources"):
    return tuple(_source_records(_stanzas(
        _decode(data, filename).splitlines())))


def _source_records(stanzas):
    for fields in stanzas:
        package = _required(fields, "Package")
        directory = relative_path(_required(fields, "Directory"))
        md5 = _file_rows(_required(fields, "Files"), 32, "Files")
        sha = (_file_rows(fields["Checksums-Sha256"], 64,
                          "Checksums-Sha256")
               if fields.get("Checksums-Sha256") else {})
        if not md5 or (sha and md5.keys() != sha.keys()):
            raise ParseError(f"conflicting source file lists for {package}")
        files = []
        for name, (size, _) in md5.items():
            if sha and sha[name][0] != size:
                raise ParseError(f"conflicting size for {name}")
            files.append(SourceFile(f"{directory}/{name}", size,
                                    sha[name][1] if sha else None))
        yield SourcePackage(package, directory, tuple(files))


def _stream_stanzas(path):
    """Read one stanza at a time directly from a compressed on-disk index."""
    path = Path(path)
    opener = (gzip.open if path.suffix == ".gz" else
              bz2.open if path.suffix == ".bz2" else
              lzma.open if path.suffix == ".xz" else open)
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            yield from _stanzas(stream)
    except (OSError, EOFError, ValueError, lzma.LZMAError,
            UnicodeError) as exc:
        raise ParseError(f"cannot decode {path}: {exc}") from exc


def iter_packages_file(path):
    """Yield binary records from a file without materializing the index."""
    yield from _package_records(_stream_stanzas(path))


def iter_sources_file(path):
    """Yield source records from a file without materializing the index."""
    yield from _source_records(_stream_stanzas(path))
