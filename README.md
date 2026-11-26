# apt-mirror-py

APT repository mirroring on Linux with the legacy apt-mirror host/path layout,
Python HTTP downloads, and apt-mirror style index copying and cleanup.

## Run a mirror

Install with `python3 -m pip install .` or run from the repository root with
`python3 -m apt_mirror`. The default config is `/etc/apt/mirror.list`.
`--config`, `--configs`, or a positional path may select another file.

```text
set base_path /srv/apt-mirror
deb https://deb.debian.org/debian bookworm main
deb-src https://deb.debian.org/debian bookworm main
clean https://deb.debian.org/debian/
```

```bash
python3 -m apt_mirror mirror.list --dry-run
python3 -m apt_mirror mirror.list --dry-run --plan-output plan.jsonl
python3 -m apt_mirror mirror.list
```

The dry-run downloads candidate indexes into temporary state storage to count
archive references, but does not download archives or change the published
mirror. The normal run downloads archives, copies metadata and compressed and
uncompressed indexes into `mirror/<host>/<path>/`, and writes the configured
`cleanscript`. The `clean` and `skip-clean` directives control cleanup scopes.
With the Perl defaults, `_contents` and `run_postmirror` are enabled,
`_autoclean` is disabled, and the postmirror script is attempted after each run.
`_autoclean 1` deletes cleanup candidates during the run. The generated
cleaning script can also be called by the postmirror script.

Release signatures and `Valid-Until` are **not enforced**. No keyring is
required. Missing Release metadata does not prevent candidate Packages and
Sources index attempts. Missing candidate indexes and archives produce
warnings and do not stop the run. SHA256 mismatches on downloaded files
produce warnings and overwrite the destination. The mirror
operator and APT clients remain responsible for repository trust decisions.

## Downloads and configuration

The `apt_mirror` package parses `mirror.list`, Release/InRelease, translation
metadata, Packages, and Sources data. It streams package records into a
temporary SQLite archive manifest. Downloads use Python's HTTP library; no
Python code invokes `wget` or another external download program.

### Directory and Deb822 configuration

An old one-line `mirror.list` still works. A root file can instead contain
`set include_dir=/etc/apt/mirror.conf.d`. Files ending in `.conf`, `.list`,
or `.sources` in that directory are read in filename order. Each file may
contain legacy `set`, `deb`, `deb-src`, `clean`, and `skip-clean` lines or
Deb822 source stanzas. Deb822 fields include `Types`, `URIs`, `Suites`,
`Components`, `Architectures`, `Signed-By`, and `Enabled`. Multiple URIs,
suites, types, and architectures expand to their combinations. `Signed-By`
is retained as source metadata; the mirror does not enforce signatures.

Preview conversion of an existing file with
`python3 -m apt_mirror.migrate_config`; add `--apply` to write the new files.
The converter backs up `mirror.list`, writes documented settings to
`10-generic.conf`, Deb822 sources to `20-repositories.sources`, cleanup
scopes to `90-clean.conf`, and leaves only an explained `include_dir` line
in `mirror.list`. It checks that the effective settings and source set match
before writing.
Sources with the same type, URI, components, and Signed-By value can combine
`Suites` and `Architectures`. The converter creates only combinations already
present in the old file, chooses the smaller stanza grouping, and removes
duplicates. When both groupings have the same size, it favors keeping an
architecture's suites together. Matching binary and source stanzas combine
as `Types: deb deb-src`. For an existing migrated
directory, preview the regrouping with
`python3 -m apt_mirror.migrate_config --merge-suites` and add `--apply` to
replace `20-repositories.sources` after saving its previous contents.

If uninstalling the Perl `apt-mirror` package restores or removes
`/etc/apt/mirror.list` while `mirror.conf.d` remains, restore only the small
include file with:

```sh
sh /opt/apt-mirror-py/deploy/migrate-mirror-config.sh --restore-stub
sh /opt/apt-mirror-py/deploy/migrate-mirror-config.sh --restore-stub --apply
```

The first command previews and verifies that an existing legacy file has
the same effective sources and settings. The second writes `mirror.list`
atomically and backs up any file it replaces. It does not regenerate or
overwrite the Deb822 directory.

Files smaller than 4 MiB use one HTTP request. Files at least 4 MiB use range
workers, roughly one per 4 MiB and at most 32. A server that ignores Range
receives one full request. `nthreads` caps combined concurrent requests at 32.
`limit_rate` caps the combined download throughput, defaulting to the Perl
script's `100m`. Downloads use temporary files, bounded retries and timeouts,
and a 256 MiB staging-space reserve. Existing same-size archives are reused
without hashing, as in Perl apt-mirror. Downloaded files with a different
SHA256 are warned about and overwritten.

Supported legacy settings are `base_path`, `mirror_path`, `skel_path`,
`var_path`, `defaultarch`, `nthreads`, `_contents`, `_autoclean`,
`cleanscript`, `run_postmirror`, `postmirror_script`, `limit_rate`, `unlink`,
`auth_no_challenge`, `no_check_certificate`, `use_proxy`, `http_proxy`,
`https_proxy`, `proxy_user`, and `proxy_password`. The CLI supports normal
and flat distribution paths.

`_tilde 0` is accepted; `_tilde 1` is not implemented.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

The live deployment and APT consumer checks are recorded in
`DEPLOYMENT_SMOKE.md`.

## Debian packages and releases

Every pull request and push runs the Python test suite, builds the native
Debian package, and install-tests that package in Debian 12 and Ubuntu 24.04
containers. A tag matching `v*` also creates a GitHub release containing the
`.deb`, `.changes`, and `.buildinfo` files. The package installs the systemd
service and timer in a disabled, stopped state.

Build the same package locally on Debian or Ubuntu with:

```sh
sudo apt-get install build-essential debhelper dh-python \
  pybuild-plugin-pyproject python3-all python3-setuptools
dpkg-buildpackage --no-sign --build=binary
```

## Per-item performance trace

Timing is disabled by default. Pass `--perf` with a new JSON Lines path to
record every work item while running the normal CLI:

```sh
apt-mirror-py --perf /sources/var/apt-mirror-perf.jsonl
```

The trace has stage events for configuration, Release acquisition, planning,
index download, index scanning, archive processing, publication, cleanup,
and postmirror. Individual operation events identify each HTTP request and
range, downloaded file, parsed index, manifest record, archive check,
metadata copy or decompression, and cleanup lookup. Each record contains a
start offset and duration in nanoseconds; stage records also include process
CPU time. Worker events can overlap, so their durations should not be added
to estimate wall time. The output file is created with mode 0600 and will
not overwrite an existing trace. A large mirror can produce millions of
events and a correspondingly large trace; the writer streams them to disk.

## Six-hour systemd schedule

`deploy/apt-mirror-py.service` runs one mirror update as a systemd background
job. `deploy/apt-mirror-py.timer` starts it at 00:00, 06:00, 12:00, and 18:00
server time. The service uses the default `/etc/apt/mirror.list` and waits
for the `/sources` mount. Runs longer than six hours are not terminated;
systemd does not start a second instance of the same service.

On the mirror host, with this repository available under `/opt/apt-mirror-py`,
install the units as root, then activate the timer after the legacy cron
entry has been manually disabled:

```sh
sh /opt/apt-mirror-py/deploy/install-systemd.sh
sh /opt/apt-mirror-py/deploy/install-systemd.sh --activate
systemctl list-timers apt-mirror-py.timer
journalctl -u apt-mirror-py.service -n 100 --no-pager
```

The installer never changes root's crontab. Activation refuses to proceed
while the legacy `apt-mirror` cron entry or process is active.
The timer does not run a missed interval when first activated. To remove
the timer and service after the service finishes:

```sh
sh /opt/apt-mirror-py/deploy/remove-systemd.sh
```
