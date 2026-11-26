"""Validation shared by the side-effect-free parsers."""

import re
from urllib.parse import urlsplit

from .errors import ParseError


def relative_path(path, *, context="path"):
    if (not path or path.startswith("/") or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or any(char.isspace() or ord(char) == 127 for char in path)
            or "%" in path or "?" in path or "#" in path
            or re.match(r"^[A-Za-z]:", path)):
        raise ParseError(f"unsafe {context}: {path!r}")
    return path


def size_value(value, *, context="size"):
    if not re.fullmatch(r"[0-9]+", value):
        raise ParseError(f"invalid {context}: {value!r}")
    return int(value)


def checksum(value, length, *, context="checksum"):
    if not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise ParseError(f"invalid {context}: {value!r}")
    return value.lower()


def repository_url(value):
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ParseError(f"invalid repository URL: {value!r}") from exc
    if (url.scheme not in ("http", "https") or not url.hostname
            or url.query or url.fragment
            or any(ord(char) < 33 for char in value)
            or "\\" in value or port == 0):
        raise ParseError(f"invalid repository URL: {value!r}")
    for part in url.path.split("/"):
        if part in (".", "..") or re.search(r"%2e|%2f|%5c", part, re.I):
            raise ParseError(f"unsafe repository URL: {value!r}")
    return value
