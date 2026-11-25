#!/usr/bin/env bash
# Download the offline-library collections (ZIM files) listed in
# <library>/zim-list.txt, then refresh the Kiwix catalog and the PDF index.
#
#   - one URL per line, as given by `scripts/aegis-library catalog` (the
#     .meta4 metalink); everything after '#' is a comment
#   - aria2c follows the metalink: several mirrors, resumable, checksum-verified
#   - safe to re-run: finished files are skipped, partial ones resume
#   - the list is re-read after every file, so lines can be appended while it runs
#
#   - a collection is left for later when taking it would leave less than
#     $AEGIS_LIBRARY_MIN_FREE_GB (default 60) free on the library's disk
#
# Library folder: $AEGIS_LIBRARY_DIR or ~/library.
# Status: <library>/zim-status.txt     Log: <library>/zim-download.log
#
# Run it detached for a big list:
#   setsid nohup scripts/library/fetch-zims.sh > ~/library/zim-download.log 2>&1 &
set -u
LIB="${AEGIS_LIBRARY_DIR:-$HOME/library}"
LIST="$LIB/zim-list.txt"
STATUS="$LIB/zim-status.txt"
# readlink: the script is often run through a symlink in the library folder.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
mkdir -p "$LIB/zim"
[ -f "$LIST" ] || { echo "no list at $LIST" >&2; exit 2; }

note() { echo "$(date '+%F %T') $*" | tee -a "$STATUS"; }

refresh() {
  # New collections become visible to kiwix-serve (it watches library.xml)…
  "$HERE/rebuild-library.sh" 2>&1 | tee -a "$STATUS"
  # …and their PDFs searchable. Unchanged collections are skipped in a stat call.
  if [ -x "$REPO/venv/bin/python" ] && command -v zimdump >/dev/null 2>&1; then
    (cd "$REPO" && venv/bin/python scripts/aegis-library index 2>&1 | grep -v '^{' | tail -n 3) | tee -a "$STATUS"
  fi
}

MIN_FREE_GB="${AEGIS_LIBRARY_MIN_FREE_GB:-60}"

# Bytes still to download for a collection: its size (from the metalink) less
# what is already on disk. Empty when the size cannot be read.
remaining_bytes() {
  local url="$1" file="$2" size have=0
  size="$(curl -fsSL --max-time 30 "$url" 2>/dev/null | grep -o '<size>[0-9]*</size>' | head -n 1 | tr -dc '0-9')"
  [ -z "$size" ] && return 0
  [ -f "$LIB/zim/$file" ] && have="$(du -B1 "$LIB/zim/$file" 2>/dev/null | cut -f1)"
  echo $(( size > ${have:-0} ? size - ${have:-0} : 0 ))
}

# True when the disk keeps its floor after this download.
room_for() {
  local need="$1" free
  [ -z "$need" ] && return 0                   # size unknown: let aria2c find out
  free="$(df -B1 --output=avail "$LIB" | tail -n 1 | tr -dc '0-9')"
  [ $(( free - need )) -ge $(( MIN_FREE_GB * 1024 * 1024 * 1024 )) ]
}

declare -A TRIED=()
declare -A HELD=()
fetched=0
while :; do
  next=""
  while IFS= read -r line; do
    url="${line%%#*}"; url="$(echo "$url" | tr -d '[:space:]')"
    [ -z "$url" ] && continue
    file="$(basename "${url%.meta4}")"
    if [ -f "$LIB/zim/$file" ] && [ ! -f "$LIB/zim/$file.aria2" ]; then
      continue                      # already complete
    fi
    [ -n "${TRIED[$url]:-}" ] && [ "${TRIED[$url]}" -ge 3 ] && continue
    [ -n "${HELD[$url]:-}" ] && continue
    next="$url"; break
  done < "$LIST"
  [ -z "$next" ] && break
  file="$(basename "${next%.meta4}")"
  need="$(remaining_bytes "$next" "$file")"
  if ! room_for "$need"; then
    HELD[$next]=1
    note "HELD  $file — needs $(( need / 1073741824 )) GB more and would leave under ${MIN_FREE_GB} GB free; run again when there is room"
    continue
  fi
  TRIED[$next]=$(( ${TRIED[$next]:-0} + 1 ))
  note "START $file (attempt ${TRIED[$next]})"
  if aria2c --dir="$LIB/zim" --follow-metalink=mem --continue=true \
        --max-connection-per-server=4 --split=8 --min-split-size=64M \
        --check-integrity=true --file-allocation=none --auto-file-renaming=false \
        --allow-overwrite=false --summary-interval=120 --console-log-level=warn \
        --user-agent="aria2" "$next"; then
    note "OK    $file $(du -h "$LIB/zim/$file" 2>/dev/null | cut -f1)"
    fetched=$((fetched + 1))
    # Keep the library usable while a long list downloads.
    [ $((fetched % 10)) -eq 0 ] && refresh
  else
    note "FAIL  $file (aria2c exit $?)"
    sleep 20
  fi
done
refresh
note "DONE zim downloads"
df -h "$LIB" | tail -1 | tee -a "$STATUS"
