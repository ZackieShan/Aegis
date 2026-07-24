#!/usr/bin/env python3
"""music_tags.py - Music Organizer: hand-written, stdlib-only audio parsers.

Contract with the Music Organizer core (music.py):

    read_tags(path)   -> dict with keys: artist, albumartist, album, title,
                         trackno, tracktotal, discno, disctotal, year, genre,
                         compilation (bool), has_art (bool).
                         Text/number fields are None when missing.
    tech_info(path)   -> dict with keys: codec, duration_s, bitrate_kbps,
                         vbr, samplerate, channels. None when unknown.
    payload_md5(path) -> md5 hex of the tag-stripped audio payload, or None.

Format coverage (all parsers hand-written, no external deps):
  MP3   ID3v2.2 / v2.3 / v2.4 (unsynchronisation, extended headers, footer,
        all four text encodings, TXXX, APIC presence, v2.4 per-frame flags),
        APEv2 footer, ID3v1 fallback. MPEG frame sync walk for tech info with
        Xing/Info/VBRI detection, else multi-frame bitrate-variance VBR call.
  FLAC  STREAMINFO + Vorbis comments + PICTURE blocks; duration from total
        samples, bitrate from the audio stream length.
  MP4   moov/udta/meta/ilst atoms (cnam cART aART calb trkn disk cday cgen
        gnre covr cpil), mvhd/mdhd duration, stsd codec (aac/alac/ac3).
  OGG   Vorbis & Opus identification + comment packets, granulepos duration.
  WAV   RIFF fmt/data chunks, LIST-INFO and "id3 " chunk tags.
  AIFF  FORM COMM/SSND chunks (80-bit extended sample rate), ID3 chunk tags.
  WMA   ASF header: file/stream properties, content description and extended
        content description objects (best effort).
  AAC   Raw ADTS frame sync walk (+ ID3v2 when prepended).

payload_md5 strips, per contract: MP3/AAC minus ID3v2 header + ID3v1/APEv2
footers; FLAC minus metadata blocks; MP4 = mdat payloads only. WAV/AIFF hash
their sound chunks; OGG hashes the whole stream (comments live inline);
WMA hashes the ASF data object. Unidentified garbage falls back to a
whole-file hash so the dedupe stage still gets a stable value.

Garbage in -> Nones/defaults out. Nothing in this module ever raises.
"""
import hashlib
import os
import re
import struct
import zlib

__all__ = ["read_tags", "tech_info", "payload_md5"]

# ---------------------------------------------------------------- constants

ID3_GENRES = [
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
    "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B",
    "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
    "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient",
    "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical",
    "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "AlternRock", "Bass", "Soul", "Punk", "Space", "Meditative",
    "Instrumental Pop", "Instrumental Rock", "Ethnic", "Gothic", "Darkwave",
    "Techno-Industrial", "Electronic", "Pop-Folk", "Eurodance", "Dream",
    "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40", "Christian Rap",
    "Pop/Funk", "Jungle", "Native American", "Cabaret", "New Wave",
    "Psychadelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal",
    "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll",
    "Hard Rock", "Folk", "Folk-Rock", "National Folk", "Swing",
    "Fast Fusion", "Bebob", "Latin", "Revival", "Celtic", "Bluegrass",
    "Avantgarde", "Gothic Rock", "Progressive Rock", "Psychedelic Rock",
    "Symphonic Rock", "Slow Rock", "Big Band", "Chorus", "Easy Listening",
    "Acoustic", "Humour", "Speech", "Chanson", "Opera", "Chamber Music",
    "Sonata", "Symphony", "Booty Bass", "Primus", "Porn Groove", "Satire",
    "Slow Jam", "Club", "Tango", "Samba", "Folklore", "Ballad",
    "Power Ballad", "Rhythmic Soul", "Freestyle", "Duet", "Punk Rock",
    "Drum Solo", "A capella", "Euro-House", "Dance Hall", "Goa",
    "Drum & Bass", "Club-House", "Hardcore", "Terror", "Indie", "BritPop",
    "Negerpunk", "Polsk Punk", "Beat", "Christian Gangsta", "Heavy Metal",
    "Black Metal", "Crossover", "Contemporary Christian", "Christian Rock",
    "Merengue", "Salsa", "Thrash Metal", "Anime", "JPop", "Synthpop",
]

# v2.2 three-letter frame ids -> v2.3/2.4 equivalents
_V22_FRAMES = {
    "TT2": "TIT2", "TP1": "TPE1", "TP2": "TPE2", "TAL": "TALB",
    "TRK": "TRCK", "TPA": "TPOS", "TYE": "TYER", "TCO": "TCON",
    "PIC": "APIC", "TCP": "TCMP", "TXX": "TXXX", "COM": "COMM",
    "ULT": "USLT", "TEN": "TENC", "TCR": "TCOP", "TOA": "TOPE",
    "TDA": "TDAT", "TIM": "TIME", "TRD": "TRDA", "TOR": "TORY",
}

# MPEG audio: bitrate kbps tables, index 1..14 (0=free, 15=bad)
_BR_V1 = {1: [None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
          2: [None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
          3: [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]}
_BR_V2 = {1: [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
          2: [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
          3: [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_SR = {"1": [44100, 48000, 32000], "2": [22050, 24000, 16000],
       "2.5": [11025, 12000, 8000]}
_ADTS_SR = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
            16000, 12000, 11025, 8000, 73500]

# ASF GUIDs as stored on disk (first three fields little-endian)
ASF_HEADER_GUID = bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C")
ASF_FILE_PROPS_GUID = bytes.fromhex("A1DCAB8C47A9CF118EE400C00C205365")
ASF_STREAM_PROPS_GUID = bytes.fromhex("9107DCB7B7A9CF118EE600C00C205365")
ASF_CONTENT_DESC_GUID = bytes.fromhex("3326B2758E66CF11A6D900AA0062CE6C")
ASF_EXT_CONTENT_GUID = bytes.fromhex("40A4D0D207E3D21197F000A0C95EA850")
ASF_DATA_GUID = bytes.fromhex("3626B2758E66CF11A6D900AA0062CE6C")
ASF_AUDIO_STREAM_GUID = bytes.fromhex("409E69F84D5BCF11A8FD00805F5C442B")

_FRAME_SCAN_CAP = 4 * 1024 * 1024      # walk at most 4MB of frames
_HASH_CHUNK = 1 << 20

_EXT_KIND = {".mp3": "mp3", ".flac": "flac", ".m4a": "mp4", ".mp4": "mp4",
             ".aac": "aac", ".ogg": "ogg", ".opus": "ogg", ".wma": "wma",
             ".wav": "wav", ".aiff": "aiff", ".aif": "aiff"}


def _blank_tags():
    return {"artist": None, "albumartist": None, "album": None,
            "title": None, "trackno": None, "tracktotal": None,
            "discno": None, "disctotal": None, "year": None,
            "genre": None, "compilation": False, "has_art": False}


def _blank_tech():
    return {"codec": None, "duration_s": None, "bitrate_kbps": None,
            "vbr": False, "samplerate": None, "channels": None}


def _fill(tags, key, value):
    """Fill a tag slot only when empty; bools are OR-ed."""
    if value is None:
        return
    if isinstance(tags.get(key), bool):
        tags[key] = tags[key] or bool(value)
    elif tags.get(key) is None:
        tags[key] = value


# ------------------------------------------------------------- small helpers

def _syncsafe(b):
    """4 syncsafe bytes -> int (7 bits per byte)."""
    if len(b) < 4:
        return 0
    return ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | \
           ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)


def _deunsync(b):
    """Undo ID3 unsynchronisation: every FF 00 becomes FF."""
    return b.replace(b"\xff\x00", b"\xff")


def _decode_text(payload):
    """Decode an ID3 text frame payload (encoding byte + text).

    Handles all four encodings (latin-1, UTF-16+BOM, UTF-16BE, UTF-8) and
    v2.4 multi-values separated by nulls (joined with '; ')."""
    if not payload:
        return None
    enc, raw = payload[0], payload[1:]
    try:
        if enc == 0:
            s = raw.decode("latin-1", "replace")
        elif enc == 1:
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                s = raw.decode("utf-16", "replace")
            else:
                s = raw.decode("utf-16-be", "replace")
        elif enc == 2:
            s = raw.decode("utf-16-be", "replace")
        elif enc == 3:
            s = raw.decode("utf-8", "replace")
        else:
            s = raw.decode("latin-1", "replace")
    except Exception:
        return None
    parts = [p.strip() for p in s.split("\x00")]
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


_YEAR_RE = re.compile(r"(\d{4})")


def _year_from(text):
    if not text:
        return None
    m = _YEAR_RE.search(str(text))
    if not m:
        return None
    y = int(m.group(1))
    return y if 1800 <= y <= 2100 else None


def _num_pair(text):
    """'3/10' -> (3, 10); '3' -> (3, None); junk -> (None, None)."""
    if not text:
        return None, None
    parts = str(text).strip().split("/", 1)

    def _num(s):
        s = s.strip()
        return int(s) if s.isdigit() else None

    num = _num(parts[0])
    total = _num(parts[1]) if len(parts) > 1 else None
    return num, total


def _truthy(text):
    return bool(text) and str(text).strip().lower() in ("1", "true", "yes")


def _genre_from(text):
    """Resolve ID3 genre text: '(17)', '(17)Rock', '17', 'RX' or plain."""
    if not text:
        return None
    t = str(text).strip()
    if not t:
        return None
    m = re.match(r"^\((\d{1,3})\)\s*(.*)$", t)
    if m:
        idx, rest = int(m.group(1)), m.group(2).strip()
        if rest:
            return rest
        return ID3_GENRES[idx] if 0 <= idx < len(ID3_GENRES) else None
    if t == "RX":
        return "Remix"
    if t == "CR":
        return "Cover"
    if t.isdigit():
        idx = int(t)
        return ID3_GENRES[idx] if 0 <= idx < len(ID3_GENRES) else None
    return t


def _sniff(path):
    """Best-effort format identification from magic bytes, then extension.

    Returns (kind, head_bytes) with kind in {mp3,flac,mp4,ogg,wav,aiff,wma,
    aac} or None."""
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except Exception:
        return None, b""
    ext = os.path.splitext(path)[1].lower()
    kind = None
    if head[:3] == b"ID3":
        kind = "aac" if ext == ".aac" else "mp3"
    elif head[:4] == b"fLaC":
        kind = "flac"
    elif head[4:8] == b"ftyp":
        kind = "mp4"
    elif head[:4] == b"OggS":
        kind = "ogg"
    elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        kind = "wav"
    elif head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        kind = "aiff"
    elif head[:16] == ASF_HEADER_GUID:
        kind = "wma"
    elif len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        kind = "aac"                                # ADTS sync, layer bits 0
    elif len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        kind = "mp3"                                # MPEG frame sync
    if kind is None:
        kind = _EXT_KIND.get(ext)
    return kind, head


def _hash_ranges(path, spans):
    """md5 over (offset, length) byte ranges; None when nothing hashed."""
    h = hashlib.md5()
    total = 0
    with open(path, "rb") as f:
        for start, length in spans:
            if length is None or length <= 0 or start is None or start < 0:
                continue
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(_HASH_CHUNK, left))
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
                left -= len(chunk)
    return h.hexdigest() if total else None


def _br_int(bps):
    """bits/second -> kbps int; None when it would round to 0 (a file with
    no meaningful audio payload reports 'unknown', not '0 kbps')."""
    try:
        v = int(round(bps / 1000.0))
    except Exception:
        return None
    return v if v > 0 else None


# ==================================================================== ID3v2

def _parse_id3v2(data):
    """Parse an ID3v2 tag at the start of `data`.

    Returns (frames, total_tag_length). frames maps the (normalised, 4-char)
    frame id -> raw payload bytes with unsynchronisation already undone.
    Returns (None, 0) when no tag is present."""
    if len(data) < 10 or data[:3] != b"ID3":
        return None, 0
    major = data[3]
    if major not in (2, 3, 4) or data[4] == 0xFF:
        return None, 0
    flags = data[5]
    size = _syncsafe(data[6:10])
    total = 10 + size
    if major == 4 and (flags & 0x10):               # footer present
        total += 10
    body = data[10:10 + size]
    if flags & 0x80:                                # tag-level unsync
        body = _deunsync(body)
    if flags & 0x40 and len(body) >= 4:             # extended header
        try:
            if major == 3:
                ext = struct.unpack(">I", body[:4])[0]
                body = body[4 + ext:]               # v2.3: size excludes itself
            elif major == 4:
                ext = _syncsafe(body[:4])
                body = body[ext:]                   # v2.4: size includes itself
        except Exception:
            pass
    frames = {}
    pos = 0
    while pos < len(body):
        if major == 2:
            if pos + 6 > len(body):
                break
            fid = body[pos:pos + 3]
            if fid == b"\x00\x00\x00":
                break
            fsize = int.from_bytes(body[pos + 3:pos + 6], "big")
            hlen, fflags = 6, b""
        else:
            if pos + 10 > len(body):
                break
            fid = body[pos:pos + 4]
            if fid == b"\x00\x00\x00\x00":
                break
            if major == 4:
                fsize = _syncsafe(body[pos + 4:pos + 8])
            else:
                fsize = struct.unpack(">I", body[pos + 4:pos + 8])[0]
            fflags = body[pos + 8:pos + 10]
            hlen = 10
        if pos + hlen + fsize > len(body):          # truncated final frame
            payload = body[pos + hlen:]
            fsize = max(0, len(body) - pos - hlen)
        else:
            payload = body[pos + hlen:pos + hlen + fsize]
        if major == 4 and len(fflags) == 2:
            if fflags[1] & 0x02:                    # per-frame unsync
                payload = _deunsync(payload)
            if fflags[1] & 0x08:                    # zlib compression
                try:
                    payload = zlib.decompress(payload)
                except Exception:
                    payload = b""
            if fflags[1] & 0x01 and len(payload) >= 4:  # data length indicator
                payload = payload[4:]
        name = fid.decode("latin-1", "replace")
        if major == 2:
            name = _V22_FRAMES.get(name, name)
        frames[name] = payload
        pos += hlen + fsize
    return frames, total


def _tags_from_id3(frames, tags):
    """Fill `tags` in place from a parsed ID3 frame dict."""
    if not frames:
        return

    def text(name):
        p = frames.get(name)
        return _decode_text(p) if p else None

    _fill(tags, "title", text("TIT2"))
    _fill(tags, "artist", text("TPE1"))
    _fill(tags, "albumartist", text("TPE2"))
    _fill(tags, "album", text("TALB"))
    num, total = _num_pair(text("TRCK"))
    _fill(tags, "trackno", num)
    _fill(tags, "tracktotal", total)
    num, total = _num_pair(text("TPOS"))
    _fill(tags, "discno", num)
    _fill(tags, "disctotal", total)
    year = _year_from(text("TDRC")) or _year_from(text("TYER")) or \
        _year_from(text("TORY")) or _year_from(text("TDAT"))
    _fill(tags, "year", year)
    _fill(tags, "genre", _genre_from(text("TCON")))
    if _truthy(text("TCMP")):
        tags["compilation"] = True
    if frames.get("APIC") or frames.get("PIC"):
        tags["has_art"] = True


def _parse_id3v1(raw):
    """128-byte ID3v1 block -> partial tags dict, or None."""
    if len(raw) != 128 or raw[:3] != b"TAG":
        return None

    def s(b):
        v = b.decode("latin-1", "replace").strip("\x00").strip()
        return v or None

    out = {"title": s(raw[3:33]), "artist": s(raw[33:63]),
           "album": s(raw[63:93]), "year": _year_from(s(raw[93:97])),
           "genre": None, "trackno": None}
    g = raw[127]
    if g < len(ID3_GENRES):
        out["genre"] = ID3_GENRES[g]
    if raw[125] == 0 and raw[126] != 0:
        out["trackno"] = raw[126]
    return out


# ==================================================================== APEv2

def _apev2_items(f, size):
    """Parse an APEv2 tag from the file tail.

    Returns (items, strip_start): items maps lowercased key -> (flags, value
    bytes); strip_start is the file offset where the whole tag begins
    (header included) so payload hashing can drop it. (None, None) if absent."""
    if size < 32:
        return None, None
    has_id3v1 = False
    if size >= 128:
        f.seek(size - 128)
        if f.read(3) == b"TAG":
            has_id3v1 = True
    footer_end = size - 128 if has_id3v1 else size
    if footer_end < 32:
        return None, None
    f.seek(footer_end - 32)
    foot = f.read(32)
    if len(foot) < 32 or foot[:8] != b"APETAGEX":
        return None, None
    tsize = struct.unpack("<I", foot[12:16])[0]
    count = struct.unpack("<I", foot[16:20])[0]
    tflags = struct.unpack("<I", foot[20:24])[0]
    items_start = footer_end - tsize               # size excludes the header
    if items_start < 0 or tsize < 32:
        return None, None
    strip_start = items_start
    if tflags & 0x80000000 and items_start >= 32:   # header present: the
        strip_start = items_start - 32              # stored size excludes it,
    f.seek(items_start)                             # but items still start here
    items = {}
    for _ in range(min(count, 1000)):
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        vlen, iflags = struct.unpack("<II", hdr)
        key = b""
        while True:
            c = f.read(1)
            if not c or c == b"\x00" or len(key) > 128:
                break
            key += c
        if vlen > 16 * 1024 * 1024:
            break
        val = f.read(vlen)
        if len(val) < vlen:
            break
        items[key.decode("ascii", "replace").lower()] = (iflags, val)
    return items, strip_start


def _tags_from_ape(items, tags):
    if not items:
        return

    def txt(*names):
        for n in names:
            it = items.get(n)
            if it and (it[0] & 0x6) == 0:           # item type 0: UTF-8 text
                v = it[1].decode("utf-8", "replace").split("\x00")[0].strip()
                if v:
                    return v
        return None

    _fill(tags, "artist", txt("artist"))
    _fill(tags, "albumartist", txt("album artist", "albumartist"))
    _fill(tags, "album", txt("album"))
    _fill(tags, "title", txt("title"))
    num, total = _num_pair(txt("track"))
    _fill(tags, "trackno", num)
    _fill(tags, "tracktotal", total)
    num, total = _num_pair(txt("disc"))
    _fill(tags, "discno", num)
    _fill(tags, "disctotal", total)
    _fill(tags, "year", _year_from(txt("year", "date")))
    _fill(tags, "genre", txt("genre"))
    if _truthy(txt("compilation")):
        tags["compilation"] = True
    for key, (iflags, val) in items.items():
        if key.startswith("cover art") and (iflags & 0x6) == 2 and val:
            tags["has_art"] = True
            break


# =========================================================== vorbis comments

def _parse_vorbis_comment(data):
    """Vorbis/FLAC/Opus comment block body -> {UPPERKEY: [values...]}."""
    out = {}
    if len(data) < 8:
        return out
    vlen = struct.unpack("<I", data[:4])[0]
    pos = 4 + vlen
    if pos + 4 > len(data):
        return out
    count = struct.unpack("<I", data[pos:pos + 4])[0]
    pos += 4
    for _ in range(min(count, 2000)):
        if pos + 4 > len(data):
            break
        ln = struct.unpack("<I", data[pos:pos + 4])[0]
        pos += 4
        if pos + ln > len(data):
            break
        entry = data[pos:pos + ln].decode("utf-8", "replace")
        pos += ln
        if "=" in entry:
            k, v = entry.split("=", 1)
            out.setdefault(k.strip().upper(), []).append(v)
    return out


def _tags_from_vorbis(comments, tags):
    if not comments:
        return

    def first(*names):
        for n in names:
            vals = comments.get(n)
            for v in vals or []:
                if v.strip():
                    return v.strip()
        return None

    _fill(tags, "artist", first("ARTIST"))
    _fill(tags, "albumartist", first("ALBUMARTIST", "ALBUM ARTIST",
                                     "ALBUM_ARTIST", "ENSEMBLE"))
    _fill(tags, "album", first("ALBUM"))
    _fill(tags, "title", first("TITLE"))
    num, total = _num_pair(first("TRACKNUMBER"))
    _fill(tags, "trackno", num)
    if total is None:
        total = _num_pair(first("TRACKTOTAL", "TOTALTRACKS"))[0]
    _fill(tags, "tracktotal", total)
    num, total = _num_pair(first("DISCNUMBER"))
    _fill(tags, "discno", num)
    if total is None:
        total = _num_pair(first("DISCTOTAL", "TOTALDISCS"))[0]
    _fill(tags, "disctotal", total)
    _fill(tags, "year", _year_from(first("DATE", "YEAR")))
    _fill(tags, "genre", first("GENRE"))
    if _truthy(first("COMPILATION")):
        tags["compilation"] = True
    if first("METADATA_BLOCK_PICTURE") or first("COVERART"):
        tags["has_art"] = True


# ====================================================================== MP3

def _mp3_bounds(path):
    """(audio_start, audio_end, file_size) with ID3v2 head and ID3v1/APEv2
    tail stripped. Works for raw AAC/ADTS too."""
    size = os.path.getsize(path)
    start = 0
    end = size
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) == 10 and head[:3] == b"ID3" and head[3] in (2, 3, 4):
            start = 10 + _syncsafe(head[6:10])
            if head[3] == 4 and (head[5] & 0x10):       # v2.4 footer
                start += 10
            start = min(start, size)
        if end - start >= 128:
            f.seek(end - 128)
            if f.read(3) == b"TAG":                     # ID3v1
                end -= 128
        if end - 32 >= start:
            f.seek(end - 32)
            foot = f.read(32)
            if len(foot) == 32 and foot[:8] == b"APETAGEX":
                tsize = struct.unpack("<I", foot[12:16])[0]
                tflags = struct.unpack("<I", foot[20:24])[0]
                new_end = end - tsize
                if tflags & 0x80000000:                 # header present
                    new_end -= 32
                if start <= new_end < end:
                    end = new_end
    return start, end, size


def _mpeg_hdr(b):
    """Parse 4 MPEG audio header bytes. Returns dict or None."""
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return None
    ver_id = (b[1] >> 3) & 3
    layer_id = (b[1] >> 1) & 3
    if ver_id == 1 or layer_id == 0:
        return None
    ver = {0: "2.5", 2: "2", 3: "1"}[ver_id]
    layer = {1: 3, 2: 2, 3: 1}[layer_id]
    br_idx = (b[2] >> 4) & 0xF
    sr_idx = (b[2] >> 2) & 3
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    pad = (b[2] >> 1) & 1
    table = _BR_V1 if ver == "1" else _BR_V2
    br = table[layer][br_idx]
    if br is None:
        return None
    sr = _SR[ver][sr_idx]
    chan_mode = (b[3] >> 6) & 3
    channels = 1 if chan_mode == 3 else 2
    if layer == 1:
        flen = (12 * br * 1000 // sr + pad) * 4
        spf = 384
    elif layer == 2:
        flen = 144 * br * 1000 // sr + pad
        spf = 1152
    else:
        coef = 144 if ver == "1" else 72
        flen = coef * br * 1000 // sr + pad
        spf = 1152 if ver == "1" else 576
    if flen < 4:
        return None
    return {"version": ver, "layer": layer, "bitrate": br, "samplerate": sr,
            "channels": channels, "flen": flen, "spf": spf,
            "crc": 0 if (b[1] & 1) else 2}


def _find_first_frame(buf):
    """Scan for a plausible MPEG frame in buf; validate with the next frame's
    sync when it is inside the buffer. Returns (offset, header) or (None, *)."""
    i = 0
    limit = len(buf) - 4
    while i < limit:
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            h = _mpeg_hdr(buf[i:i + 4])
            if h:
                nxt = i + h["flen"]
                if nxt + 1 < len(buf):
                    if buf[nxt] == 0xFF and (buf[nxt + 1] & 0xE0) == 0xE0 and \
                            _mpeg_hdr(buf[nxt:nxt + 4]):
                        return i, h
                else:
                    return i, h               # next frame beyond buffer: accept
        i += 1
    return None, None


def _xing_info(buf, off, hdr):
    """Check the first frame for a Xing/Info header.

    Returns (frames, bytes_total, is_vbr_marker) or None."""
    if hdr["version"] == "1":
        side = 32 if hdr["channels"] == 2 else 17
    else:
        side = 17 if hdr["channels"] == 2 else 9
    at = off + 4 + hdr["crc"] + side
    if at + 8 > len(buf):
        return None
    marker = buf[at:at + 4]
    if marker not in (b"Xing", b"Info"):
        return None
    flags = struct.unpack(">I", buf[at + 4:at + 8])[0]
    pos = at + 8
    frames = bytes_total = None
    if flags & 0x1 and pos + 4 <= len(buf):
        frames = struct.unpack(">I", buf[pos:pos + 4])[0]
        pos += 4
    if flags & 0x2 and pos + 4 <= len(buf):
        bytes_total = struct.unpack(">I", buf[pos:pos + 4])[0]
        pos += 4
    return frames, bytes_total, marker == b"Xing"


def _vbri_info(buf, off):
    """VBRI sits 32 bytes after the 4-byte header of the first frame."""
    at = off + 36
    if at + 18 > len(buf) or buf[at:at + 4] != b"VBRI":
        return None
    bytes_total = struct.unpack(">I", buf[at + 10:at + 14])[0]
    frames = struct.unpack(">I", buf[at + 14:at + 18])[0]
    return frames, bytes_total, True


def _mp3_tech(path, start, end, size):
    tech = _blank_tech()
    region = end - start
    if region < 4:
        return tech
    with open(path, "rb") as f:
        f.seek(start)
        buf = f.read(min(region, _FRAME_SCAN_CAP))
    off, hdr = _find_first_frame(buf)
    if hdr is None:
        return tech
    tech["codec"] = "mp3" if hdr["layer"] == 3 else "mp%d" % hdr["layer"]
    tech["samplerate"] = hdr["samplerate"]
    tech["channels"] = hdr["channels"]
    audio_bytes = region - off

    xing = _xing_info(buf, off, hdr)
    if xing is None:
        xing = _vbri_info(buf, off)
    if xing is not None:
        frames, bytes_total, is_vbr = xing
        tech["vbr"] = bool(is_vbr)
        if frames:
            dur = frames * hdr["spf"] / hdr["samplerate"]
            tech["duration_s"] = round(dur, 3)
            total = bytes_total if bytes_total else audio_bytes
            if dur > 0:
                tech["bitrate_kbps"] = _br_int(total * 8 / dur)
            return tech

    # No Xing/VBRI: walk frames and look at bitrate variance.
    pos = off
    n = 0
    br_counts = {}
    while pos + 4 <= len(buf):
        h = _mpeg_hdr(buf[pos:pos + 4])
        if not h:
            break
        br_counts[h["bitrate"]] = br_counts.get(h["bitrate"], 0) + 1
        n += 1
        pos += h["flen"]
    if n == 0:
        return tech
    reached_eof = (len(buf) == region) and (pos >= len(buf) - 3)
    tech["vbr"] = len(br_counts) > 1
    if reached_eof:
        dur = n * hdr["spf"] / hdr["samplerate"]
        tech["duration_s"] = round(dur, 3)
        if dur > 0:
            tech["bitrate_kbps"] = _br_int(audio_bytes * 8 / dur)
    else:
        # Large file: estimate from the sampled frames.
        avg_br = sum(b * c for b, c in br_counts.items()) / sum(br_counts.values())
        if avg_br > 0:
            dur = audio_bytes * 8 / (avg_br * 1000)
            tech["duration_s"] = round(dur, 3)
            tech["bitrate_kbps"] = int(round(avg_br))
    return tech


def _mp3_parse(path):
    """Returns (tags, tech, (audio_start, audio_end))."""
    tags = _blank_tags()
    start, end, size = _mp3_bounds(path)
    try:
        with open(path, "rb") as f:
            f.seek(0)
            head = f.read(min(size, 256 * 1024))
            frames, _ = _parse_id3v2(head)
            _tags_from_id3(frames, tags)
            items, _ = _apev2_items(f, size)
            _tags_from_ape(items, tags)
            if size >= 128:
                f.seek(size - 128)
                v1 = _parse_id3v1(f.read(128))
                if v1:
                    for k, v in v1.items():
                        _fill(tags, k, v)
    except Exception:
        pass
    tech = _mp3_tech(path, start, end, size)
    return tags, tech, (start, end)


# ===================================================================== FLAC

def _flac_audio_start(path):
    """Offset of the first audio frame (after the metadata blocks)."""
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            return None
        while True:
            hb = f.read(1)
            if len(hb) < 1:
                return None
            lb = f.read(3)
            if len(lb) < 3:
                return None
            length = int.from_bytes(lb, "big")
            f.seek(length, os.SEEK_CUR)
            if hb[0] & 0x80:                        # last metadata block
                return f.tell()


def _flac_parse(path):
    """Returns (tags, tech, audio_start)."""
    tags = _blank_tags()
    tech = _blank_tech()
    audio_start = None
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(4) == b"fLaC":
            while True:
                hb = f.read(1)
                if len(hb) < 1:
                    break
                lb = f.read(3)
                if len(lb) < 3:
                    break
                btype = hb[0] & 0x7F
                last = bool(hb[0] & 0x80)
                length = int.from_bytes(lb, "big")
                data = f.read(length)
                if btype == 0 and len(data) >= 34:  # STREAMINFO
                    packed = int.from_bytes(data[10:18], "big")
                    rate = (packed >> 44) & 0xFFFFF
                    chans = ((packed >> 41) & 0x7) + 1
                    total = packed & 0xFFFFFFFFF
                    tech["codec"] = "flac"
                    tech["vbr"] = True              # lossless frames vary
                    if rate:
                        tech["samplerate"] = rate
                        tech["channels"] = chans
                        if total:
                            dur = total / rate
                            tech["duration_s"] = round(dur, 3)
                elif btype == 4 and len(data) >= 8:  # VORBIS_COMMENT
                    _tags_from_vorbis(_parse_vorbis_comment(data), tags)
                elif btype == 6:                     # PICTURE
                    tags["has_art"] = True
                if last:
                    audio_start = f.tell()
                    break
    if tech.get("duration_s") and audio_start is not None:
        audio_bytes = max(0, size - audio_start)
        dur = tech["duration_s"]
        if dur > 0:
            tech["bitrate_kbps"] = _br_int(audio_bytes * 8 / dur)
    return tags, tech, audio_start


# ====================================================================== MP4

_MP4_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta")


def _mp4_children(f, start, end):
    """Yield (type_bytes, payload_offset, payload_length) for atoms in
    [start, end). Handles 64-bit sizes and size==0 (to end)."""
    pos = start
    while pos + 8 <= end:
        try:
            f.seek(pos)
            hdr = f.read(8)
        except Exception:
            return
        if len(hdr) < 8:
            return
        size = struct.unpack(">I", hdr[:4])[0]
        atyp = hdr[4:8]
        hlen = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield atyp, pos + hlen, size - hlen
        pos += size


def _mp4_find(f, start, end, wanted):
    for atyp, off, ln in _mp4_children(f, start, end):
        if atyp == wanted:
            return off, ln
    return None, None


def _mp4_mvhd_duration(payload):
    if len(payload) < 4:
        return None
    ver = payload[0]
    try:
        if ver == 1:
            if len(payload) < 32:
                return None
            ts = struct.unpack(">I", payload[20:24])[0]
            dur = struct.unpack(">Q", payload[24:32])[0]
        else:
            if len(payload) < 20:
                return None
            ts = struct.unpack(">I", payload[12:16])[0]
            dur = struct.unpack(">I", payload[16:20])[0]
    except Exception:
        return None
    if ts:
        return dur / ts
    return None


def _mp4_stsd(f, off, ln, tech):
    """First sample entry -> codec (+ channels/samplerate for mp4a/alac)."""
    data = b""
    try:
        f.seek(off)
        data = f.read(min(ln, 4096))
    except Exception:
        return
    if len(data) < 16:
        return
    entry = data[8:]                                 # skip version/flags+count
    if len(entry) < 8:
        return
    fmt = entry[4:8]
    codec = {b"mp4a": "aac", b"alac": "alac", b"ac-3": "ac3",
             b"ec-3": "eac3", b"Opus": "opus", b"fLaC": "flac"}.get(fmt)
    if codec is None:
        try:
            codec = fmt.decode("ascii", "replace").strip("\x00 ") or None
        except Exception:
            codec = None
    if tech.get("codec") is None:
        tech["codec"] = codec
    # AudioSampleEntry: 6 res + 2 dref + 8 res, then ch(2) bits(2) ...(4)
    if len(entry) >= 36 and fmt in (b"mp4a", b"alac"):
        try:
            tech["channels"] = struct.unpack(">H", entry[24:26])[0] or None
            rate = struct.unpack(">I", entry[32:36])[0] >> 16
            tech["samplerate"] = rate or None
        except Exception:
            pass


def _mp4_ilst(f, off, ln, tags):
    for item_typ, ioff, iln in _mp4_children(f, off, off + ln):
        dtype = None
        payload = None
        for dat_typ, doff, dln in _mp4_children(f, ioff, ioff + iln):
            if dat_typ != b"data" or dln < 8:
                continue
            try:
                f.seek(doff)
                dhdr = f.read(8)
                dtype = struct.unpack(">I", dhdr[:4])[0] & 0xFFFFFF
                payload = f.read(dln - 8)
            except Exception:
                payload = None
            break
        if payload is None:
            continue

        def text():
            return payload.decode("utf-8", "replace").strip("\x00").strip() or None

        if item_typ == b"\xa9nam":
            _fill(tags, "title", text())
        elif item_typ == b"\xa9ART":
            _fill(tags, "artist", text())
        elif item_typ == b"aART":
            _fill(tags, "albumartist", text())
        elif item_typ == b"\xa9alb":
            _fill(tags, "album", text())
        elif item_typ == b"\xa9day":
            _fill(tags, "year", _year_from(text()))
        elif item_typ == b"\xa9gen":
            _fill(tags, "genre", text())
        elif item_typ == b"gnre" and len(payload) >= 2:
            idx = struct.unpack(">H", payload[:2])[0] - 1
            if 0 <= idx < len(ID3_GENRES):
                _fill(tags, "genre", ID3_GENRES[idx])
        elif item_typ == b"trkn" and len(payload) >= 6:
            num = struct.unpack(">H", payload[2:4])[0] or None
            tot = struct.unpack(">H", payload[4:6])[0] or None
            _fill(tags, "trackno", num)
            _fill(tags, "tracktotal", tot)
        elif item_typ == b"disk" and len(payload) >= 6:
            num = struct.unpack(">H", payload[2:4])[0] or None
            tot = struct.unpack(">H", payload[4:6])[0] or None
            _fill(tags, "discno", num)
            _fill(tags, "disctotal", tot)
        elif item_typ == b"cpil" and len(payload) >= 1:
            if payload[-1] != 0:
                tags["compilation"] = True
        elif item_typ == b"covr":
            tags["has_art"] = True


def _mp4_mdat_spans(path):
    """Payload spans of every top-level mdat atom."""
    spans = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        for atyp, off, ln in _mp4_children(f, 0, size):
            if atyp == b"mdat":
                spans.append((off, ln))
    return spans


def _mp4_parse(path):
    """Returns (tags, tech, mdat_spans)."""
    tags = _blank_tags()
    tech = _blank_tech()
    spans = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        moov = None
        for atyp, off, ln in _mp4_children(f, 0, size):
            if atyp == b"mdat":
                spans.append((off, ln))
            elif atyp == b"moov":
                moov = (off, ln)
        if moov:
            off, ln = moov
            mvhd_off, mvhd_ln = _mp4_find(f, off, off + ln, b"mvhd")
            if mvhd_off is not None:
                f.seek(mvhd_off)
                dur = _mp4_mvhd_duration(f.read(min(mvhd_ln, 64)))
                if dur:
                    tech["duration_s"] = round(dur, 3)
            for ttyp, toff, tln in _mp4_children(f, off, off + ln):
                if ttyp == b"udta":
                    moff, mln = _mp4_find(f, toff, toff + tln, b"meta")
                    if moff is not None and mln >= 4:
                        # meta payload: 4 bytes version/flags, then ilst atom
                        ioff, iln = _mp4_find(f, moff + 4, moff + mln, b"ilst")
                        if ioff is not None:
                            _mp4_ilst(f, ioff, iln, tags)
                elif ttyp == b"trak":
                    moff, mln = _mp4_find(f, toff, toff + tln, b"mdia")
                    if moff is None:
                        continue
                    doff, dln = _mp4_find(f, moff, moff + mln, b"mdhd")
                    if doff is not None:
                        f.seek(doff)
                        dur = _mp4_mvhd_duration(f.read(min(dln, 64)))
                        if dur:
                            tech["duration_s"] = round(dur, 3)
                    ioff, iln = _mp4_find(f, moff, moff + mln, b"minf")
                    if ioff is None:
                        continue
                    soff, sln = _mp4_find(f, ioff, ioff + iln, b"stbl")
                    if soff is None:
                        continue
                    xoff, xln = _mp4_find(f, soff, soff + sln, b"stsd")
                    if xoff is not None:
                        _mp4_stsd(f, xoff, xln, tech)
    if tech.get("duration_s") and spans:
        total = sum(l for _, l in spans)
        dur = tech["duration_s"]
        if dur > 0:
            tech["bitrate_kbps"] = _br_int(total * 8 / dur)
    return tags, tech, spans


# ====================================================================== OGG

def _ogg_packets(f, cap=512 * 1024):
    """Yield assembled packets from the start of an Ogg stream."""
    cur = b""
    read = 0
    while read < cap:
        hdr = f.read(27)
        if len(hdr) < 27 or hdr[:4] != b"OggS":
            return
        nseg = hdr[26]
        segtab = f.read(nseg)
        if len(segtab) < nseg:
            return
        size = sum(segtab)
        payload = f.read(size)
        if len(payload) < size:
            return
        read += 27 + nseg + size
        off = 0
        for seg in segtab:
            cur += payload[off:off + seg]
            off += seg
            if seg < 255:
                yield cur
                cur = b""


def _ogg_last_granule(path, size):
    """Granule position of the last page (backwards scan of the tail)."""
    tail = min(size, 256 * 1024)
    with open(path, "rb") as f:
        f.seek(size - tail)
        buf = f.read(tail)
    pos = len(buf)
    while True:
        idx = buf.rfind(b"OggS", 0, pos)
        if idx < 0 or idx + 14 > len(buf):
            return None
        granule = struct.unpack("<q", buf[idx + 6:idx + 14])[0]
        if granule >= 0:
            return granule
        pos = idx


def _ogg_parse(path):
    """Returns (tags, tech) for Vorbis and Opus streams."""
    tags = _blank_tags()
    tech = _blank_tech()
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        packets = []
        try:
            for pkt in _ogg_packets(f):
                packets.append(pkt)
                if len(packets) >= 4:
                    break
        except Exception:
            pass
    if not packets:
        return tags, tech
    first = packets[0]
    is_vorbis = first.startswith(b"\x01vorbis")
    is_opus = first.startswith(b"OpusHead")
    comment = None
    preskip = 0
    if is_vorbis and len(first) >= 30:
        tech["codec"] = "vorbis"
        tech["channels"] = first[11] or None
        tech["samplerate"] = struct.unpack("<I", first[12:16])[0] or None
        nominal = struct.unpack("<i", first[20:24])[0]
        if nominal > 0:
            tech["bitrate_kbps"] = _br_int(nominal)
        tech["vbr"] = True
        for pkt in packets[1:]:
            if pkt.startswith(b"\x03vorbis"):
                comment = pkt[7:]
                break
    elif is_opus and len(first) >= 19:
        tech["codec"] = "opus"
        tech["channels"] = first[9] or None
        # Opus timestamps always run at 48kHz regardless of the input rate.
        tech["samplerate"] = 48000
        preskip = struct.unpack("<H", first[10:12])[0]
        tech["vbr"] = True
        for pkt in packets[1:]:
            if pkt.startswith(b"OpusTags"):
                comment = pkt[8:]
                break
    else:
        return tags, tech
    if comment:
        _tags_from_vorbis(_parse_vorbis_comment(comment), tags)
    granule = _ogg_last_granule(path, size)
    if granule is not None:
        if is_opus:
            dur = max(0, granule - preskip) / 48000
        elif tech.get("samplerate"):
            dur = granule / tech["samplerate"]
        else:
            dur = 0
        if dur > 0:
            tech["duration_s"] = round(dur, 3)
    if tech.get("bitrate_kbps") is None and tech.get("duration_s"):
        tech["bitrate_kbps"] = _br_int(size * 8 / tech["duration_s"])
    return tags, tech


# ====================================================================== WAV

def _wav_parse(path):
    """Returns (tags, tech, data_span)."""
    tags = _blank_tags()
    tech = _blank_tech()
    data_span = None
    fmt = None
    list_info = None
    id3_data = None
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            return tags, tech, None
        size = os.path.getsize(path)
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4]
            clen = struct.unpack("<I", hdr[4:8])[0]
            body = b""
            if cid in (b"fmt ", b"LIST", b"id3 ", b"ID3 ") and clen <= 8 * 1024 * 1024:
                body = f.read(clen)
            if cid == b"fmt ":
                fmt = body
            elif cid == b"data":
                data_span = (pos + 8, clen)
            elif cid == b"LIST" and body[:4] == b"INFO" and list_info is None:
                list_info = body[4:]
            elif cid in (b"id3 ", b"ID3 ") and id3_data is None:
                id3_data = body
            pos += 8 + clen + (clen & 1)
    # ID3 tags are authoritative; LIST-INFO only fills the gaps.
    if id3_data is not None:
        frames, _ = _parse_id3v2(id3_data)
        _tags_from_id3(frames, tags)
    if list_info is not None:
        _wav_list_info(list_info, tags)
    if fmt is not None and len(fmt) >= 16:
        audio_fmt, channels, rate, byterate = struct.unpack("<HHII", fmt[:12])
        if audio_fmt == 0xFFFE and len(fmt) >= 26:
            audio_fmt = struct.unpack("<H", fmt[24:26])[0]
        tech["codec"] = "wav"
        tech["vbr"] = False
        tech["samplerate"] = rate or None
        tech["channels"] = channels or None
        if byterate:
            tech["bitrate_kbps"] = _br_int(byterate * 8)
            if data_span:
                tech["duration_s"] = round(data_span[1] / byterate, 3)
    return tags, tech, data_span


_WAV_INFO_MAP = {"IART": "artist", "INAM": "title", "IPRD": "album",
                 "IGNR": "genre", "IPAL": "albumartist"}


def _wav_list_info(data, tags):
    pos = 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        clen = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        raw = data[pos + 8:pos + 8 + clen]
        val = raw.decode("utf-8", "replace").strip("\x00").strip() or None
        key = None
        try:
            key = cid.decode("ascii")
        except Exception:
            key = None
        if key in _WAV_INFO_MAP and val:
            _fill(tags, _WAV_INFO_MAP[key], val)
        elif key == "ITRK" and val:
            num, total = _num_pair(val)
            _fill(tags, "trackno", num)
            _fill(tags, "tracktotal", total)
        elif key == "ICRD" and val:
            _fill(tags, "year", _year_from(val))
        pos += 8 + clen + (clen & 1)


# ===================================================================== AIFF

def _extended80(b):
    """10-byte 80-bit IEEE extended float -> float."""
    if len(b) < 10:
        return 0.0
    expon = ((b[0] & 0x7F) << 8) | b[1]
    hi = int.from_bytes(b[2:6], "big")
    lo = int.from_bytes(b[6:10], "big")
    if expon == 0 and hi == 0 and lo == 0:
        return 0.0
    f = hi * 2.0 ** (expon - 16383 - 31) + lo * 2.0 ** (expon - 16383 - 63)
    return -f if (b[0] & 0x80) else f


def _aiff_parse(path):
    """Returns (tags, tech, ssnd_span)."""
    tags = _blank_tags()
    tech = _blank_tech()
    ssnd = None
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"FORM" or \
                head[8:12] not in (b"AIFF", b"AIFC"):
            return tags, tech, None
        size = os.path.getsize(path)
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4]
            clen = struct.unpack(">I", hdr[4:8])[0]
            if cid == b"COMM":
                body = f.read(min(clen, 64))
                if len(body) >= 18:
                    channels = struct.unpack(">H", body[0:2])[0]
                    frames = struct.unpack(">I", body[2:6])[0]
                    bits = struct.unpack(">H", body[6:8])[0]
                    rate = _extended80(body[8:18])
                    tech["codec"] = "aiff"
                    tech["vbr"] = False
                    tech["channels"] = channels or None
                    if rate:
                        tech["samplerate"] = int(round(rate))
                        if frames:
                            tech["duration_s"] = round(frames / rate, 3)
                    if rate and bits and channels:
                        tech["bitrate_kbps"] = _br_int(rate * bits * channels)
            elif cid == b"SSND":
                ssnd = (pos + 8 + 8, max(0, clen - 8))   # skip offset+blocksize
            elif cid == b"ID3 " and clen <= 8 * 1024 * 1024:
                frames, _ = _parse_id3v2(f.read(clen))
                _tags_from_id3(frames, tags)
            pos += 8 + clen + (clen & 1)
    return tags, tech, ssnd


# ====================================================================== WMA

def _utf16z(b):
    return b.decode("utf-16-le", "replace").strip("\x00").strip() or None


def _asf_parse(path):
    """Returns (tags, tech, data_span) from the ASF header object."""
    tags = _blank_tags()
    tech = _blank_tech()
    data_span = None
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(30)
        if len(head) < 30 or head[:16] != ASF_HEADER_GUID:
            return tags, tech, None
        count = struct.unpack("<I", head[24:28])[0]
        pos = 30
        for _ in range(min(count, 200)):
            if pos + 24 > size:
                break
            f.seek(pos)
            ohdr = f.read(24)
            if len(ohdr) < 24:
                break
            guid = ohdr[:16]
            osize = struct.unpack("<Q", ohdr[16:24])[0]
            plen = osize - 24
            if osize < 24 or pos + osize > size + 24:
                break
            if plen <= 64 * 1024 * 1024:
                f.seek(pos + 24)
                payload = f.read(plen)
            else:
                payload = b""
            if guid == ASF_FILE_PROPS_GUID and len(payload) >= 72:
                play = struct.unpack("<Q", payload[40:48])[0]
                preroll = struct.unpack("<Q", payload[56:64])[0]
                dur = play / 1e7 - preroll / 1000.0
                if dur > 0:
                    tech["duration_s"] = round(dur, 3)
            elif guid == ASF_STREAM_PROPS_GUID and len(payload) >= 54 + 18:
                if payload[:16] == ASF_AUDIO_STREAM_GUID:
                    ts = payload[54:]
                    chans = struct.unpack("<H", ts[2:4])[0]
                    rate = struct.unpack("<I", ts[4:8])[0]
                    avg_b = struct.unpack("<I", ts[8:12])[0]
                    tech["codec"] = "wma"
                    tech["channels"] = chans or None
                    tech["samplerate"] = rate or None
                    if avg_b:
                        tech["bitrate_kbps"] = _br_int(avg_b * 8)
            elif guid == ASF_CONTENT_DESC_GUID and len(payload) >= 10:
                lens = struct.unpack("<5H", payload[:10])
                parts = []
                off = 10
                for ln in lens:
                    parts.append(payload[off:off + ln])
                    off += ln
                if len(parts) >= 2:
                    _fill(tags, "title", _utf16z(parts[0]))
                    _fill(tags, "artist", _utf16z(parts[1]))
            elif guid == ASF_EXT_CONTENT_GUID and len(payload) >= 2:
                _asf_ext_content(payload, tags, tech)
            pos += osize
        # Top-level data object (after the header object) for payload hashing.
        f.seek(0)
        hdr = f.read(24)
        if len(hdr) == 24:
            header_size = struct.unpack("<Q", hdr[16:24])[0]
            dpos = header_size
            if dpos + 24 <= size:
                f.seek(dpos)
                dobj = f.read(24)
                if len(dobj) == 24 and dobj[:16] == ASF_DATA_GUID:
                    dsize = struct.unpack("<Q", dobj[16:24])[0]
                    data_span = (dpos + 24, min(dsize - 24, size - dpos - 24))
    if tech["codec"] is None and tags != _blank_tags():
        tech["codec"] = "wma"
    if tech.get("bitrate_kbps") is None and tech.get("duration_s"):
        tech["bitrate_kbps"] = _br_int(size * 8 / tech["duration_s"])
    return tags, tech, data_span


def _asf_ext_content(payload, tags, tech):
    try:
        count = struct.unpack("<H", payload[:2])[0]
    except Exception:
        return
    pos = 2
    for _ in range(min(count, 500)):
        if pos + 2 > len(payload):
            return
        nlen = struct.unpack("<H", payload[pos:pos + 2])[0]
        pos += 2
        name = _utf16z(payload[pos:pos + nlen]) or ""
        pos += nlen
        if pos + 4 > len(payload):
            return
        vtype, vlen = struct.unpack("<HH", payload[pos:pos + 4])
        pos += 4
        val = payload[pos:pos + vlen]
        pos += vlen

        def vtext():
            if vtype == 0:
                return _utf16z(val)
            if vtype == 3 and len(val) >= 4:
                return str(struct.unpack("<I", val[:4])[0])
            if vtype == 5 and len(val) >= 2:
                return str(struct.unpack("<H", val[:2])[0])
            if vtype == 4 and len(val) >= 8:
                return str(struct.unpack("<Q", val[:8])[0])
            return None

        if name == "WM/AlbumTitle":
            _fill(tags, "album", vtext())
        elif name == "WM/AlbumArtist":
            _fill(tags, "albumartist", vtext())
        elif name in ("WM/Year", "WM/OriginalReleaseYear"):
            _fill(tags, "year", _year_from(vtext()))
        elif name == "WM/Genre":
            _fill(tags, "genre", vtext())
        elif name == "WM/TrackNumber":
            num, total = _num_pair(vtext())
            _fill(tags, "trackno", num)
            _fill(tags, "tracktotal", total)
        elif name == "WM/Track":                      # legacy, 0-based
            v = vtext()
            if v and v.isdigit():
                _fill(tags, "trackno", int(v) + 1)
        elif name == "WM/PartOfSet":
            num, total = _num_pair(vtext())
            _fill(tags, "discno", num)
            _fill(tags, "disctotal", total)
        elif name == "WM/Picture" and vtype == 1 and val:
            tags["has_art"] = True
        elif name == "WM/IsVBR":
            v = vtext()
            tech["vbr"] = bool(v and v != "0")


# ====================================================================== AAC

def _adts_hdr(b):
    """7-byte ADTS fixed header -> dict or None."""
    if len(b) < 7 or b[0] != 0xFF or (b[1] & 0xF6) != 0xF0:
        return None
    sr_idx = (b[2] >> 2) & 0xF
    if sr_idx >= len(_ADTS_SR):
        return None
    flen = ((b[3] & 0x3) << 11) | (b[4] << 3) | (b[5] >> 5)
    if flen < 7:
        return None
    return {"samplerate": _ADTS_SR[sr_idx],
            "channels": ((b[2] & 1) << 2) | (b[3] >> 6),
            "flen": flen}


def _aac_tech(path, start, end):
    tech = _blank_tech()
    region = end - start
    if region < 7:
        return tech
    with open(path, "rb") as f:
        f.seek(start)
        buf = f.read(min(region, _FRAME_SCAN_CAP))
    off = 0
    hdr = None
    limit = min(len(buf) - 7, 64 * 1024)
    while off < limit:
        hdr = _adts_hdr(buf[off:off + 7])
        if hdr:
            nxt = off + hdr["flen"]
            if nxt + 7 <= len(buf):
                if _adts_hdr(buf[nxt:nxt + 7]):
                    break
            else:
                break
        hdr = None
        off += 1
    if hdr is None:
        return tech
    tech["codec"] = "aac"
    tech["samplerate"] = hdr["samplerate"]
    tech["channels"] = hdr["channels"] or None
    pos = off
    n = 0
    lens = set()
    while pos + 7 <= len(buf):
        h = _adts_hdr(buf[pos:pos + 7])
        if not h:
            break
        lens.add(h["flen"])
        n += 1
        pos += h["flen"]
    if n == 0:
        return tech
    reached_eof = (len(buf) == region) and (pos >= len(buf) - 6)
    tech["vbr"] = len(lens) > 1
    if reached_eof:
        dur = n * 1024 / hdr["samplerate"]
        tech["duration_s"] = round(dur, 3)
        if dur > 0:
            tech["bitrate_kbps"] = _br_int((region - off) * 8 / dur)
    else:
        # Estimate from the average sampled frame bitrate.
        avg_len = (pos - off) / n
        avg_br = avg_len * 8 * hdr["samplerate"] / 1024 / 1000
        if avg_br > 0:
            tech["duration_s"] = round((region - off) * 8 / (avg_br * 1000), 3)
            tech["bitrate_kbps"] = int(round(avg_br))
    return tech


# ============================================================ public contract

def read_tags(path):
    """Tag dict for any supported audio file. Never raises; missing fields
    are None, compilation/has_art default to False."""
    tags = _blank_tags()
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
            return tags
        kind, _ = _sniff(path)
        if kind == "mp3":
            t, _, _ = _mp3_parse(path)
            return t
        if kind == "aac":
            t = _blank_tags()
            with open(path, "rb") as f:
                frames, _ = _parse_id3v2(f.read(256 * 1024))
            _tags_from_id3(frames, t)
            return t
        if kind == "flac":
            return _flac_parse(path)[0]
        if kind == "mp4":
            return _mp4_parse(path)[0]
        if kind == "ogg":
            return _ogg_parse(path)[0]
        if kind == "wav":
            return _wav_parse(path)[0]
        if kind == "aiff":
            return _aiff_parse(path)[0]
        if kind == "wma":
            return _asf_parse(path)[0]
    except Exception:
        pass
    return tags


def tech_info(path):
    """Codec/duration/bitrate/vbr/samplerate/channels. Never raises;
    unknown fields are None (vbr False)."""
    tech = _blank_tech()
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
            return tech
        kind, _ = _sniff(path)
        if kind == "mp3":
            return _mp3_parse(path)[1]
        if kind == "aac":
            start, end, _ = _mp3_bounds(path)
            return _aac_tech(path, start, end)
        if kind == "flac":
            return _flac_parse(path)[1]
        if kind == "mp4":
            return _mp4_parse(path)[1]
        if kind == "ogg":
            return _ogg_parse(path)[1]
        if kind == "wav":
            return _wav_parse(path)[1]
        if kind == "aiff":
            return _aiff_parse(path)[1]
        if kind == "wma":
            return _asf_parse(path)[1]
    except Exception:
        pass
    return tech


def payload_md5(path):
    """md5 of the tag-stripped audio payload (see module docstring for the
    per-format rule). None for missing/empty/unreadable files; unidentified
    bytes fall back to a whole-file hash. Never raises."""
    try:
        if not path or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size == 0:
            return None
        kind, _ = _sniff(path)
        if kind in ("mp3", "aac"):
            start, end, _ = _mp3_bounds(path)
            return _hash_ranges(path, [(start, end - start)])
        if kind == "flac":
            start = _flac_audio_start(path)
            if start is None:
                return _hash_ranges(path, [(0, size)])
            return _hash_ranges(path, [(start, size - start)])
        if kind == "mp4":
            spans = _mp4_mdat_spans(path)
            if spans:
                return _hash_ranges(path, spans)
            return None
        if kind == "wav":
            _, _, span = _wav_parse(path)
            if span:
                return _hash_ranges(path, [span])
            return None
        if kind == "aiff":
            _, _, span = _aiff_parse(path)
            if span:
                return _hash_ranges(path, [span])
            return None
        if kind == "wma":
            _, _, span = _asf_parse(path)
            if span:
                return _hash_ranges(path, [span])
            return _hash_ranges(path, [(0, size)])
        return _hash_ranges(path, [(0, size)])     # ogg / unknown / garbage
    except Exception:
        return None
