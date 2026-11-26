"""Build inspectable download plans without network or filesystem access."""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from ._validation import relative_path, repository_url
from .config import Config
from .errors import ParseError
from .metadata import Release, discover_dep11_files, discover_translation_files
from .package_index import BinaryPackage, SourcePackage


@dataclass(frozen=True)
class Download:
    url: str
    local_path: str
    size: int
    sha256: str | None
    kind: str


@dataclass(frozen=True)
class DownloadPlan:
    indexes: tuple[Download, ...]
    archives: tuple[Download, ...]
    clean_prefixes: tuple[str, ...]
    skip_clean_prefixes: tuple[str, ...]


_INDEX_EXTENSIONS = (".gz", ".xz", ".bz2")


def _repository_prefix(uri):
    repository_url(uri)
    parsed = urlsplit(uri)
    # apt-mirror strips scheme, credentials, and port from local paths.
    host = parsed.hostname.lower()
    prefix = f"{host}{parsed.path.rstrip('/')}"
    for component in prefix.split("/"):
        relative_path(component, context="repository path")
    return prefix


def _download(uri, path, entry, kind):
    path = relative_path(path)
    parsed = urlsplit(repository_url(uri))
    url = urlunsplit((parsed.scheme, parsed.netloc,
                      parsed.path.rstrip("/") + "/" + path, "", ""))
    return Download(url, _repository_prefix(uri) + "/" + path,
                    entry.size, entry.sha256, kind)


def _release_path(distribution, path):
    relative_path(path)
    if distribution == "./":
        return path
    if distribution.endswith("/"):
        return distribution + path
    return f"dists/{distribution}/{path}"


def _select(release, base):
    for extension in _INDEX_EXTENSIONS:
        path = base + extension
        if path in release.files:
            return path
    raise ParseError(f"required index missing from Release: {base}")


def _variants(release, base):
    return tuple(base + extension for extension in _INDEX_EXTENSIONS
                 if base + extension in release.files)


def _optional_variant(release, base):
    for extension in _INDEX_EXTENSIONS:
        path = base + extension
        if path in release.files:
            return path
    return None


def _append_unique(items, seen, item):
    previous = seen.get(item.local_path)
    if previous is not None:
        if (previous.size, previous.sha256, previous.url) != (
                item.size, item.sha256, item.url):
            raise ParseError(f"conflicting download target: {item.local_path}")
        return
    seen[item.local_path] = item
    items.append(item)


def _clean_prefix(uri):
    return _repository_prefix(uri) + "/"


def plan_downloads(
        config: Config,
        releases: dict[tuple[str, str], Release],
        packages: dict[tuple[str, str, str, str],
                       tuple[BinaryPackage, ...]] | None = None,
        sources: dict[tuple[str, str, str],
                      tuple[SourcePackage, ...]] | None = None
) -> DownloadPlan:
    """Plan files from trusted Release and parsed archive indexes.

    ``releases`` is keyed by (repository URL, distribution). Package indexes
    are keyed by (URL, distribution, component, architecture); source indexes
    omit architecture. The mirror does not enforce Release signatures.
    """
    packages = packages or {}
    sources = sources or {}
    indexes = []
    archives = []
    index_seen = {}
    archive_seen = {}
    entries = [(source, "binary") for source in config.binary_sources]
    entries += [(source, "source") for source in config.source_sources]
    for source, kind in entries:
        try:
            release = releases[(source.uri, source.distribution)]
        except KeyError as exc:
            raise ParseError(f"missing Release for {source.uri} "
                             f"{source.distribution}") from exc
        for component in source.components:
            if kind == "binary":
                base = f"{component}/binary-{source.architecture}/Packages"
                key = (source.uri, source.distribution, component,
                       source.architecture)
            else:
                base = f"{component}/source/Sources"
                key = (source.uri, source.distribution, component)
            for chosen in _variants(release, base):
                full = _release_path(source.distribution, chosen)
                _append_unique(indexes, index_seen, _download(
                    source.uri, full, release.files[chosen], "index"))
            records = (packages.get(key, ()) if kind == "binary"
                       else sources.get(key, ()))
            for record in records:
                files = (record,) if kind == "binary" else record.files
                for file in files:
                    path = file.filename if kind == "binary" else file.path
                    _append_unique(archives, archive_seen, _download(
                        source.uri, path, file, "archive"))
            if kind == "binary":
                optional = discover_translation_files(release, component)
                optional += discover_dep11_files(release, component,
                                                 source.architecture)
                extra = (
                    f"{component}/binary-{source.architecture}/Release",
                    f"{component}/i18n/Index",
                    f"{component}/cnf/Commands-{source.architecture}.xz",
                )
                optional += tuple(path for path in extra
                                  if path in release.files)
                if config.settings.get("_contents", "1") == "1":
                    contents_bases = (
                        f"Contents-{source.architecture}",
                        f"{component}/Contents-{source.architecture}",
                        "Contents-all", f"{component}/Contents-all")
                    for base in contents_bases:
                        path = _optional_variant(release, base)
                        if path is not None:
                            optional += (path,)
                for path in optional:
                    _append_unique(indexes, index_seen, _download(
                        source.uri, _release_path(source.distribution, path),
                        release.files[path], "optional-index"))
            elif f"{component}/source/Release" in release.files:
                path = f"{component}/source/Release"
                _append_unique(indexes, index_seen, _download(
                    source.uri, _release_path(source.distribution, path),
                    release.files[path], "optional-index"))
            if (kind == "source" and
                    config.settings.get("_contents", "1") == "1"):
                path = _optional_variant(
                    release, f"{component}/Contents-source")
                if path is not None:
                    _append_unique(indexes, index_seen, _download(
                        source.uri,
                        _release_path(source.distribution, path),
                        release.files[path], "optional-index"))
        if not source.components:
            base = "Packages" if kind == "binary" else "Sources"
            for chosen in _variants(release, base):
                _append_unique(indexes, index_seen, _download(
                    source.uri, _release_path(source.distribution, chosen),
                    release.files[chosen], "index"))
            key = ((source.uri, source.distribution, "", source.architecture)
                   if kind == "binary" else
                   (source.uri, source.distribution, ""))
            records = (packages.get(key, ()) if kind == "binary"
                       else sources.get(key, ()))
            for record in records:
                files = (record,) if kind == "binary" else record.files
                for file in files:
                    path = file.filename if kind == "binary" else file.path
                    _append_unique(archives, archive_seen, _download(
                        source.uri, path, file, "archive"))
    return DownloadPlan(tuple(indexes), tuple(archives),
                        tuple(dict.fromkeys(_clean_prefix(url)
                                            for url in config.clean_urls)),
                        tuple(dict.fromkeys(
                            _clean_prefix(url)
                            for url in config.skip_clean_urls)))
