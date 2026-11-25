#!/usr/bin/env bash
# Rebuild <library>/library.xml from every finished ZIM in <library>/zim.
#   - files still downloading (a .aria2 control file next to them) are skipped
#   - when two editions of the same collection exist, only the newest is listed
#   - written to a temp file and renamed, so kiwix-serve (--monitorLibrary)
#     never sees a half-written catalog
# Run it after adding or removing ZIM files; fetch-zims.sh runs it for you.
# Library folder: $AEGIS_LIBRARY_DIR or ~/library.
set -euo pipefail
LIB="${AEGIS_LIBRARY_DIR:-$HOME/library}"
cd "$LIB"
tmp="$(mktemp "$LIB/.library.XXXXXX.xml")"
rm -f "$tmp"                                  # kiwix-manage wants to create it
trap 'rm -f "$tmp"' EXIT

declare -A newest=()
shopt -s nullglob
for f in zim/*.zim; do
  [ -f "$f.aria2" ] && continue
  base="$(basename "$f" .zim)"
  name="$(echo "$base" | sed -E 's/_[0-9]{4}-[0-9]{2}(-[0-9]{2})?$//')"
  cur="${newest[$name]:-}"
  if [ -z "$cur" ] || [[ "$f" > "$cur" ]]; then newest[$name]="$f"; fi
done

count=0
for name in $(printf '%s\n' "${!newest[@]}" | sort); do
  if kiwix-manage "$tmp" add "${newest[$name]}" >/dev/null 2>&1; then
    count=$((count + 1))
  else
    echo "skipped (unreadable): ${newest[$name]}" >&2
  fi
done
[ -f "$tmp" ] || echo '<?xml version="1.0" encoding="UTF-8"?><library version="20110515"></library>' > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$LIB/library.xml"
trap - EXIT
echo "library.xml: $count collections"
