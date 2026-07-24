#!/usr/bin/env python3
"""Add RAW/video/sidecar fixtures to fixture_photos/ (run AFTER make_fixtures.py).

Creates:
  RAW_NIKON.NEF   hand-built TIFF: NIKON D700, DTO 2013:06:15 12:00:00,
                  GPS Rochester, embedded JPEG preview == master.jpg bytes
                  (so the RAW groups with its JPEG twin via aHash)
  raw_canon.dng   TIFF variant: Hasselblad L2D-20c, DTO 2019:11:02 09:30:00,
                  no GPS, distinct embedded preview
  vid_2014.mp4    ftyp + moov/mvhd v0, creation 2014-12-24 10:00 UTC
  RAW_NIKON.xmp   sidecar companion of RAW_NIKON.NEF (same stem)
  orphan.xmp      sidecar with no matching media file
  broken.cr3      garbage bytes -> graceful mtime fallback
  clip.avi        garbage bytes -> video mtime fallback

Idempotent: the seven files are rewritten on every run.
"""
import os
import struct
import sys
from datetime import datetime, timezone

from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
sys.path.insert(0, BASE)

import tiff_exif  # noqa: E402
from make_fixtures import ahash, make_pattern_image, build_exif  # noqa: E402

NEW_FILES = ["RAW_NIKON.NEF", "raw_canon.dng", "vid_2014.mp4", "RAW_NIKON.xmp",
             "orphan.xmp", "broken.cr3", "clip.avi",
             # date-ladder fixtures
             "IMG_20230629_220120.jpg", "canon6d_2000.jpg", "nodate.jpg",
             "IMG-20191102-WA0007.jpg", "fake_container.heic"]

ROCHESTER = (43.1566, -77.6088)

ASCII, LONG, RATIONAL = 2, 4, 5


# ---------------- tiny TIFF writer (little-endian) ----------------

def _ifd_layout(entries, ifd_off, data_off):
    """entries: list of (tag, typ, count, value) where value is int or bytes.
    Returns (ifd_bytes, data_bytes, next_free_off)."""
    n = len(entries)
    data = bytearray()
    out = bytearray()
    out += struct.pack("<H", n)
    for tag, typ, count, value in sorted(entries):
        if isinstance(value, int):
            raw = struct.pack("<I", value)
        else:
            raw = bytes(value)
        if len(raw) <= 4:
            field = raw + b"\x00" * (4 - len(raw))
        else:
            field = struct.pack("<I", data_off + len(data))
            data += raw
            if len(data) % 2:
                data += b"\x00"
        out += struct.pack("<HHI", tag, typ, count) + field
    out += struct.pack("<I", 0)  # next IFD patched by caller when needed
    return bytes(out), bytes(data), data_off + len(data)


def build_raw_tiff(make, model, dto, gps, jpeg_bytes):
    """Build a minimal RAW-flavoured TIFF with EXIF IFD, optional GPS IFD and
    an IFD1 carrying the embedded JPEG preview pointer."""
    ifd0_entries = [
        (0x010F, ASCII, len(make) + 1, make.encode() + b"\x00"),
        (0x0110, ASCII, len(model) + 1, model.encode() + b"\x00"),
        (0x0132, ASCII, 20, dto.encode() + b"\x00"),
        (0x8769, LONG, 1, 0),  # EXIF ptr, patched below
    ]
    if gps:
        ifd0_entries.append((0x8825, LONG, 1, 0))  # GPS ptr, patched below

    exif_entries = [(0x9003, ASCII, 20, dto.encode() + b"\x00")]

    gps_entries = []
    if gps:
        lat, lon = gps

        def rationals(dec):
            d = int(abs(dec))
            m_f = (abs(dec) - d) * 60
            m = int(m_f)
            s = round((m_f - m) * 6000)  # sec * 100
            return struct.pack("<IIIIII", d, 1, m, 1, s, 100)

        gps_entries = [
            (0x0000, 1, 4, bytes([2, 3, 0, 0])),
            (0x0001, ASCII, 2, ("N" if lat >= 0 else "S").encode() + b"\x00"),
            (0x0002, RATIONAL, 3, rationals(lat)),
            (0x0003, ASCII, 2, ("E" if lon >= 0 else "W").encode() + b"\x00"),
            (0x0004, RATIONAL, 3, rationals(lon)),
        ]

    with Image.open(__import__("io").BytesIO(jpeg_bytes)) as im:
        w, h = im.size

    # ---- layout pass: compute offsets ----
    off_ifd0 = 8
    n0 = len(ifd0_entries)
    ifd0_len = 2 + 12 * n0 + 4
    data0_len = sum((len(v) + (len(v) & 1)) for v in
                    (e[3] for e in ifd0_entries) if not isinstance(v, int) and len(v) > 4)
    off_exif = off_ifd0 + ifd0_len + data0_len
    exif_len = 2 + 12 * len(exif_entries) + 4
    exif_data_len = sum((len(v) + (len(v) & 1)) for v in
                        (e[3] for e in exif_entries) if not isinstance(v, int) and len(v) > 4)
    off = off_exif + exif_len + exif_data_len
    off_gps = 0
    gps_len = 0
    gps_data_len = 0
    if gps:
        off_gps = off
        gps_len = 2 + 12 * len(gps_entries) + 4
        gps_data_len = sum((len(v) + (len(v) & 1)) for v in
                           (e[3] for e in gps_entries) if not isinstance(v, int) and len(v) > 4)
        off += gps_len + gps_data_len
    off_ifd1 = off
    ifd1_entries_len = 2 + 12 * 4 + 4
    off_jpeg = off_ifd1 + ifd1_entries_len

    # patch pointers
    patched0 = []
    for tag, typ, cnt, val in ifd0_entries:
        if tag == 0x8769:
            val = off_exif
        elif tag == 0x8825:
            val = off_gps
        patched0.append((tag, typ, cnt, val))

    ifd1_entries = [
        (0x0100, LONG, 1, w),
        (0x0101, LONG, 1, h),
        (0x0201, LONG, 1, off_jpeg),
        (0x0202, LONG, 1, len(jpeg_bytes)),
    ]

    # ---- emit ----
    buf = bytearray()
    buf += b"II" + struct.pack("<H", 42) + struct.pack("<I", off_ifd0)
    b_ifd0, d0, _ = _ifd_layout(patched0, off_ifd0, off_ifd0 + ifd0_len)
    # patch IFD0 "next IFD" pointer -> IFD1
    b_ifd0 = b_ifd0[:-4] + struct.pack("<I", off_ifd1)
    buf += b_ifd0 + d0
    b_exif, d_ex, _ = _ifd_layout(exif_entries, off_exif, off_exif + exif_len)
    buf += b_exif + d_ex
    if gps:
        b_gps, d_g, _ = _ifd_layout(gps_entries, off_gps, off_gps + gps_len)
        buf += b_gps + d_g
    b_ifd1, d1, _ = _ifd_layout(ifd1_entries, off_ifd1, off_ifd1 + ifd1_entries_len)
    buf += b_ifd1 + d1
    assert len(buf) == off_jpeg, f"layout mismatch {len(buf)} != {off_jpeg}"
    buf += jpeg_bytes
    return bytes(buf)


def build_mp4(dt_utc):
    """Minimal MP4: ftyp + moov(mvhd v0) with the given creation time."""
    ctime = int(dt_utc.timestamp()) + 2082844800
    mvhd = bytearray()
    mvhd += bytes([0, 0, 0, 0])                    # version 0 + flags
    mvhd += struct.pack(">I", ctime)               # creation_time
    mvhd += struct.pack(">I", ctime)               # modification_time
    mvhd += struct.pack(">I", 1000)                # timescale
    mvhd += struct.pack(">I", 0)                   # duration
    mvhd += struct.pack(">I", 0x00010000)          # rate 1.0
    mvhd += struct.pack(">H", 0x0100)              # volume 1.0
    mvhd += b"\x00" * 10                           # reserved
    mvhd += struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0,
                        0x40000000)                # unity matrix
    mvhd += b"\x00" * 24                           # predefined
    mvhd += struct.pack(">I", 2)                   # next_track_id
    assert len(mvhd) == 100
    mvhd_atom = struct.pack(">I", 8 + len(mvhd)) + b"mvhd" + bytes(mvhd)
    moov = struct.pack(">I", 8 + len(mvhd_atom)) + b"moov" + mvhd_atom
    ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isomiso2"
    return ftyp + moov


def _exif_only_tiff(make, model, dto):
    """Tiny TIFF with just IFD0(make/model) + EXIF IFD(dto) - no preview."""
    ifd0_entries = [
        (0x010F, ASCII, len(make) + 1, make.encode() + b"\x00"),
        (0x0110, ASCII, len(model) + 1, model.encode() + b"\x00"),
        (0x8769, LONG, 1, 0),
    ]
    exif_entries = [(0x9003, ASCII, 20, dto.encode() + b"\x00")]
    ifd0_len = 2 + 12 * len(ifd0_entries) + 4
    data0_len = sum((len(v) + (len(v) & 1)) for v in
                    (e[3] for e in ifd0_entries) if not isinstance(v, int) and len(v) > 4)
    off_exif = 8 + ifd0_len + data0_len
    patched = [(t, y, c, (off_exif if t == 0x8769 else v))
               for t, y, c, v in ifd0_entries]
    buf = bytearray(b"II" + struct.pack("<H", 42) + struct.pack("<I", 8))
    b0, d0, _ = _ifd_layout(patched, 8, 8 + ifd0_len)
    buf += b0 + d0
    be, de, _ = _ifd_layout(exif_entries, off_exif,
                            off_exif + 2 + 12 * len(exif_entries) + 4)
    buf += be + de
    return bytes(buf)


def build_heic_with_exif(dto):
    """Minimal HEIC: ftyp + meta(iinf[Exif item] + iloc) + mdat(Exif TIFF).
    Just enough for tiff_exif.heic_creation_date to chew on."""
    tiff = _exif_only_tiff("Apple", "iPhone 12", dto)
    payload = struct.pack(">I", 0) + tiff  # u32 tiff-offset, then TIFF

    # iinf v0 with one infe v2 entry: item 1, type 'Exif'
    infe_body = (bytes([2, 0, 0, 0]) + struct.pack(">H", 1)
                 + struct.pack(">H", 0) + b"Exif" + b"Exif\x00")
    infe = struct.pack(">I", 8 + len(infe_body)) + b"infe" + infe_body
    iinf_body = bytes([0, 0, 0, 0]) + struct.pack(">H", 1) + infe
    iinf = struct.pack(">I", 8 + len(iinf_body)) + b"iinf" + iinf_body

    # iloc v0: offset_size=4 length_size=4 base=0, item 1 -> patched offset
    iloc_body = bytearray()
    iloc_body += bytes([0, 0, 0, 0])
    iloc_body += bytes([0x44, 0x00])            # off=4,len=4,base=0,index=0
    iloc_body += struct.pack(">H", 1)           # item_count
    iloc_body += struct.pack(">H", 1)           # item_ID
    iloc_body += struct.pack(">H", 0)           # data_reference_index
    iloc_body += struct.pack(">H", 1)           # extent_count
    iloc_body += struct.pack(">I", 0xFFFFFFFF)  # extent_offset (patched)
    iloc_body += struct.pack(">I", len(payload))
    iloc = struct.pack(">I", 8 + len(iloc_body)) + b"iloc" + bytes(iloc_body)

    hdlr_body = bytes(4) + bytes(4) + b"pict" + bytes(12) + b"\x00"
    hdlr = struct.pack(">I", 8 + len(hdlr_body)) + b"hdlr" + hdlr_body
    meta_body_pre = bytes(4) + hdlr + iinf + iloc
    meta_len = 8 + len(meta_body_pre)
    ftyp = struct.pack(">I", 24) + b"ftyp" + b"heic" + struct.pack(">I", 0) + b"heicmif1"
    exif_abs_off = len(ftyp) + meta_len + 8  # + mdat header
    iloc = iloc.replace(struct.pack(">I", 0xFFFFFFFF),
                        struct.pack(">I", exif_abs_off))
    meta_body = bytes(4) + hdlr + iinf + iloc
    meta = struct.pack(">I", 8 + len(meta_body)) + b"meta" + meta_body
    mdat = struct.pack(">I", 8 + len(payload)) + b"mdat" + payload
    return ftyp + meta + mdat


MTIME_1980 = 315550800.0  # the zeroed-clock value seen in the user's library


def force_mtime(path, ts):
    os.utime(path, (ts, ts))


def main():
    os.makedirs(FIX, exist_ok=True)
    master = os.path.join(FIX, "master.jpg")
    if not os.path.isfile(master):
        sys.exit("run make_fixtures.py first (master.jpg missing)")
    for fn in NEW_FILES:
        p = os.path.join(FIX, fn)
        if os.path.isfile(p):
            os.remove(p)

    with open(master, "rb") as f:
        master_jpeg = f.read()

    # 1) NEF with preview == master.jpg bytes
    nef = build_raw_tiff("NIKON CORPORATION", "NIKON D700",
                         "2013:06:15 12:00:00", ROCHESTER, master_jpeg)
    with open(os.path.join(FIX, "RAW_NIKON.NEF"), "wb") as f:
        f.write(nef)

    # 2) DNG with a distinct embedded preview
    prev = make_pattern_image(777, size=(160, 120))
    import io
    bio = io.BytesIO()
    prev.save(bio, "JPEG", quality=88, exif=build_exif())
    dng = build_raw_tiff("Hasselblad", "L2D-20c", "2019:11:02 09:30:00",
                         None, bio.getvalue())
    with open(os.path.join(FIX, "raw_canon.dng"), "wb") as f:
        f.write(dng)

    # 3) MP4 with mvhd
    with open(os.path.join(FIX, "vid_2014.mp4"), "wb") as f:
        f.write(build_mp4(datetime(2014, 12, 24, 10, 0, tzinfo=timezone.utc)))

    # 4) + 5) sidecars
    with open(os.path.join(FIX, "RAW_NIKON.xmp"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<rdf:RDF><rdf:Description xmp:Rating="4"/></rdf:RDF>'
                '</x:xmpmeta>\n' + "<!-- companion of RAW_NIKON.NEF -->\n")
    with open(os.path.join(FIX, "orphan.xmp"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<rdf:RDF><rdf:Description xmp:Rating="1"/></rdf:RDF>'
                '</x:xmpmeta>\n' + "<!-- orphan: no matching media file. "
                "padding to keep it clearly above fixture sizes. -->\n")

    # 6) + 7) garbage
    with open(os.path.join(FIX, "broken.cr3"), "wb") as f:
        f.write(bytes((i * 37 + 11) % 256 for i in range(300)))
    with open(os.path.join(FIX, "clip.avi"), "wb") as f:
        f.write(bytes((i * 53 + 7) % 256 for i in range(400)))

    # ---- date-ladder fixtures ----
    # zeroed-clock file whose NAME carries the real date (mode 1 from the
    # user's library): filename tier must win
    save_jpeg_noexif = lambda seed, name: make_pattern_image(seed).save(
        os.path.join(FIX, name), "JPEG", quality=88)
    save_jpeg_noexif(401, "IMG_20230629_220120.jpg")
    force_mtime(os.path.join(FIX, "IMG_20230629_220120.jpg"), MTIME_1980)

    # camera clock never set: Canon 6D II (2017) reporting year 2000;
    # plausible mtime must win, EXIF preserved as suspect (mode 2)
    exif6d = build_exif("Canon", "Canon EOS 6D Mark II", "2000:01:01 00:00:00")
    make_pattern_image(402).save(os.path.join(FIX, "canon6d_2000.jpg"),
                                 "JPEG", quality=88, exif=exif6d)
    force_mtime(os.path.join(FIX, "canon6d_2000.jpg"),
                datetime(2021, 5, 5, 12, 0, 0).timestamp())

    # no usable date anywhere -> 'unknown' -> _Unknown Date in plan
    save_jpeg_noexif(403, "nodate.jpg")
    force_mtime(os.path.join(FIX, "nodate.jpg"), MTIME_1980)

    # WhatsApp-style name, plausible mtime: filename tier still wins
    save_jpeg_noexif(404, "IMG-20191102-WA0007.jpg")
    force_mtime(os.path.join(FIX, "IMG-20191102-WA0007.jpg"),
                datetime(2022, 1, 15, 9, 0, 0).timestamp())

    # HEIC whose date only exists in the container meta box
    with open(os.path.join(FIX, "fake_container.heic"), "wb") as f:
        f.write(build_heic_with_exif("2021:08:15 10:00:00"))
    force_mtime(os.path.join(FIX, "fake_container.heic"), MTIME_1980)

    # ---- verification ----
    print("RAW/video/sidecar fixtures written. Verifying...\n")
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    p = tiff_exif.parse_tiff_exif(os.path.join(FIX, "RAW_NIKON.NEF"))
    check("NEF parses", p is not None)
    if p:
        check("NEF make/model", p.get("make") == "NIKON CORPORATION"
              and p.get("model") == "NIKON D700")
        check("NEF dto", p.get("dto") == "2013:06:15 12:00:00")
        g = p.get("gps") or {}
        check("NEF gps ~Rochester", abs(g.get("lat", 0) - 43.1566) < 0.01
              and abs(g.get("lon", 0) + 77.6088) < 0.01)
        prev_rng = p.get("preview")
        check("NEF preview pointer", prev_rng is not None)
        if prev_rng:
            data = tiff_exif.extract_preview(os.path.join(FIX, "RAW_NIKON.NEF"),
                                             *prev_rng)
            check("NEF preview == master.jpg bytes", data == master_jpeg)

    d = tiff_exif.parse_tiff_exif(os.path.join(FIX, "raw_canon.dng"))
    check("DNG parses", d is not None and d.get("model") == "L2D-20c"
          and d.get("dto") == "2019:11:02 09:30:00" and "gps" not in (d or {}))

    mv = tiff_exif.parse_mvhd(os.path.join(FIX, "vid_2014.mp4"))
    check("MP4 mvhd year 2014", mv is not None and mv.year == 2014)

    m_ah = ahash(master)
    with Image.open(io.BytesIO(master_jpeg)) as im:
        check("preview opens in Pillow", im.size == (320, 240))
    check("broken.cr3 not TIFF", tiff_exif.parse_tiff_exif(
        os.path.join(FIX, "broken.cr3")) is None)
    check("clip.avi no mvhd", tiff_exif.parse_mvhd(
        os.path.join(FIX, "clip.avi")) is None)

    # date-ladder fixture verification
    sys.path.insert(0, BASE)
    import date_quality as dq  # noqa: E402
    check("filename date IMG_20230629_220120",
          dq.parse_filename_date("IMG_20230629_220120.jpg")
          == datetime(2023, 6, 29, 22, 1, 20))
    check("filename date WhatsApp",
          dq.parse_filename_date("IMG-20191102-WA0007.jpg")
          == datetime(2019, 11, 2))
    check("filename date rejected when invalid month",
          dq.parse_filename_date("IMG_20231329_220120.jpg") is None)
    check("nodate.jpg has no filename date",
          dq.parse_filename_date("nodate.jpg") is None)
    check("zeroed mtime is suspect", dq.check_mtime(MTIME_1980)[0] is False)
    check("Canon 6D II at 2000 implausible",
          dq.check_datetime(datetime(2000, 1, 1), "Canon EOS 6D Mark II")[0]
          is False)
    hdt = tiff_exif.heic_creation_date(os.path.join(FIX, "fake_container.heic"))
    check("HEIC container Exif date 2021-08-15 10:00 (got %s)" % hdt,
          hdt == datetime(2021, 8, 15, 10, 0, 0))

    if not ok:
        sys.exit("RAW FIXTURE VERIFICATION FAILED")
    print("\nAll RAW fixture checks passed.")


if __name__ == "__main__":
    main()
