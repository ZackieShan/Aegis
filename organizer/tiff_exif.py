"""Minimal stdlib-only TIFF/EXIF + QuickTime/MP4 mvhd parsing.

Used for RAW formats (.nef .dng .cr2 .arw .rwl .rw2 .orf .pef .srw ...) which
are TIFF-based, and for video files (ISO-BMFF: .mp4 .mov .m4v .3gp).

Only the first ~256 KB of a TIFF file and the first/last ~2 MB of a video
file are ever read. Everything is best-effort: parse failures return
None / {} and the caller falls back to filesystem dates.
"""
import os
import struct
from datetime import datetime, timezone

TIFF_HEADER_EXTS = {".nef", ".dng", ".cr2", ".arw", ".rwl", ".rw2",
                    ".orf", ".pef", ".srw", ".tif", ".tiff"}

# TIFF field type -> (size, struct format)
_TYPES = {1: (1, "B"), 2: (1, "c"), 3: (2, "H"), 4: (4, "I"), 5: (8, "II"),
          6: (1, "b"), 7: (1, "B"), 8: (2, "h"), 9: (4, "i"), 10: (8, "ii"),
          11: (4, "f"), 12: (8, "d")}

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_DATETIME = 0x0132
TAG_SUBIFDS = 0x014A
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DTO = 0x9003          # DateTimeOriginal
TAG_PREVIEW_OFF = 0x0201  # JPEGInterchangeFormat
TAG_PREVIEW_LEN = 0x0202  # JPEGInterchangeFormatLength


def _read_head(path, cap=262144):
    try:
        with open(path, "rb") as f:
            return f.read(cap)
    except OSError:
        return b""


def _ascii(v):
    if isinstance(v, (bytes, bytearray)):
        return v.split(b"\x00")[0].decode("ascii", "replace").strip()
    return str(v).strip()


class _Tiff:
    """Parse TIFF IFDs from a header buffer."""

    def __init__(self, buf):
        self.buf = buf
        self.ok = False
        if len(buf) < 8 or buf[:2] not in (b"II", b"MM"):
            return
        self.le = buf[:2] == b"II"
        self.en = "<" if self.le else ">"
        # 42 = classic TIFF; 85 = Panasonic RW2 / Leica RWL raw variant
        if struct.unpack(self.en + "H", buf[2:4])[0] not in (42, 85):
            return
        self.ifd0 = struct.unpack(self.en + "I", buf[4:8])[0]
        self.ok = True

    def _u16(self, off):
        if off + 2 > len(self.buf):
            raise ValueError("oob")
        return struct.unpack(self.en + "H", self.buf[off:off + 2])[0]

    def _u32(self, off):
        if off + 4 > len(self.buf):
            raise ValueError("oob")
        return struct.unpack(self.en + "I", self.buf[off:off + 4])[0]

    def read_ifd(self, off):
        """Return (entries dict tag->raw value, next_ifd_offset)."""
        entries = {}
        try:
            n = self._u16(off)
        except ValueError:
            return entries, 0
        base = off + 2
        for i in range(min(n, 512)):
            e = base + i * 12
            try:
                tag = self._u16(e)
                typ = self._u16(e + 2)
                cnt = self._u32(e + 4)
            except ValueError:
                break
            size, fmt = _TYPES.get(typ, (1, "B"))
            total = size * cnt
            try:
                if total <= 4:
                    raw = self.buf[e + 8:e + 8 + 4]
                else:
                    voff = self._u32(e + 8)
                    raw = self.buf[voff:voff + min(total, 65536)]
                entries[tag] = self._decode(typ, cnt, raw)
            except (ValueError, struct.error):
                continue
        try:
            nxt = self._u32(base + n * 12)
        except ValueError:
            nxt = 0
        return entries, nxt

    def _decode(self, typ, cnt, raw):
        size, fmt = _TYPES.get(typ, (1, "B"))
        if typ == 2:
            return _ascii(raw)
        if typ == 7:
            return bytes(raw[:cnt])
        vals = []
        for i in range(min(cnt, 4096)):
            chunk = raw[i * size:(i + 1) * size]
            if len(chunk) < size:
                break
            if typ in (5, 10):
                a, b = struct.unpack(self.en + fmt, chunk)
                vals.append((a, b))
            else:
                vals.append(struct.unpack(self.en + fmt, chunk)[0])
        return vals[0] if cnt == 1 and vals else vals


def parse_tiff_exif(path):
    """Parse a TIFF-based RAW file. Returns dict with any of:
    make, model, dt_base (TIFF DateTime), dto (DateTimeOriginal),
    gps {"lat":..,"lon":..}, preview (offset, length).
    Returns None if the file is not a classic TIFF.
    """
    buf = _read_head(path)
    t = _Tiff(buf)
    if not t.ok:
        return None
    out = {}
    previews = []
    try:
        ifd0, nxt = t.read_ifd(t.ifd0)
    except Exception:
        return None
    make = _ascii(ifd0.get(TAG_MAKE, "") or "")
    model = _ascii(ifd0.get(TAG_MODEL, "") or "")
    if make:
        out["make"] = make
    if model:
        out["model"] = model
    dtb = _ascii(ifd0.get(TAG_DATETIME, "") or "")
    if dtb:
        out["dt_base"] = dtb

    # SubIFDs (some RAWs keep the preview / EXIF here)
    sub = ifd0.get(TAG_SUBIFDS)
    if sub:
        offs = sub if isinstance(sub, list) else [sub]
        for so in offs[:8]:
            if isinstance(so, int) and 0 < so < len(buf):
                e, _ = t.read_ifd(so)
                _collect(e, out, previews, t)

    # EXIF IFD
    ex = ifd0.get(TAG_EXIF_IFD)
    if isinstance(ex, int) and 0 < ex < len(buf):
        e, _ = t.read_ifd(ex)
        dto = _ascii(e.get(TAG_DTO, "") or "")
        if dto:
            out["dto"] = dto
        _collect(e, out, previews, t)

    # GPS IFD
    gp = ifd0.get(TAG_GPS_IFD)
    if isinstance(gp, int) and 0 < gp < len(buf):
        g, _ = t.read_ifd(gp)
        try:
            lat = _gps_dec(g.get(2), g.get(1))
            lon = _gps_dec(g.get(4), g.get(3))
            if lat is not None and lon is not None:
                out["gps"] = {"lat": lat, "lon": lon}
        except Exception:
            pass

    # Preview pointers can live in IFD0 / IFD1 / SubIFDs
    _collect(ifd0, out, previews, t)
    seen = 0
    while nxt and seen < 4 and nxt < len(buf):
        e, nxt = t.read_ifd(nxt)
        _collect(e, out, previews, t)
        seen += 1

    if previews:
        out["preview"] = max(previews, key=lambda p: p[1])
    return out


def _collect(entries, out, previews, t):
    off = entries.get(TAG_PREVIEW_OFF)
    ln = entries.get(TAG_PREVIEW_LEN)
    if isinstance(off, list):
        off = off[0] if off else None
    if isinstance(ln, list):
        ln = ln[0] if ln else None
    if isinstance(off, int) and isinstance(ln, int) and off > 0 and ln > 0:
        previews.append((off, ln))


def _gps_dec(vals, ref):
    if not vals or not isinstance(vals, list) or len(vals) < 3:
        return None
    try:
        d = vals[0][0] / vals[0][1] if vals[0][1] else 0
        m = vals[1][0] / vals[1][1] if vals[1][1] else 0
        s = vals[2][0] / vals[2][1] if vals[2][1] else 0
        dec = d + m / 60.0 + s / 3600.0
        ref_s = _ascii(ref or "")
        if ref_s.upper() in ("S", "W"):
            dec = -dec
        if abs(dec) > 180:
            return None
        return round(dec, 6)
    except (TypeError, ZeroDivisionError, IndexError):
        return None


def extract_preview(path, offset, length, cap=8 * 1024 * 1024):
    """Read the embedded JPEG preview range from a RAW file."""
    if length <= 0 or length > cap:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        if len(data) >= 4 and data[:2] == b"\xff\xd8":
            return data
        return None
    except OSError:
        return None


def raf_jpeg_range(path):
    """Fuji RAF: header holds embedded JPEG offset/length (big-endian u32).
    Offsets 0x54/0x58 for modern RAF; some very old files use 0x18/0x1C."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x60)
    except OSError:
        return None
    if len(head) < 0x5C or not head.startswith(b"FUJIFILM"):
        return None
    for o_off, l_off in ((0x54, 0x58), (0x18, 0x1C)):
        try:
            off = struct.unpack(">I", head[o_off:o_off + 4])[0]
            ln = struct.unpack(">I", head[l_off:l_off + 4])[0]
            if 0 < off < 1 << 31 and 0 < ln < 1 << 31:
                return off, ln
        except struct.error:
            continue
    return None


# ---------------- video (ISO-BMFF mvhd) ----------------

_BMFF_EPOCH_DELTA = 2082844800  # seconds between 1904-01-01 and 1970-01-01


def parse_mvhd(path, cap=2 * 1024 * 1024):
    """Find moov/mvhd and return its creation_time as a naive LOCAL datetime,
    or None. Reads only the first `cap` bytes; when the file is not
    faststart-ed (moov at the end, common for camera QuickTime) it reads the
    last `cap` bytes instead. Bounded reads either way."""
    try:
        with open(path, "rb") as f:
            dt = _mvhd_from(f.read(cap))
            if dt is not None:
                return dt
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= cap:
                return None
            f.seek(size - cap)
            return _mvhd_from(f.read(cap))
    except OSError:
        return None


def _mvhd_from(buf):
    # normal path: buffer starts at an atom boundary (head read)
    moov = _find_atom(buf, 0, len(buf), b"moov")
    spans = [moov] if moov else []
    if not spans:
        # fallback: tail-of-file buffer starts mid-atom; locate the fourcc
        # and recover the span from the size field right before it
        pos = 0
        while True:
            idx = buf.find(b"moov", pos)
            if idx < 0:
                break
            end = len(buf)
            if idx >= 4:
                size = struct.unpack(">I", buf[idx - 4:idx])[0]
                if 8 <= size <= len(buf):
                    end = min(len(buf), idx - 4 + size)
            spans.append((idx + 4, end))
            pos = idx + 4
    for ms, me in spans:
        mv = _find_atom(buf, ms, me, b"mvhd")
        if mv is None:
            continue
        dt = _mvhd_at(buf, mv[0])
        if dt is not None:
            return dt
    return None


def _mvhd_at(buf, s):
    try:
        version = buf[s]
        if version == 1:
            ctime = struct.unpack(">Q", buf[s + 4:s + 12])[0]
        elif version == 0:
            ctime = struct.unpack(">I", buf[s + 4:s + 8])[0]
        else:
            return None
        if ctime < _BMFF_EPOCH_DELTA:
            return None
        unix_ts = ctime - _BMFF_EPOCH_DELTA
        if unix_ts <= 0 or unix_ts > 4102444800:  # > year 2100
            return None
        return datetime.fromtimestamp(unix_ts)  # local naive
    except (struct.error, ValueError, OSError, OverflowError, IndexError):
        return None


def _find_atom(buf, start, end, fourcc):
    """Return (payload_start, payload_end) of first child atom matching fourcc."""
    off = start
    while off + 8 <= end:
        try:
            size = struct.unpack(">I", buf[off:off + 4])[0]
        except struct.error:
            return None
        typ = buf[off + 4:off + 8]
        hdr = 8
        if size == 1:
            if off + 16 > end:
                return None
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end + 8:
            return None
        if typ == fourcc:
            return off + hdr, min(off + size, end)
        off += size
    return None

# ---------------- HEIC/HEIF container dates ----------------

def heic_creation_date(path, cap=8 * 1024 * 1024):
    """Best-effort creation date for HEIC/HEIF. Tries mvhd first, then the
    Exif item inside the top-level meta box (iinf + iloc -> TIFF payload)."""
    try:
        with open(path, "rb") as f:
            head = f.read(cap)
    except OSError:
        return None
    dt = _mvhd_from(head)
    if dt is not None:
        return dt
    meta = _find_atom(head, 0, len(head), b"meta")
    if meta is None:
        return None
    ms, me = meta
    ms += 4  # meta is a full box: skip version/flags
    exif_ids = _iinf_exif_ids(head, ms, me)
    if not exif_ids:
        return None
    loc = _iloc_extents(head, ms, me, exif_ids)
    if not loc:
        return None
    off, length = loc
    if length <= 8 or length > 4 * 1024 * 1024:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(off)
            payload = f.read(length)
    except OSError:
        return None
    if len(payload) < 12:
        return None
    # Exif item: u32 offset-to-TIFF-header, then the TIFF data
    try:
        tiff_off = struct.unpack(">I", payload[:4])[0]
    except struct.error:
        return None
    if tiff_off >= len(payload) - 8:
        return None
    tiff_buf = payload[4 + tiff_off:]
    return _tiff_dto_from_buf(tiff_buf)


def _tiff_dto_from_buf(tiff_buf):
    t = _Tiff(tiff_buf)
    if not t.ok:
        return None
    try:
        ifd0, _ = t.read_ifd(t.ifd0)
    except Exception:
        return None
    ex = ifd0.get(TAG_EXIF_IFD)
    dto = None
    if isinstance(ex, int) and 0 < ex < len(tiff_buf):
        e, _ = t.read_ifd(ex)
        dto = _ascii(e.get(TAG_DTO, "") or "")
    if not dto:
        dto = _ascii(ifd0.get(TAG_DATETIME, "") or "")
    return _parse_exif_dt(dto)


def _parse_exif_dt(s):
    try:
        return datetime.strptime(s.strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _iinf_exif_ids(buf, start, end):
    """item_ID list for items of type 'Exif' in the iinf box."""
    out = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if typ == b"iinf":
            body = pos + 8
            version = buf[body]
            p = body + 4
            if version == 0:
                if p + 2 > pos + size: break
                count = struct.unpack(">H", buf[p:p + 2])[0]
                p += 2
            else:
                if p + 4 > pos + size: break
                count = struct.unpack(">I", buf[p:p + 4])[0]
                p += 4
            for _ in range(min(count, 64)):
                r = _parse_infe(buf, p, pos + size)
                if r is None:
                    break
                item_id, item_type, p = r
                if item_type == b"Exif":
                    out.append(item_id)
            return out
        pos += size
    return out


def _parse_infe(buf, p, box_end):
    """Parse one infe entry (v2/v3); return (item_id, item_type, next_pos)."""
    if p + 8 > box_end:
        return None
    size = struct.unpack(">I", buf[p:p + 4])[0]
    if buf[p + 4:p + 8] != b"infe" or size < 12 or p + size > box_end:
        return None
    version = buf[p + 8]
    q = p + 12
    if version == 3:
        item_id = struct.unpack(">I", buf[q:q + 4])[0]
        q += 4
    else:
        item_id = struct.unpack(">H", buf[q:q + 2])[0]
        q += 2
    q += 2  # item_protection_index
    item_type = buf[q:q + 4]
    return item_id, item_type, p + size


def _iloc_extents(buf, start, end, want_ids):
    """Return (abs_offset, length) of the first wanted item in iloc."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if typ == b"iloc":
            return _parse_iloc(buf, pos + 8, pos + size, want_ids)
        pos += size
    return None


def _parse_iloc(buf, p, box_end, want_ids):
    try:
        version = buf[p]
        sizes = buf[p + 4]
        off_size, len_size = sizes >> 4, sizes & 0xF
        b = buf[p + 5]
        base_size, index_size = b >> 4, b & 0xF
        q = p + 6
        if version < 2:
            count = struct.unpack(">H", buf[q:q + 2])[0]
            q += 2
        else:
            count = struct.unpack(">I", buf[q:q + 4])[0]
            q += 4

        def rd(n):
            nonlocal q
            v = int.from_bytes(buf[q:q + n], "big") if n else 0
            q += n
            return v

        for _ in range(min(count, 512)):
            item_id = rd(4) if version == 2 else rd(2)
            if version in (1, 2):
                rd(2)  # construction_method
            rd(2)      # data_reference_index
            base = rd(base_size)
            ext_cnt = struct.unpack(">H", buf[q:q + 2])[0]
            q += 2
            for j in range(min(ext_cnt, 16)):
                if version in (1, 2) and index_size:
                    rd(index_size)
                eoff = rd(off_size)
                elen = rd(len_size)
                if item_id in want_ids and elen:
                    return base + eoff, elen
        return None
    except (struct.error, IndexError):
        return None
