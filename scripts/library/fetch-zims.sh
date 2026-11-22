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

declare -A TRIED=()
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
    next="$url"; break
  done < "$LIST"
  [ -z "$next" ] && break
  file="$(basename "${next%.meta4}")"
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
