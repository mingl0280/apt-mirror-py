# Why the Perl index stage is slow

This is a code-path analysis, not a timed benchmark. The sample `mirror.list`
contains 39 binary directives and 2 source directives. They expand to 144
component index scans, but only 132 distinct `(URI, distribution, component,
architecture, kind)` indexes. Twelve Bookworm amd64 indexes are scanned twice.

## Costs in the current Perl path

1. Initial download planning requests gzip, bzip2, and xz variants for every
   Packages and Sources index, even when Release metadata lists only one or
   two variants (`sample-source/apt-mirror`, lines 393–466). Missing variants
   cost HTTP requests and retries. The script also requests multiple metadata
   files that may not exist.
2. `process_index` prefers `.gz` whenever it exists, then `.xz`, then `.bz2`
   (lines 777–800). It runs an external decompressor and writes a complete
   uncompressed copy into staging on *every run*, even if the compressed file
   has not changed. Compressed and plain copies consume disk space at once.
   A stale gzip file can take precedence over a newer xz file.
3. Index scanning is serial (lines 851–887). Each package stanza is split
   into fields, several intermediate strings are built, and `stat` is called
   for every referenced archive (lines 800–845). Hash output files and the
   `ALL` and `NEW` files are written line by line. On a large mirror this can
   become a disk I/O bottleneck, particularly with cold filesystem caches.
4. Translation and DEP-11 discovery rereads Release data for each configured
   component (lines 479–719). Repeated configuration entries repeat this work.
5. The downloader launches many `wget` workers, but index processing runs
   afterward on one Perl process. `nthreads` does not speed up decompression
   or package stanza scanning (lines 225–282, 851–887).

The pinned Kali Release lists a 21,853,003-byte gzip file for
`main/binary-amd64/Packages.gz`; the four Kali amd64 component gzip indexes
sum to 22,143,750 compressed bytes. The sample configuration selects many
more repositories, distributions, and architectures. This shows why repeated
full decompression and scanning can be substantial, but does not establish
which operation dominates on a particular host.

## Better processing path

1. Verify a Release signature, parse its SHA256 table once, and request only
   the chosen listed compression variant per required index.
2. Deduplicate index references before downloading or parsing. Use the
   `(URI, distribution, relative index path, SHA256)` identity to avoid
   rescanning an unchanged verified index. Any persistent scan cache needs a
   durable versioned format and must be invalidated when parser logic changes.
3. Stream decompression and stanza parsing directly from the verified
   compressed file. Emit archive references as they are parsed. Do not write
   an uncompressed staging copy or accumulate every stanza in memory.
4. Batch archive existence checks by directory where useful, and keep
   processing concurrency bounded to the available disk and CPU capacity.
   Measure before adding parallel scans; concurrent reads can hurt on a
   spinning disk.
5. Preserve the previous published state until all required index and
   archive work succeeds.

The Python package now has `iter_packages_file(path)` and
`iter_sources_file(path)`, which read gzip, bzip2, xz, or plain files one
stanza at a time. Its planner already selects a Release-listed index variant
and deduplicates download targets. The future acquisition stage should feed
these iterators into an incremental archive plan. The existing `parse_*`
functions still return complete tuples for tests and small in-memory inputs.

## How to measure the remaining bottleneck

On a representative local mirror, record elapsed time and bytes for metadata
download, decompression/parse, archive existence checks, archive download,
and index publication separately. For the scan stage, compare a cold run and
an unchanged warm run, and record peak memory and staging disk usage. This
will show whether parser CPU, storage latency, or repeated network probes
deserve the next optimization.

## Read-only measurement on the existing mirror

On 2026-11-24, the configured mirror at `192.168.1.202` was inspected
read-only. The active sample configuration selects 132 distinct component
indexes, all of which had a staged plain file. Their plain files total
1,146,833,941 bytes (1.07 GiB). Because 12 entries are repeated in the
configuration, the Perl loop processes 144 component indexes.

For the Kali rolling `main/binary-amd64/Packages` index, the existing plain
file and `Packages.gz` decompressed to identical bytes. The file contains
71,670 package records, 85,628,109 plain bytes, and 21,853,003 gzip bytes.
All 71,670 referenced archives were present in the mirror.

| Operation | First pass | Warm pass |
| --- | ---: | ---: |
| Perl stanza scan of existing plain file, without archive checks | 1.349 s | 1.171 s |
| Perl stanza scan with archive file checks | 1.494 s | 1.543 s |
| Python gzip scan with archive file checks | 2.241 s | 2.178 s |
| Python gzip scan with a 256-directory listing cache | 3.508 s | 3.532 s |
| gzip decompression to `/dev/null` | 0.38 s | 0.39 s |

The Perl benchmark follows the field split and file existence check from
`process_index`, but omits its five output files and path normalization. The
Python benchmark extracts only `Filename`, so its parser time is not a
like-for-like comparison with the full Python package parser. The measurement
also excludes network transfers, checksum verification, and publication.
“First pass” is the first measured pass after reading both files for content
equality; it is **not** an OS-cache-cold run.

For this populated mirror, archive checks added about 0.15–0.37 seconds to
the Perl scan. The directory listing cache made the Python scan slower because
it enumerated populated directories. The earlier synthetic workload had all
archives absent, where that cache helped considerably. Directory caching
should therefore be conditional on measured missing-file patterns, not a
default optimization.

The most promising warm-run improvement is to skip parsing indexes whose
verified Release SHA256 and parser version match a durable cached archive
manifest. This would avoid rereading the 1.07 GiB selected plain data when
metadata has not changed. Streaming compressed indexes still saves the full
plain staging copy and its writes; this measurement does not show it improving
elapsed time on the single Kali index.
