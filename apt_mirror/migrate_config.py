"""Move an apt-mirror mirror.list into include_dir and Deb822 source files."""

import argparse
from collections import Counter, OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import tempfile

from .config import Config, parse_config


OPTIONS = (
    ("base_path", "/var/spool/apt-mirror", "Root of mirror, skel, and var."),
    ("mirror_path", "$base_path/mirror", "Published repository files."),
    ("skel_path", "$base_path/skel", "Downloaded metadata and staging files."),
    ("var_path", "$base_path/var", "Lock, plan, and generated script state."),
    ("defaultarch", "amd64", "Architecture for unqualified deb entries."),
    ("nthreads", "20", "Maximum HTTP concurrency; Python caps it at 32."),
    ("limit_rate", "100m", "Combined download speed limit (k, m, or g)."),
    ("_contents", "1", "Download Contents indexes when available."),
    ("_tilde", "0", "Legacy tilde option; only 0 is supported."),
    ("_autoclean", "0", "Remove cleanup candidates during the run."),
    ("cleanscript", "$var_path/clean.sh", "Generated cleanup script path."),
    ("run_postmirror", "1", "Run postmirror_script after an update."),
    ("postmirror_script", "$var_path/postmirror.sh", "Post-update script path."),
    ("unlink", "0", "Unlink an old destination before replacement."),
    ("auth_no_challenge", "0", "Send origin credentials preemptively."),
    ("no_check_certificate", "0", "Disable TLS certificate checks."),
    ("use_proxy", "off", "Use the configured HTTP and HTTPS proxy."),
    ("http_proxy", "http://proxy.example:3128", "HTTP proxy URL."),
    ("https_proxy", "http://proxy.example:3128", "HTTPS proxy URL."),
    ("proxy_user", "user", "Proxy authentication username."),
    ("proxy_password", "password", "Proxy authentication password."),
)


def _quote(value):
    return shlex.quote(value)


def render_stub(include_dir):
    return ("# Sources and settings moved to Deb822-style files in\n"
            f"# {include_dir}\n"
            "# Legacy one-line mirror.list files are still supported.\n"
            f"set include_dir={_quote(str(include_dir))}\n")


def render_generic(settings):
    lines = [
        "# apt-mirror-py settings migrated from mirror.list.",
        "# Uncomment and edit an inactive option only when needed.",
        "# Repository sources are in 20-repositories.sources; cleanup is in",
        "# 90-clean.conf. Release signatures and expiry are not enforced here.",
        "",
    ]
    for key, example, description in OPTIONS:
        lines.append(f"# {description}")
        value = settings.get(key)
        lines.append(("" if value is not None else "# ") +
                     f"set {key} {_quote(value if value is not None else example)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_sources(config: Config):
    stanzas = []
    for kind, items in (("deb", config.binary_sources),
                        ("deb-src", config.source_sources)):
        grouped = OrderedDict()
        for source in items:
            key = (source.uri, source.components, source.signed_by)
            suite_arches = grouped.setdefault(key, OrderedDict())
            arches = suite_arches.setdefault(source.distribution, [])
            if source.architecture not in arches:
                arches.append(source.architecture)
        for (uri, components, signed_by), suite_arches in grouped.items():
            by_arch_set = OrderedDict()
            for suite, arches in suite_arches.items():
                key = frozenset(arches)
                if key not in by_arch_set:
                    by_arch_set[key] = ([], arches)
                by_arch_set[key][0].append(suite)

            arch_suites = OrderedDict()
            for suite, arches in suite_arches.items():
                for architecture in arches:
                    arch_suites.setdefault(architecture, []).append(suite)
            by_suite_set = OrderedDict()
            for architecture, suites in arch_suites.items():
                key = frozenset(suites)
                if key not in by_suite_set:
                    by_suite_set[key] = (suites, [])
                by_suite_set[key][1].append(architecture)

            # Minimize stanzas. On a tie, prefer all suites for one
            # architecture, which is easier to read for mixed-arch mirrors.
            rectangles = (by_suite_set if len(by_suite_set) <= len(by_arch_set)
                          else by_arch_set)
            for suites, architectures in rectangles.values():
                stanzas.append({"types": kind, "uri": uri,
                                "suites": tuple(suites),
                                "components": components,
                                "architectures": tuple(architectures),
                                "signed_by": signed_by})

    # A source stanza can share a binary stanza only when its complete
    # suite set matches. Architectures constrain deb but not deb-src.
    for source in stanzas:
        if source["types"] != "deb-src":
            continue
        for binary in stanzas:
            if (binary["types"] == "deb" and
                    binary["uri"] == source["uri"] and
                    binary["components"] == source["components"] and
                    binary["signed_by"] == source["signed_by"] and
                    set(binary["suites"]) == set(source["suites"])):
                binary["types"] = "deb deb-src"
                source["types"] = None
                break

    lines = ["# Deb822 repository sources migrated from mirror.list.",
             "# Types, suites, and architectures share compatible stanzas",
             "# without adding repository targets.", ""]
    for stanza in stanzas:
        if stanza["types"] is None:
            continue
        lines.extend(("Types: " + stanza["types"],
                      "URIs: " + stanza["uri"],
                      "Suites: " + " ".join(stanza["suites"])))
        if stanza["components"]:
            lines.append("Components: " + " ".join(stanza["components"]))
        if stanza["architectures"][0] is not None:
            lines.append("Architectures: " +
                         " ".join(stanza["architectures"]))
        if stanza["signed_by"]:
            lines.append("Signed-By: " + stanza["signed_by"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_clean(config: Config):
    lines = ["# Legacy cleanup scopes remain one-line directives.", ""]
    lines.extend("clean " + _quote(url) for url in config.clean_urls)
    lines.extend("skip-clean " + _quote(url)
                 for url in config.skip_clean_urls)
    return "\n".join(lines).rstrip() + "\n"


def _equivalent(before, after):
    return (before.settings == {key: value for key, value in
                                after.settings.items() if key != "include_dir"}
            and set(before.binary_sources) == set(after.binary_sources)
            and set(before.source_sources) == set(after.source_sources)
            and Counter(before.clean_urls) == Counter(after.clean_urls)
            and Counter(before.skip_clean_urls) == Counter(after.skip_clean_urls))


def migrate(config_path, include_dir, *, apply=False):
    config_path = Path(config_path).absolute()
    include_dir = Path(include_dir).absolute()
    if include_dir == config_path.parent:
        raise ValueError("include_dir must be a separate directory")
    original = config_path.read_text(encoding="utf-8")
    from .runner import host_architecture
    preliminary = parse_config(original, default_arch=host_architecture(),
                               source_path=config_path)
    if "include_dir" in preliminary.settings:
        raise ValueError("mirror.list already contains include_dir")
    default_arch = preliminary.settings.get("defaultarch", host_architecture())
    if default_arch == "<running host architecture>":
        default_arch = host_architecture()
    before = parse_config(original, default_arch=default_arch,
                          source_path=config_path)
    files = {
        "10-generic.conf": render_generic(before.settings),
        "20-repositories.sources": render_sources(before),
        "90-clean.conf": render_clean(before),
    }
    # Validate generated content without depending on the destination path.
    with tempfile.TemporaryDirectory(prefix=".mirror-conf-check-",
                                     dir=config_path.parent) as check:
        check = Path(check)
        for name, content in files.items():
            (check / name).write_text(content, encoding="utf-8")
        after = parse_config(f"set include_dir={_quote(str(check))}\n",
                             default_arch=default_arch)
    if not _equivalent(before, after):
        raise ValueError("generated config does not match the original")
    result = {"settings": len(before.settings),
              "binary_sources": len(before.binary_sources),
              "source_sources": len(before.source_sources),
              "clean_scopes": len(before.clean_urls),
              "skip_clean_scopes": len(before.skip_clean_urls),
              "files": [str(include_dir / name) for name in files],
              "applied": False}
    if not apply:
        return result
    if include_dir.exists():
        raise FileExistsError(f"destination already exists: {include_dir}")
    include_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = config_path.with_name(config_path.name + ".before-deb822-" +
                                   datetime.now().strftime("%Y%m%d-%H%M%S"))
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    fd, staged_name = tempfile.mkstemp(prefix=".mirror-list-",
                                       dir=config_path.parent)
    staged = Path(staged_name)
    temporary_dir = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render_stub(include_dir))
        os.chmod(staged, config_path.stat().st_mode & 0o777)
        temporary_dir = Path(tempfile.mkdtemp(prefix=".mirror-conf-",
                                              dir=include_dir.parent))
        for name, content in files.items():
            path = temporary_dir / name
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o600 if "proxy_password" in content and
                     before.settings.get("proxy_password") else 0o644)
        backup.write_bytes(config_path.read_bytes())
        os.chmod(backup, config_path.stat().st_mode & 0o777)
        os.replace(temporary_dir, include_dir)
        temporary_dir = None
        os.replace(staged, config_path)
    finally:
        staged.unlink(missing_ok=True)
        if temporary_dir is not None and temporary_dir.exists():
            for path in temporary_dir.iterdir():
                path.unlink()
            temporary_dir.rmdir()
    result["applied"] = True
    result["backup"] = str(backup)
    return result


def restore_stub(config_path, include_dir, *, apply=False):
    """Recover mirror.list after a package uninstall restores or removes it."""
    config_path = Path(config_path).absolute()
    include_dir = Path(include_dir).absolute()
    for name in ("10-generic.conf", "20-repositories.sources",
                 "90-clean.conf"):
        if not (include_dir / name).is_file():
            raise FileNotFoundError(f"missing migrated config: {include_dir / name}")
    stub = render_stub(include_dir)
    from .runner import host_architecture
    preliminary = parse_config(stub, default_arch=host_architecture(),
                               source_path=config_path)
    default_arch = preliminary.settings.get("defaultarch", host_architecture())
    if default_arch == "<running host architecture>":
        default_arch = host_architecture()
    after = parse_config(stub, default_arch=default_arch,
                         source_path=config_path)
    if not after.binary_sources and not after.source_sources:
        raise ValueError("included directory contains no repository sources")
    original = (config_path.read_text(encoding="utf-8")
                if config_path.exists() else None)
    if original is not None and original != stub:
        before = parse_config(original, default_arch=default_arch,
                              source_path=config_path)
        if "include_dir" in before.settings:
            raise ValueError("mirror.list already includes another directory")
        if not _equivalent(before, after):
            raise ValueError("existing mirror.list differs from mirror.conf.d")
    result = {"applied": False, "changed": original != stub,
              "config": str(config_path), "include_dir": str(include_dir),
              "binary_sources": len(set(after.binary_sources)),
              "source_sources": len(set(after.source_sources))}
    if not apply or original == stub:
        return result
    backup = None
    if original is not None:
        backup = config_path.with_name(
            config_path.name + ".before-include-restore-" +
            datetime.now().strftime("%Y%m%d-%H%M%S"))
        if backup.exists():
            raise FileExistsError(f"backup already exists: {backup}")
    descriptor, name = tempfile.mkstemp(prefix=".mirror-list-",
                                        dir=config_path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(stub)
        os.chmod(temporary, config_path.stat().st_mode & 0o777
                 if original is not None else 0o644)
        if backup is not None:
            backup.write_text(original, encoding="utf-8")
            os.chmod(backup, config_path.stat().st_mode & 0o777)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    result["applied"] = True
    if backup is not None:
        result["backup"] = str(backup)
    return result


def merge_suites(config_path, include_dir, *, apply=False):
    """Regroup an existing migrated .sources file without touching settings."""
    config_path = Path(config_path).absolute()
    include_dir = Path(include_dir).absolute()
    root = parse_config(config_path.read_text(encoding="utf-8"),
                        source_path=config_path)
    configured = root.settings.get("include_dir")
    if configured is None or Path(configured).absolute() != include_dir:
        raise ValueError("mirror.list does not include this directory")
    source_file = include_dir / "20-repositories.sources"
    original = source_file.read_text(encoding="utf-8")
    before = parse_config(original, source_path=source_file)
    if before.settings or before.clean_urls or before.skip_clean_urls:
        raise ValueError("repository file also contains settings or cleanup")
    rendered = render_sources(before)
    after = parse_config(rendered, source_path=source_file)
    if (set(before.binary_sources) != set(after.binary_sources) or
            set(before.source_sources) != set(after.source_sources)):
        raise ValueError("suite merge changed the effective source set")
    result = {"applied": False, "changed": rendered != original,
              "source_file": str(source_file),
              "binary_sources": len(set(before.binary_sources)),
              "source_sources": len(set(before.source_sources)),
              "stanzas": sum(line.startswith("Types: ")
                             for line in rendered.splitlines())}
    if not apply or rendered == original:
        return result
    backup = source_file.with_name(
        source_file.name + ".before-suite-merge-" +
        datetime.now().strftime("%Y%m%d-%H%M%S"))
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    descriptor, name = tempfile.mkstemp(prefix=".mirror-sources-",
                                        dir=include_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        os.chmod(temporary, source_file.stat().st_mode & 0o777)
        backup.write_bytes(source_file.read_bytes())
        os.chmod(backup, source_file.stat().st_mode & 0o777)
        os.replace(temporary, source_file)
    finally:
        temporary.unlink(missing_ok=True)
    result["applied"] = True
    result["backup"] = str(backup)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path("/etc/apt/mirror.list"))
    parser.add_argument("--include-dir", type=Path,
                        default=Path("/etc/apt/mirror.conf.d"))
    parser.add_argument("--apply", action="store_true",
                        help="write files; otherwise only validate and report")
    parser.add_argument("--merge-suites", action="store_true",
                        help="regroup an existing 20-repositories.sources")
    parser.add_argument("--restore-stub", action="store_true",
                        help="restore only mirror.list from existing include_dir")
    args = parser.parse_args(argv)
    if args.merge_suites and args.restore_stub:
        parser.error("--merge-suites and --restore-stub are mutually exclusive")
    try:
        action = (restore_stub if args.restore_stub else
                  merge_suites if args.merge_suites else migrate)
        result = action(args.config, args.include_dir, apply=args.apply)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"migration failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
