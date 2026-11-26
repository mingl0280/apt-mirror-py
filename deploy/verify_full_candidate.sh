#!/bin/sh
set -eu

base=${1:-/sources/apt-mirror-py-full-candidate}
work=${2:-"$base/var/apt-verify"}
mkdir -p "$work/lists/partial" "$work/cache/archives/partial" "$work/empty"
cat > "$work/sources.list" <<EOF
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] file:$base/mirror/deb.debian.org/debian bookworm-backports contrib
deb [signed-by=/usr/share/keyrings/kali-archive-keyring.gpg] file:$base/mirror/http.kali.org/kali kali-rolling main
EOF

apt-get update \
  -o Dir::Etc::sourcelist="$work/sources.list" \
  -o Dir::Etc::sourceparts="$work/empty" \
  -o Dir::State::lists="$work/lists" \
  -o Dir::Cache::archives="$work/cache/archives" \
  -o APT::Get::List-Cleanup=0 \
  -o Acquire::Languages=none \
  -o Acquire::IndexTargets::deb::Contents-deb=false \
  -o Acquire::IndexTargets::deb::Contents-deb-legacy=false \
  -o Acquire::IndexTargets::deb::Contents-deb-src=false \
  -o APT::Sandbox::User=root

printf '\nAPT LIST FILES\n'
find "$work/lists" -maxdepth 1 -type f -printf '%f %s bytes\n' | head -15
printf '\nPACKAGE CANDIDATES\n'
apt-cache \
  -o Dir::Etc::sourcelist="$work/sources.list" \
  -o Dir::Etc::sourceparts="$work/empty" \
  -o Dir::State::lists="$work/lists" \
  policy diaspora-installer-mysql bluez-firmware

cd "$work"
apt-get \
  -o Dir::Etc::sourcelist="$work/sources.list" \
  -o Dir::Etc::sourceparts="$work/empty" \
  -o Dir::State::lists="$work/lists" \
  -o Dir::Cache::archives="$work/cache/archives" \
  -o APT::Sandbox::User=root \
  download diaspora-installer-mysql
printf '\nDOWNLOADED PACKAGE\n'
sha256sum diaspora-installer-mysql_*.deb
