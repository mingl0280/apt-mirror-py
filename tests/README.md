# Parser test contract

These tests specify the first Python milestone. The `apt_mirror` parser
implementation and offline download planner are covered by the suite.

Run the tests from the repository root with the standard library:

```bash
python3 -m unittest discover -s tests -v
```

The tests expect these public interfaces:

- `apt_mirror.errors.ParseError` reports a parse failure with context.
- `apt_mirror.config.parse_config(text, default_arch=...)` returns settings,
  binary and source entries, and cleanup URLs.
- `apt_mirror.metadata.parse_release(text)` returns Release fields and file
  metadata keyed by relative path.
- `apt_mirror.metadata.parse_translation_index(text)` returns translation
  files keyed by name.
- `apt_mirror.metadata.discover_translation_files(release, component)` returns
  matching Release paths.
- `apt_mirror.metadata.discover_dep11_files(release, component, architecture)`
  returns matching Release paths.
- `apt_mirror.package_index.parse_packages(data, filename=...)` returns binary
  package records.
- `apt_mirror.package_index.parse_sources(data, filename=...)` returns source
  package and file records.

Collections are ordered tuples except lookup tables, which are mappings.
Malformed metadata raises `ParseError`; an empty `Packages` index is valid.
Paths are relative to the repository root and must not escape it. The tests
accept indexes and archive records without SHA256 and expand a comma-separated
`arch` option into one binary source per architecture.

The parsing suite covers ordinary and flat repositories; Ubuntu and Kali
Release metadata; translation and DEP-11 selection; plain, gzip, bzip2, and xz
indexes; multiline source file lists; zero-byte files; missing fields; invalid
hashes and sizes; unsafe paths; and corrupt compression. Network access is not
required to run the suite.

## Real repository fixtures

Downloaded on 2026-11-24 from these repository URLs:

- <https://archive.ubuntu.com/ubuntu/dists/noble-backports/Release>
- <https://archive.ubuntu.com/ubuntu/dists/noble-backports/multiverse/binary-amd64/Packages.xz>
- <https://archive.ubuntu.com/ubuntu/dists/noble-backports/multiverse/source/Sources.xz>
- <https://http.kali.org/kali/dists/kali-rolling/Release>

The fixture integrity test pins each downloaded file's SHA256 digest and checks
that the Ubuntu compressed indexes match the hashes and sizes listed in its
Release file. Repository data can change upstream; tests use these saved files
and never depend on a live mirror being available. Pre-commit excludes these
upstream files so formatters and spell checkers cannot change their bytes.
