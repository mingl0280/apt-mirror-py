# Isolated Linux deployment and live download smoke test

The first sections document earlier revisions. The current implementation
is described in **Legacy compatibility follow-up** below.

On 2026-11-24, the current Python package and tests were copied to
`/opt/apt-mirror-py` on `192.168.1.202`. The host runs Python 3.14.10.
The existing `/sources/mirror` and `/sources/skel` trees were not modified.

`python3 -m unittest discover -s tests -q` passed all 41 tests on the host.

`benchmarks/live_download_smoke.py` fetched current Release metadata and used
its size and SHA256 entries to download two indexes into isolated `/tmp`
staging. This script does not verify Release signatures or publish mirror
metadata; it is a transfer smoke test only.

| Index | Size | Transfer | Elapsed | Result |
| --- | ---: | ---: | ---: | --- |
| Ubuntu noble-backports multiverse Packages.xz | 748 B | 1 request | 0.763 s | SHA256 passed |
| Kali rolling main amd64 Packages.gz | 21,853,003 B | 6 range workers | 8.448 s | SHA256 passed |

A second run marked both files `current` after checking their hashes (0.209 s
and 0.260 s). No `.part` files remained. Additional fresh Kali transfers
showed substantial run-to-run variation:

| Workers | First comparison | Second comparison |
| ---: | ---: | ---: |
| 1 | 2.031 s | 1.775 s |
| 6 | 8.448 s | 0.777 s |

The six-worker first run may include upstream mirror or cache effects. These
small samples do not establish a stable speed ratio. They confirm that both
single and ranged paths work in the real environment, including temporary
staging, final verification, and repeat-run detection.

The first deployment was a source package and download test harness. It did
not replace the running Perl mirror.

## Signed CLI follow-up

The isolated deployment was updated with the signed mirror CLI. The Linux
suite now passes 49 tests, including a tiny locally signed repository that
exercises first publication, repeated runs, a rejected signature, a failed
archive download, atomic replacement of an existing distribution, detached
Release signature fallback, a flat repository, dry-run isolation, and scoped
cleanup preview/application.

A live Kali dry-run verified `kali-rolling` with the installed
`/usr/share/keyrings/kali-archive-keyring.pgp` and reported signer fingerprint
`827C8569F2518CC677FECA1AED65462EC8D5E4C5`. It selected one required
index and 83 referenced archives totaling 733,368,010 bytes. No archives
were downloaded and no metadata was published by that dry-run. The existing
Perl mirror at `/sources/mirror` remains untouched. The CLI is available as
`apt-mirror-py` through `/usr/local/bin/apt-mirror-py`, which runs the source
package in `/opt/apt-mirror-py` without changing system Python packages.
`apt-mirror-py --config /opt/apt-mirror-py/kali-smoke.list --dry-run
--skip-optional-indexes` completed successfully with the same signer and
archive counts.
The `--plan-output` option wrote 84 JSON Lines: one required index and 83
archive files, including each URL, expected SHA256, size, and worker count.

## Real signed publication and APT consumer check

The installed CLI also mirrored Debian `bookworm-backports` `contrib` into
`/tmp/apt-mirror-py-debian-live`, using the installed Debian archive keyring.
It verified signer `4CB50190207B4758A3F73A796ED0E7B82643E131`, downloaded
32 archives totaling 106,577,436 bytes, and atomically published one
distribution. A repeat run reported all 32 archives and the required index
as current, with no new archive downloads or partial files.

The first isolated `apt-get update` encountered the host's apt-file Contents
index targets, which were not selected with `_contents 0`. After adding
Release-listed `Contents-amd64.gz` and `Contents-all.gz` selection and running
the CLI with `_contents 1`, the normal isolated `apt-get update` succeeded.
APT then downloaded the 4,852-byte `diaspora-installer-mysql` package through
the published `file:` mirror path. All APT source, list, cache, and download
paths used for this check were under `/tmp/apt-mirror-py-debian-live`; the
system APT sources and `/sources/mirror` were not changed. The Linux test
suite now passes 50 tests.

## Full Kali repository on `/sources`

On 2026-11-24, `/dev/sda1` had 2.4 TB free. A full signed mirror run for
`kali-rolling non-free-firmware` was published in the separate
`/sources/apt-mirror-py-validation` tree with `--workers 16
--skip-optional-indexes --skip-postmirror`. The installed Kali keyring
validated signer `827C8569F2518CC677FECA1AED65462EC8D5E4C5`.
The run downloaded all 83 referenced archives (733,368,010 bytes, about
700 MiB) and the required amd64 Packages index, then published one
distribution. Of the archives, 53 used single-request transfers and 30
used ranged transfers. A second run reported 83 current archives, one
current index, and zero downloads. The mirror tree contains 86 regular
files and no `.part` or `.tmp` files.

With temporary APT source and list directories, `apt-get update` succeeded
against `file:/sources/apt-mirror-py-validation/mirror/https/http.kali.org/kali`.
APT then downloaded `bluez-firmware_1.2-13_all.deb` (190 kB) from that
local mirror. Its SHA256 matched the published pool file:
`4dba886ff82067c814f81ec9d675aa0631b497ca8e7d7985694bac6cfe61e825`.
The APT check disabled optional language and Contents targets because this
validation run selected only required indexes. The production Perl mirror
under `/sources/mirror` and its staging tree under `/sources/skel` were
not modified.

## Legacy compatibility follow-up

After removing mandatory signature and expiry checks and restoring the legacy
path and copy behavior, the revised package was installed under the isolated
`/opt/apt-mirror-py` deployment. The final Linux suite passed 55 tests. The active
`/etc/apt/mirror.list` parsed successfully with 39 binary entries, 2 source
entries, 11 clean scopes, and 9 explicit settings. No full production mirror
run was performed.

An unsigned-keyring-free CLI run with `--configs` mirrored Debian
`bookworm-backports contrib` under the separate
`/sources/apt-mirror-py-compat-validation` tree. It downloaded 32 archives
totaling 106,577,436 bytes, and a repeat run found all 32 current. The
published path was `mirror/deb.debian.org/debian`, matching the Perl host/path
layout. Missing upstream plain `Packages` returned a warning while the gzip
variant was used. Isolated APT successfully updated from the local mirror,
including amd64 and all Contents indexes, and downloaded the 4,852-byte
`diaspora-installer-mysql` package. No `.part` files remained. The production
`/sources/mirror`, `/sources/skel`, and `/sources/var` trees were untouched.

## Full isolated production-config rehearsal

On 2026-11-25, the installed CLI completed a full run using the active
`/etc/apt/mirror.list` against `/sources/apt-mirror-py-full-candidate`.
The candidate started as a hardlink clone of the production mirror and skel
trees. Its `var` directory and postmirror script were separate and pointed
only into the candidate tree. The run did not use the production base path.

The full dry-run found 29 repository distributions and 949,815 archive
references totaling 3,165,067,003,471 bytes. The candidate run kept
949,812 archives current by the legacy same-size rule and downloaded none.
Three archive requests returned upstream HTTP 404: two `snappea` packages
from Raspbian stretch and an old Debian bullseye-security Chromium driver.
They were warned and counted as `archives_failed: 3`; the run completed.
It published 2,224 index files, generated a candidate-local `clean.sh`
with 647 removal entries, and ran the candidate-local postmirror script
with exit code 0. No `.part` or `.tmp` files remained in the candidate mirror.
The filesystem still reported about 2.4 TB free.

An isolated APT consumer used the candidate's Debian bookworm-backports
and Kali rolling repositories with their installed archive keyrings.
`apt-get update` verified both signed InRelease files and read the
Packages and Contents indexes. `apt-get download diaspora-installer-mysql`
fetched the 4,852-byte package from the candidate's local pool; its SHA256
was `05e50f43d84a1cbff57c4019d867d6677007d1307f9202f6005a117192f40a70`.
All APT state for this check was under the candidate's `var/apt-verify`.
The Linux test suite passed 59 tests after this run.

At the time of this rehearsal, production cutover had not been performed. It
exercised the full configured repository set and cleanup/postmirror flow,
but it did not redownload the existing 3.16 TB of same-size archives.
The three unavailable upstream archive files remain a source-side gap.

## Deb822 configuration migration

On 2026-11-26, the active `/etc/apt/mirror.list` was migrated while the
systemd service was idle. The old file is backed up at
`/etc/apt/mirror.list.before-deb822-20261126-002113`. The live root file now
contains only comments and `set include_dir=/etc/apt/mirror.conf.d`.
The new directory contains `10-generic.conf` with nine active settings and
commented examples for the remaining supported options,
`20-repositories.sources` with 39 binary entries (35 unique targets) and one
source entry in Deb822 form, and `90-clean.conf` with 11 legacy cleanup scopes.

The converter previewed and then verified the effective settings, source
multisets, and cleanup scopes against the backup. A separate readback with
the production parser also reported exact equivalence. The six-hour timer
remained active and the service idle. No mirror update or APT consumer test
was started for this migration.

The repository file was then regrouped into 10 Deb822 stanzas. Compatible
suites and architectures share a stanza; combinations absent from the old
file are not added. Four duplicate binary targets were removed. The prior
source file is backed up at
`/etc/apt/mirror.conf.d/20-repositories.sources.before-suite-merge-20261126-002551`.
A readback confirmed the same 35 unique binary targets, one source target,
settings, and cleanup scopes as the original mirror.list. No mirror run or
APT consumer test was started for the regrouping.

The grouping was refined to prefer all suites for one architecture when
that uses the same number of stanzas. Debian Bullseye and Bookworm now
share an amd64 stanza, while the i386 stanza contains only Bullseye suites.
The same arrangement applies to Debian security. This preserves the exact
source set without adding i386 Bookworm. The previous grouped file is backed
up at `/etc/apt/mirror.conf.d/20-repositories.sources.before-suite-merge-20261126-002857`.

The matching Kali binary and source stanzas were then combined as
`Types: deb deb-src`, leaving nine stanzas. The preceding file is backed up
at `/etc/apt/mirror.conf.d/20-repositories.sources.before-suite-merge-20261126-003106`.
Readback still found the original 35 unique binary targets and one source
target. The mirror service was not started for this edit.

## Opt-in per-item performance trace

On 2026-11-26, `--perf TRACE.jsonl` was added to the installed CLI. The trace
streams separate timing events for individual HTTP requests, transfers,
index parses, manifest records, archive checks, metadata publication, and
cleanup lookups, plus stage timings. A tiny isolated local HTTP run exercised
the enabled trace, and another isolated case exercised the default path with
tracing disabled. The production systemd service stayed idle and its timer
remained active. No live mirror run or APT consumer test was started for this
feature.

After the Perl `apt-mirror` package was uninstalled, its original 16,786-byte
`/etc/apt/mirror.list` reappeared and exactly matched the saved pre-migration
backup. The Deb822 directory remained intact. The new `--restore-stub` mode
verified equivalent settings and repository targets, then restored only the
include-only root file. The uninstall-restored legacy file was backed up at
`/etc/apt/mirror.list.before-include-restore-20261126-004502`. The timer
remained active; no mirror run was started.

## Production systemd cutover

On 2026-11-25, the live service completed once in 8 minutes 46 seconds with
exit status 0. It processed 949,850 archive entries, reused 949,730,
downloaded 117 archives and 216 indexes, published 2,224 indexes, and ran
postmirror cleanup with exit code 0. The same three unavailable upstream
archives produced warnings. A hardlink rollback snapshot is at
`/sources/apt-mirror-py-precutover-20261125`; it shares file data with the
live mirror rather than copying terabytes of contents.

An isolated signed APT update against `/sources/mirror` read Debian and Kali
metadata, and APT downloaded `diaspora-installer-mysql` from the live pool.
The root crontab's old apt-mirror entry was commented out by the operator.
`apt-mirror-py.timer` is active with runs at 00:00, 06:00, 12:00, and 18:00
server time. It has `Persistent=false`, so enabling it did not trigger an
immediate catch-up run.
