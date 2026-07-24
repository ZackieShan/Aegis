#!/usr/bin/env python3
"""Tag write-back: fill MISSING metadata in audio files from the organizer's
identified values (MusicBrainz-canonical after an online identify, tag/filename
-derived otherwise).

Safety model:
  * mode "missing" (default) only writes fields that are EMPTY in the file —
    existing tags are never touched. mode "all" also overwrites differing
    fields with the canonical values.
  * every change is recorded (old + new per field) in a JSON backup manifest
    before anything is written, and undo() restores the old values (including
    deleting fields that did not exist before).
  * after each write the tag-stripped audio payload hash must be unchanged
    (music_tags.payload_md5) — proving the audio bytes were untouched.

Uses mutagen (easy tags) when available; every entry point degrades to a clear
"unavailable" answer without it, so the stdlib-only organizer still runs.
"""
import os

try:
    import mutagen
    from mutagen import File as _MFile
except Exception:            # mutagen not installed -> feature unavailable
    mutagen = None
    _MFile = None

# rec field -> mutagen "easy" tag key
FIELD_MAP = {
    "artist": "artist",
    "albumartist": "albumartist",
    "album": "album",
    "title": "title",
    "year": "date",
    "genre": "genre",
    "trackno": "tracknumber",
    "discno": "discnumber",
}
# formats mutagen's easy-tag layer handles reliably
SUPPORTED_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a"}


def available():
    return mutagen is not None


def _easy(path):
    """Mutagen easy-tags object or None (unsupported/corrupt)."""
    try:
        f = _MFile(path, easy=True)
    except Exception:
        return None
    if f is None or getattr(f, "tags", None) is None:
        try:
            if f is not None:
                f.add_tags()
                return f
        except Exception:
            return None
    return f


def read_current(path):
    """{field: value} of the mapped fields as they exist ON DISK now."""
    f = _easy(path)
    if f is None:
        return None
    out = {}
    for field, key in FIELD_MAP.items():
        try:
            vals = f.get(key)
            out[field] = str(vals[0]) if vals else None
        except Exception:
            out[field] = None
    return out


def plan_changes(path, desired, mode="missing"):
    """{field: {"old": x, "new": y}} of what a fix would write, or None when
    the file can't be handled. Genre "Unclassified" is never written."""
    if os.path.splitext(path)[1].lower() not in SUPPORTED_EXTS:
        return None
    current = read_current(path)
    if current is None:
        return None
    changes = {}
    for field in FIELD_MAP:
        want = desired.get(field)
        if want in (None, "", 0):
            continue
        if field == "genre" and str(want).lower() in ("unclassified", "unknown"):
            continue
        want_s = str(want)
        have = current.get(field)
        if have is None or not str(have).strip():
            changes[field] = {"old": have, "new": want_s}
        elif mode == "all" and str(have).strip() != want_s:
            changes[field] = {"old": have, "new": want_s}
    return changes


def apply_changes(path, changes):
    """Write the planned field values. Returns None on success, else an error
    string. Callers verify payload_md5 around this."""
    f = _easy(path)
    if f is None:
        return "unsupported or unreadable file"
    try:
        for field, ch in changes.items():
            f[FIELD_MAP[field]] = str(ch["new"])
        f.save()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def revert_changes(path, changes):
    """Undo one file's changes: restore old values, delete fields that were
    absent before. Returns None on success, else an error string."""
    f = _easy(path)
    if f is None:
        return "unsupported or unreadable file"
    try:
        for field, ch in changes.items():
            key = FIELD_MAP[field]
            old = ch.get("old")
            if old is None or not str(old).strip():
                if key in f:
                    del f[key]
            else:
                f[key] = str(old)
        f.save()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
