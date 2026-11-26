"""Parse legacy mirror.list files and Deb822 source stanzas."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex

from ._validation import relative_path, repository_url
from .errors import ParseError


@dataclass(frozen=True)
class MirrorSource:
    uri: str
    distribution: str
    components: tuple[str, ...]
    architecture: str | None = None
    signed_by: str | None = None


@dataclass(frozen=True)
class Config:
    settings: dict[str, str]
    binary_sources: tuple[MirrorSource, ...]
    source_sources: tuple[MirrorSource, ...]
    clean_urls: tuple[str, ...]
    skip_clean_urls: tuple[str, ...]


def _distribution(value):
    if value == "./":
        return value
    relative_path(value[:-1] if value.endswith("/") else value,
                  context="distribution")
    return value


def _components(values, distribution):
    result = tuple(relative_path(value, context="component")
                   for value in values)
    if not result and not distribution.endswith("/"):
        raise ParseError("components required for non-flat repository")
    return result


def _architectures(value):
    result = re.split(r"[\s,]+", value.strip())
    if not result or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", arch)
                         for arch in result):
        raise ParseError("invalid architecture")
    return result


def parse_config(text: str, default_arch: str = "amd64", *,
                 source_path: Path | None = None) -> Config:
    """Parse config text, expanding include_dir relative to source_path."""
    settings = {}
    binary = []
    sources = []
    clean = []
    skip_clean = []
    included = set()

    def add_source(kind, uri, distribution, components, arch=None,
                   signed_by=None):
        uri = repository_url(uri)
        distribution = _distribution(distribution)
        components = _components(components, distribution)
        if kind == "deb-src":
            sources.append(MirrorSource(uri, distribution, components,
                                        signed_by=signed_by))
        else:
            for architecture in _architectures(arch or default_arch):
                binary.append(MirrorSource(uri, distribution, components,
                                           architecture, signed_by))

    def parse_deb822(content, origin):
        fields = {}
        start = 1
        last = None

        def finish():
            if not fields:
                return
            try:
                enabled = fields.get("enabled", "yes").lower()
                if enabled not in ("yes", "no"):
                    raise ParseError("Enabled must be yes or no")
                if enabled == "no":
                    return
                kinds = fields["types"].split()
                uris = fields["uris"].split()
                suites = fields["suites"].split()
                components = fields.get("components", "").split()
                if not kinds or not uris or not suites:
                    raise ParseError("Types, URIs, and Suites are required")
                architectures = fields.get("architectures")
                if architectures is not None:
                    architectures = " ".join(_architectures(architectures))
                for kind in kinds:
                    if kind not in ("deb", "deb-src"):
                        raise ParseError(f"unsupported source type {kind!r}")
                    for uri in uris:
                        for suite in suites:
                            add_source(kind, uri, suite, components,
                                       architectures, fields.get("signed-by"))
            except (KeyError, ValueError, ParseError) as exc:
                if isinstance(exc, KeyError):
                    exc = ParseError(f"missing {exc.args[0]} field")
                raise ParseError(f"{origin}: line {start}: {exc}") from exc

        for number, raw in enumerate(content.splitlines(), 1):
            if not raw.strip():
                finish()
                fields = {}
                last = None
                start = number + 1
                continue
            if raw.lstrip().startswith("#"):
                continue
            if raw[0].isspace():
                if last is None:
                    raise ParseError(f"{origin}: line {number}: orphan continuation")
                fields[last] += "\n" + raw.strip()
                continue
            key, separator, value = raw.partition(":")
            if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key):
                raise ParseError(f"{origin}: line {number}: invalid Deb822 field")
            last = key.lower()
            if last in fields:
                raise ParseError(f"{origin}: line {number}: duplicate {key} field")
            fields[last] = value.strip()
        finish()

    def parse_legacy(content, origin, parent):
        for number, raw in enumerate(content.splitlines(), 1):
            try:
                lexer = shlex.shlex(raw, posix=True)
                lexer.whitespace_split = True
                lexer.commenters = "#"
                tokens = list(lexer)
                if not tokens:
                    continue
                kind = tokens[0]
                if kind == "set":
                    if len(tokens) == 2 and "=" in tokens[1]:
                        key, value = tokens[1].split("=", 1)
                    elif len(tokens) >= 3:
                        key, value = tokens[1], " ".join(tokens[2:])
                    else:
                        raise ParseError("invalid set directive")
                    if not re.fullmatch(r"[A-Za-z_][\w]*", key) or not value:
                        raise ParseError("invalid set directive")
                    settings[key] = value
                    if key == "include_dir":
                        directory = Path(value).expanduser()
                        if not directory.is_absolute():
                            directory = parent / directory
                        directory = directory.resolve()
                        if directory in included:
                            raise ParseError(f"recursive include_dir {directory}")
                        if not directory.is_dir():
                            raise ParseError(f"include_dir is not a directory: {directory}")
                        included.add(directory)
                        for path in sorted(directory.iterdir()):
                            if (path.is_file() and
                                    path.suffix in (".conf", ".list", ".sources")):
                                parse_document(path.read_text(encoding="utf-8"),
                                               str(path), path.parent)
                elif kind in ("clean", "skip-clean"):
                    if len(tokens) != 2:
                        raise ParseError("invalid cleanup directive")
                    repository_url(tokens[1])
                    (clean if kind == "clean" else skip_clean).append(tokens[1])
                elif kind in ("deb", "deb-src") or kind.startswith("deb-"):
                    arch = None
                    signed_by = None
                    if kind not in ("deb", "deb-src"):
                        arch = kind[4:]
                        _architectures(arch)
                    rest = tokens[1:]
                    if rest and rest[0].startswith("["):
                        end = next((i for i, token in enumerate(rest)
                                    if token.endswith("]")), None)
                        if end is None:
                            raise ParseError("unclosed options")
                        options = " ".join(rest[:end + 1])[1:-1].split()
                        rest = rest[end + 1:]
                        for option in options:
                            key, separator, value = option.partition("=")
                            if not separator or not value:
                                raise ParseError("invalid source option")
                            if key == "arch":
                                arch = value
                            elif key == "signed-by":
                                signed_by = value
                    if len(rest) < 2:
                        raise ParseError("incomplete source directive")
                    add_source("deb-src" if kind == "deb-src" else "deb",
                               rest[0], rest[1], rest[2:], arch, signed_by)
                else:
                    raise ParseError(f"unknown directive {kind!r}")
            except (OSError, ValueError, ParseError) as exc:
                label = f"{origin}: " if origin else ""
                raise ParseError(f"{label}line {number}: {exc}") from exc

    def parse_document(content, origin, parent):
        first = next((line.strip() for line in content.splitlines()
                      if line.strip() and not line.lstrip().startswith("#")), "")
        if re.match(r"^[A-Za-z][A-Za-z0-9-]*\s*:", first):
            parse_deb822(content, origin)
        else:
            parse_legacy(content, origin, parent)

    path = Path(source_path).absolute() if source_path is not None else None
    parse_document(text, str(path) if path else "",
                   path.parent if path else Path.cwd())
    return Config(settings, tuple(binary), tuple(sources), tuple(clean),
                  tuple(skip_clean))
