#!/usr/bin/env python3
"""Tests for music_tags.py - synthetic fixtures built with struct only.

Every fixture is generated in a temp dir inside the project (deleted on
exit). No network, no real audio: MP3/FLAC/MP4/OGG/WAV/AIFF/WMA/AAC files
are hand-assembled byte streams, so every parsed field has a known value.
"""
import hashlib
import os
import shutil
import struct
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import music_tags as mt

PASS, FAIL = [], []
TMP = None


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


def w(name, data):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def md5(b):
    return hashlib.md5(b).hexdigest()


def blank_tags_ok(t):
    return (set(t.keys()) == {"artist", "albumartist", "album", "title",
                              "trackno", "tracktotal", "discno", "disctotal",
                              "year", "genre", "compilation", "has_art"}
            and all(t[k] is None for k in
                    ("artist", "albumartist", "album", "title", "trackno",
                     "tracktotal", "discno", "disctotal", "year", "genre"))
            and t["compilation"] is False and t["has_art"] is False)


def blank_tech_ok(ti):
    return (set(ti.keys()) == {"codec", "duration_s", "bitrate_kbps", "vbr",
                               "samplerate", "channels"}
            and all(ti[k] is None for k in
                    ("codec", "duration_s", "bitrate_kbps", "samplerate",
                     "channels")) and ti["vbr"] is False)


# -------------------------------------------------------------- byte builders

def syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F,
                  (n >> 7) & 0x7F, n & 0x7F])


def text_payload(s, enc=0):
    if enc == 0:
        return b"\x00" + s.encode("latin-1")
    if enc == 1:
        return b"\x01" + b"\xff\xfe" + s.encode("utf-16-le")
    if enc == 2:
        return b"\x02" + s.encode("utf-16-be")
    return b"\x03" + s.encode("utf-8")


def frame23(fid, payload, flags=b"\x00\x00"):
    return fid.encode("latin-1") + struct.pack(">I", len(payload)) + flags + payload


def frame24(fid, payload, flags=b"\x00\x00"):
    return fid.encode("latin-1") + syncsafe(len(payload)) + flags + payload


def frame22(fid, payload):
    return fid.encode("latin-1") + len(payload).to_bytes(3, "big") + payload


def id3_tag(ver, frames, flags=0, footer=False, prefix_ext=b""):
    body = prefix_ext + frames
    tag = b"ID3" + bytes([ver, 0, flags]) + syncsafe(len(body)) + body
    if footer:
        tag += b"3DI" + bytes([ver, 0, flags]) + syncsafe(len(body))
    return tag


def mp3_frame(br_idx=9, sr_idx=0, stereo=True, pad=0):
    """MPEG-1 Layer III frame, header + zero fill."""
    b1 = 0xFB                                   # v1, L3, no CRC
    b2 = (br_idx << 4) | (sr_idx << 2) | (pad << 1)
    b3 = 0x00 if stereo else 0xC0
    br = [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192,
          224, 256, 320][br_idx]
    sr = [44100, 48000, 32000][sr_idx]
    flen = 144 * br * 1000 // sr + pad
    return bytes([0xFF, b1, b2, b3]) + b"\x00" * (flen - 4)


def xing_frame(marker, nframes, nbytes):
    fr = bytearray(mp3_frame(9))
    payload = marker + struct.pack(">I", 0x3) + \
        struct.pack(">I", nframes) + struct.pack(">I", nbytes)
    fr[36:36 + len(payload)] = payload
    return bytes(fr)


def vbri_frame(nframes, nbytes):
    fr = bytearray(mp3_frame(9))
    payload = b"VBRI" + b"\x00\x01\x00\x00\x02\x00" + \
        struct.pack(">I", nbytes) + struct.pack(">I", nframes)
    fr[36:36 + len(payload)] = payload
    return bytes(fr)


def id3v1(title, artist, album, year, track, genre):
    def f(s, n):
        return s.encode("latin-1").ljust(n, b"\x00")[:n]
    return (b"TAG" + f(title, 30) + f(artist, 30) + f(album, 30) + f(year, 4)
            + f("comment", 28) + b"\x00" + bytes([track]) + bytes([genre]))


def ape_item(key, value, binary=False):
    flags = 0x2 if binary else 0x0
    return struct.pack("<II", len(value), flags) + key.encode() + b"\x00" + value


def ape_tag(items):
    blob = b"".join(items)
    count = len(items)
    tsize = len(blob) + 32
    footer = (b"APETAGEX" + struct.pack("<I", 2000) + struct.pack("<I", tsize)
              + struct.pack("<I", count) + struct.pack("<I", 0) + b"\x00" * 8)
    return blob + footer


def flac_block(btype, data, last):
    return (bytes([(0x80 if last else 0) | btype])
            + len(data).to_bytes(3, "big") + data)


def vorbis_comment(entries, vendor=b"music-tags-test"):
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
    for e in entries:
        b = e.encode("utf-8")
        out += struct.pack("<I", len(b)) + b
    return out


def atom(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def data_atom(payload, dtype=1):
    return atom(b"data", struct.pack(">I", dtype) + b"\x00\x00\x00\x00" + payload)


def ilst_item(typ, payload, dtype=1):
    return atom(typ, data_atom(payload, dtype))


def ogg_page(serial, seq, granule, packets, bos=False, eos=False):
    segs = []
    payload = b""
    for p in packets:
        payload += p
        n = len(p)
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)
    htype = (2 if bos else 0) | (4 if eos else 0)
    hdr = (b"OggS" + bytes([0, htype]) + struct.pack("<q", granule)
           + struct.pack("<I", serial) + struct.pack("<I", seq)
           + b"\x00" * 4 + bytes([len(segs)]) + bytes(segs))
    return hdr + payload


def wav_chunk(cid, payload):
    return (cid + struct.pack("<I", len(payload)) + payload
            + (b"\x00" if len(payload) & 1 else b""))


def aiff_chunk(cid, payload):
    return (cid + struct.pack(">I", len(payload)) + payload
            + (b"\x00" if len(payload) & 1 else b""))


def asf_obj(guid, payload):
    return guid + struct.pack("<Q", 24 + len(payload)) + payload


ASF_HEADER = bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C")
ASF_FILE_PROPS = bytes.fromhex("A1DCAB8C47A9CF118EE400C00C205365")
ASF_STREAM_PROPS = bytes.fromhex("9107DCB7B7A9CF118EE600C00C205365")
ASF_CONTENT_DESC = bytes.fromhex("3326B2758E66CF11A6D900AA0062CE6C")
ASF_EXT_CONTENT = bytes.fromhex("40A4D0D207E3D21197F000A0C95EA850")
ASF_DATA = bytes.fromhex("3626B2758E66CF11A6D900AA0062CE6C")
ASF_AUDIO = bytes.fromhex("409E69F84D5BCF11A8FD00805F5C442B")


def utf16(s):
    return s.encode("utf-16-le") + b"\x00\x00"


def asf_desc(name, vtype, val):
    nb = name.encode("utf-16-le") + b"\x00\x00"
    if vtype == 0:
        vb = val.encode("utf-16-le") + b"\x00\x00"
    elif vtype == 3:
        vb = struct.pack("<I", val)
    else:
        vb = val
    return struct.pack("<H", len(nb)) + nb + struct.pack("<HH", vtype, len(vb)) + vb


def adts_frame(payload_len, sr_idx=4, chan=2):
    flen = 7 + payload_len
    b2 = (1 << 6) | (sr_idx << 2) | (chan >> 2)      # AAC LC
    b3 = ((chan & 3) << 6) | (flen >> 11)
    b4 = (flen >> 3) & 0xFF
    b5 = ((flen & 7) << 5) | 0x1F
    return bytes([0xFF, 0xF1, b2, b3, b4, b5, 0xFC]) + b"\x00" * payload_len


# ==================================================================== MP3

AUDIO_200 = mp3_frame(9) * 200            # 200 x 128k CBR frames
DUR_200 = round(200 * 1152 / 44100, 3)    # 5.224


def test_mp3_v23():
    print("\n== MP3: ID3v2.3 full tag + CBR frames ==")
    frames = b"".join([
        frame23("TIT2", text_payload("Song Title")),
        frame23("TPE1", text_payload("The Artist")),
        frame23("TPE2", text_payload("The Album Artist")),
        frame23("TALB", text_payload("The Album")),
        frame23("TRCK", text_payload("3/10")),
        frame23("TPOS", text_payload("1/2")),
        frame23("TYER", text_payload("1999")),
        frame23("TCON", text_payload("(17)")),
        frame23("TCMP", text_payload("1")),
        frame23("TXXX", b"\x00MusicBrainz Album Id\x00abc-123"),
        frame23("APIC", b"\x00image/jpeg\x00\x03\x00\xff\xd8FAKEJPEG"),
    ])
    p = w("v23.mp3", id3_tag(3, frames) + AUDIO_200)
    t = mt.read_tags(p)
    check("v2.3 text fields",
          (t["title"], t["artist"], t["albumartist"], t["album"]) ==
          ("Song Title", "The Artist", "The Album Artist", "The Album"),
          str(t))
    check("v2.3 numbers",
          (t["trackno"], t["tracktotal"], t["discno"], t["disctotal"]) ==
          (3, 10, 1, 2), str(t))
    check("v2.3 year/genre/compilation/art",
          (t["year"], t["genre"], t["compilation"], t["has_art"]) ==
          (1999, "Rock", True, True), str(t))
    ti = mt.tech_info(p)
    check("v2.3 tech codec/rate/ch",
          (ti["codec"], ti["samplerate"], ti["channels"]) ==
          ("mp3", 44100, 2), str(ti))
    check("v2.3 tech duration/bitrate/vbr",
          (ti["duration_s"], ti["bitrate_kbps"], ti["vbr"]) ==
          (DUR_200, 128, False), str(ti))


def test_mp3_v24():
    print("\n== MP3: ID3v2.4 (ext header+CRC flag, footer, utf-8, per-frame unsync) ==")
    ext = syncsafe(11) + b"\x01\x20" + b"\x00" * 5       # CRC-present flag + 5B CRC
    unsync_raw = b"\x00Name \xff\xe0 End"                 # latin-1, FF E0 inside
    unsync_stored = unsync_raw.replace(b"\xff\xe0", b"\xff\x00\xe0")
    frames = b"".join([
        frame24("TIT2", text_payload("Björk – Vênus", enc=3)),
        frame24("TPE1", b"\x00Art One\x00Art Two"),       # v2.4 multi-value
        frame24("TALB", unsync_stored, flags=b"\x00\x02"),  # per-frame unsync
        frame24("TRCK", text_payload("7")),
        frame24("TDRC", text_payload("2005-03-04", enc=3)),
        frame24("TCON", text_payload("(17)Rock")),
    ])
    tag = id3_tag(4, frames, flags=0x40 | 0x10, footer=True, prefix_ext=ext)
    p = w("v24.mp3", tag + AUDIO_200)
    t = mt.read_tags(p)
    check("v2.4 utf-8 title", t["title"] == "Björk – Vênus", repr(t["title"]))
    check("v2.4 multi-value artist", t["artist"] == "Art One; Art Two",
          repr(t["artist"]))
    check("v2.4 per-frame unsync album", t["album"] == "Name \xff\xe0 End",
          repr(t["album"]))
    check("v2.4 TDRC year + trck + genre",
          (t["year"], t["trackno"], t["tracktotal"], t["genre"]) ==
          (2005, 7, None, "Rock"), str(t))
    ti = mt.tech_info(p)
    check("v2.4 tech (footer skipped before frame walk)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"]) ==
          ("mp3", DUR_200, 128), str(ti))


def test_mp3_v22():
    print("\n== MP3: ID3v2.2 mapping ==")
    frames = b"".join([
        frame22("TT2", text_payload("V22 Title")),
        frame22("TP1", text_payload("V22 Artist")),
        frame22("TYE", text_payload("1993")),
        frame22("TCO", text_payload("(8)")),
        frame22("PIC", b"\x00JPG\x03\x00\xff\xd8PIC"),
    ])
    p = w("v22.mp3", id3_tag(2, frames) + AUDIO_200)
    t = mt.read_tags(p)
    check("v2.2 mapped fields",
          (t["title"], t["artist"], t["year"], t["genre"], t["has_art"]) ==
          ("V22 Title", "V22 Artist", 1993, "Jazz", True), str(t))


def test_mp3_tag_unsync():
    print("\n== MP3: v2.3 tag-level unsynchronisation ==")
    raw = b"\x00A\xff\xe0B"                               # enc + A FF E0 B
    stored = raw.replace(b"\xff\xe0", b"\xff\x00\xe0")
    # frame size stored as the ORIGINAL (pre-insertion) length, mutagen-style
    frame = b"TPE1" + struct.pack(">I", len(raw)) + b"\x00\x00" + stored
    p = w("unsync.mp3", id3_tag(3, frame, flags=0x80) + AUDIO_200)
    t = mt.read_tags(p)
    check("tag-level unsync decodes", t["artist"] == "A\xff\xe0B",
          repr(t["artist"]))


def test_mp3_vbr_detection():
    print("\n== MP3: Xing / Info / VBRI / variance ==")
    p = w("xing.mp3", xing_frame(b"Xing", 100, 41700) + mp3_frame(9) * 99)
    ti = mt.tech_info(p)
    check("Xing -> vbr True, duration from frame count",
          (ti["vbr"], ti["duration_s"], ti["bitrate_kbps"], ti["codec"]) ==
          (True, round(100 * 1152 / 44100, 3), 128, "mp3"), str(ti))

    p = w("info.mp3", xing_frame(b"Info", 100, 41700) + mp3_frame(9) * 99)
    ti = mt.tech_info(p)
    check("Info marker -> vbr False",
          (ti["vbr"], ti["duration_s"]) ==
          (False, round(100 * 1152 / 44100, 3)), str(ti))

    p = w("vbri.mp3", vbri_frame(100, 41700) + mp3_frame(9) * 99)
    ti = mt.tech_info(p)
    check("VBRI -> vbr True",
          (ti["vbr"], ti["duration_s"], ti["bitrate_kbps"]) ==
          (True, round(100 * 1152 / 44100, 3), 128), str(ti))

    mixed = (mp3_frame(9) + mp3_frame(11)) * 100        # 128k/192k alternating
    p = w("variance.mp3", mixed)
    ti = mt.tech_info(p)
    nbytes = 100 * 417 + 100 * 626
    dur = round(200 * 1152 / 44100, 3)
    check("bitrate variance -> vbr True",
          (ti["vbr"], ti["duration_s"],
           ti["bitrate_kbps"]) == (True, dur,
                                   int(round(nbytes * 8 / dur / 1000))),
          str(ti))


def test_mp3_apev2_and_id3v1():
    print("\n== MP3: APEv2 footer / ID3v1 / merge priority ==")
    tag = ape_tag([
        ape_item("Artist", b"Ape Artist"),
        ape_item("Album Artist", b"Ape AlbumArtist"),
        ape_item("Album", b"Ape Album"),
        ape_item("Title", b"Ape Title"),
        ape_item("Track", b"4/9"),
        ape_item("Disc", b"1/2"),
        ape_item("Year", b"1995"),
        ape_item("Genre", b"Blues"),
        ape_item("Compilation", b"1"),
        ape_item("Cover Art (Front)", b"cover.jpg\x00\xff\xd8IMG", binary=True),
    ])
    p = w("ape.mp3", AUDIO_200 + tag)
    t = mt.read_tags(p)
    check("APEv2 fields",
          (t["artist"], t["albumartist"], t["album"], t["title"],
           t["trackno"], t["tracktotal"], t["discno"], t["disctotal"],
           t["year"], t["genre"], t["compilation"], t["has_art"]) ==
          ("Ape Artist", "Ape AlbumArtist", "Ape Album", "Ape Title",
           4, 9, 1, 2, 1995, "Blues", True, True), str(t))
    ti = mt.tech_info(p)
    check("APEv2 stripped before tech walk",
          (ti["duration_s"], ti["bitrate_kbps"]) == (DUR_200, 128), str(ti))

    p = w("id3v1.mp3", AUDIO_200 + id3v1("Old Title", "Old Artist",
                                         "Old Album", "1988", 5, 17))
    t = mt.read_tags(p)
    check("ID3v1 fallback",
          (t["title"], t["artist"], t["album"], t["year"], t["trackno"],
           t["genre"], t["has_art"]) ==
          ("Old Title", "Old Artist", "Old Album", 1988, 5, "Rock", False),
          str(t))

    v2 = id3_tag(3, frame23("TIT2", text_payload("V2 Wins")))
    ape = ape_tag([ape_item("Artist", b"Ape Artist"),
                   ape_item("Year", b"1995")])
    p = w("merge.mp3", v2 + AUDIO_200 + ape
          + id3v1("V1 Title", "V1 Artist", "V1 Album", "1980", 2, 13))
    t = mt.read_tags(p)
    check("priority ID3v2 > APEv2 > ID3v1",
          (t["title"], t["artist"], t["year"], t["album"], t["genre"]) ==
          ("V2 Wins", "Ape Artist", 1995, "V1 Album", "Pop"), str(t))


def test_mp3_payload_md5():
    print("\n== MP3: payload_md5 tag-stripping ==")
    bare = w("bare.mp3", AUDIO_200)
    v2 = w("tagged.mp3", id3_tag(3, frame23("TIT2", text_payload("X")))
           + AUDIO_200)
    v1 = w("v1tag.mp3", AUDIO_200 + id3v1("T", "A", "B", "1990", 1, 0))
    ape = w("apetag.mp3", AUDIO_200
            + ape_tag([ape_item("Title", b"Z")]))
    other = w("other.mp3", mp3_frame(11) * 200)
    expected = md5(AUDIO_200)
    got = [mt.payload_md5(p) for p in (bare, v2, v1, ape)]
    check("payload_md5 equal across tag flavours",
          all(g == expected for g in got), str(got))
    check("payload_md5 differs for different audio",
          mt.payload_md5(other) != expected, mt.payload_md5(other))


def test_mp3_apev2_with_header():
    print("\n== MP3: APEv2 tag with header (flag bit31) ==")
    items = b"".join([ape_item("Artist", b"Head Ape"),
                      ape_item("Title", b"Head Title")])
    tsize = len(items) + 32                     # excludes header per spec
    def half(flags):
        return (b"APETAGEX" + struct.pack("<I", 2000)
                + struct.pack("<I", tsize) + struct.pack("<I", 2)
                + struct.pack("<I", flags) + b"\x00" * 8)
    tag = half(0x80000000) + items + half(0x80000000)   # header + items + footer
    p = w("apehdr.mp3", AUDIO_200 + tag)
    t = mt.read_tags(p)
    check("APEv2-with-header fields",
          (t["artist"], t["title"]) == ("Head Ape", "Head Title"), str(t))
    check("APEv2-with-header payload strips header+items+footer",
          mt.payload_md5(p) == md5(AUDIO_200), mt.payload_md5(p))


def test_mp3_truncated():
    print("\n== MP3: truncated tag / declared size beyond EOF ==")
    good = frame23("TIT2", text_payload("Survives"))
    bad = b"TPE1" + struct.pack(">I", 9999) + b"\x00\x00" + b"\x00A\x00"
    body = good + bad
    tag = b"ID3" + bytes([3, 0, 0]) + syncsafe(len(body) + 5000) + body
    p = w("trunc.mp3", tag)          # declared tag size >> actual file
    t = mt.read_tags(p)
    check("truncated tag: earlier frames still parse",
          t["title"] == "Survives", str(t))
    ti = mt.tech_info(p)
    check("truncated tag: no audio -> tech Nones", blank_tech_ok(ti), str(ti))


# ==================================================================== FLAC

FLAC_AUDIO = b"\xAA" * 32000


def flac_file(comment_entries, audio=FLAC_AUDIO, total=88200, rate=44100,
              ch=2, bps=16, picture=True, bad_streaminfo=False):
    packed = (rate << 44) | ((ch - 1) << 41) | ((bps - 1) << 36) | total
    si = (struct.pack(">HH", 4096, 4096) + b"\x00" * 6
          + packed.to_bytes(8, "big") + b"\x00" * 16)
    blocks = b""
    if bad_streaminfo:
        blocks += bytes([0]) + (34).to_bytes(3, "big") + b"\x00" * 10  # short
        return b"fLaC" + blocks                                     # then EOF
    blocks += flac_block(0, si, False)
    blocks += flac_block(4, vorbis_comment(comment_entries), not picture)
    if picture:
        pic = (struct.pack(">I", 3) + struct.pack(">I", 10) + b"image/jpeg"
               + struct.pack(">I", 0) + struct.pack(">IIII", 100, 100, 24, 0)
               + struct.pack(">I", 4) + b"\xff\xd8\xff\xd9")
        blocks += flac_block(6, pic, True)
    return b"fLaC" + blocks + audio


FLAC_COMMENTS = ["ARTIST=Flac Artist", "ALBUMARTIST=Flac AlbumArtist",
                 "ALBUM=Flac Album", "TITLE=Flac Title", "TRACKNUMBER=2/9",
                 "DISCNUMBER=1/2", "DATE=2001-07-04", "GENRE=Jazz",
                 "COMPILATION=1"]


def test_flac():
    print("\n== FLAC: STREAMINFO + vorbis comments + PICTURE ==")
    p = w("test.flac", flac_file(FLAC_COMMENTS))
    t = mt.read_tags(p)
    check("flac tag fields",
          (t["artist"], t["albumartist"], t["album"], t["title"],
           t["trackno"], t["tracktotal"], t["discno"], t["disctotal"],
           t["year"], t["genre"], t["compilation"], t["has_art"]) ==
          ("Flac Artist", "Flac AlbumArtist", "Flac Album", "Flac Title",
           2, 9, 1, 2, 2001, "Jazz", True, True), str(t))
    ti = mt.tech_info(p)
    check("flac tech (duration from total samples, bitrate from stream)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("flac", 2.0, 128, True, 44100, 2), str(ti))


def test_flac_payload():
    print("\n== FLAC: payload_md5 skips metadata blocks ==")
    a = w("a.flac", flac_file(FLAC_COMMENTS))
    b = w("b.flac", flac_file(["TITLE=Different", "ARTIST=Different"]))
    c = w("c.flac", flac_file(FLAC_COMMENTS, audio=b"\xBB" * 32000))
    check("flac payload equal across different comments",
          mt.payload_md5(a) == mt.payload_md5(b) == md5(FLAC_AUDIO),
          f"{mt.payload_md5(a)} {mt.payload_md5(b)}")
    check("flac payload differs for different audio",
          mt.payload_md5(c) != md5(FLAC_AUDIO), mt.payload_md5(c))
    d = w("d.flac", flac_file(["METADATA_BLOCK_PICTURE=QUJDREVGRw=="],
                              picture=False))
    check("flac has_art via base64 picture comment (no PICTURE block)",
          mt.read_tags(d)["has_art"] is True, str(mt.read_tags(d)))


def test_flac_truncated():
    print("\n== FLAC: truncated STREAMINFO ==")
    p = w("bad.flac", flac_file([], bad_streaminfo=True))
    t = mt.read_tags(p)
    ti = mt.tech_info(p)
    check("truncated flac: blank tags, no raise", blank_tags_ok(t), str(t))
    check("truncated flac: tech Nones, no raise", blank_tech_ok(ti), str(ti))


# ==================================================================== MP4

def mp4_stsd(fmt_code):
    entry_body = (b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
                  + struct.pack(">H", 2) + struct.pack(">H", 16)
                  + b"\x00" * 4 + struct.pack(">I", 44100 << 16))
    entry = struct.pack(">I", 8 + len(entry_body)) + fmt_code + entry_body
    return atom(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + entry)


MDAT = b"\x11" * 8000


def mp4_file(items, fmt_code=b"mp4a", mdat=MDAT):
    mvhd = atom(b"mvhd", struct.pack(">IIIII", 0, 0, 0, 44100, 88200)
                + b"\x00" * 80)
    mdhd = atom(b"mdhd", struct.pack(">IIIII", 0, 0, 0, 44100, 88200)
                + b"\x00" * 4)
    ilst = atom(b"ilst", b"".join(items))
    meta = atom(b"meta", b"\x00" * 4 + ilst)
    moov = atom(b"moov", mvhd
                + atom(b"trak", atom(b"mdia", mdhd
                       + atom(b"minf", atom(b"stbl", mp4_stsd(fmt_code)))))
                + atom(b"udta", meta))
    ftyp = atom(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42")
    return ftyp + moov + atom(b"mdat", mdat)


MP4_ITEMS = [
    ilst_item(b"\xa9nam", "Mp4 Title".encode()),
    ilst_item(b"\xa9ART", "Mp4 Artist".encode()),
    ilst_item(b"aART", "Mp4 AlbumArtist".encode()),
    ilst_item(b"\xa9alb", "Mp4 Album".encode()),
    ilst_item(b"\xa9day", b"1998-05-01"),
    ilst_item(b"\xa9gen", b"Rock"),
    ilst_item(b"trkn", b"\x00\x00" + struct.pack(">HH", 5, 12)
              + b"\x00\x00", dtype=0),
    ilst_item(b"disk", b"\x00\x00" + struct.pack(">HH", 1, 2)
              + b"\x00\x00", dtype=0),
    ilst_item(b"cpil", b"\x01", dtype=21),
    ilst_item(b"covr", b"\xff\xd8JPEGDATA", dtype=13),
]


def test_mp4():
    print("\n== MP4/M4A: ilst atoms + mvhd/mdhd + stsd ==")
    p = w("test.m4a", mp4_file(MP4_ITEMS))
    t = mt.read_tags(p)
    check("mp4 tag fields",
          (t["title"], t["artist"], t["albumartist"], t["album"], t["year"],
           t["genre"], t["trackno"], t["tracktotal"], t["discno"],
           t["disctotal"], t["compilation"], t["has_art"]) ==
          ("Mp4 Title", "Mp4 Artist", "Mp4 AlbumArtist", "Mp4 Album", 1998,
           "Rock", 5, 12, 1, 2, True, True), str(t))
    ti = mt.tech_info(p)
    check("mp4 tech (aac, mdhd duration, mdat bitrate)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"],
           ti["samplerate"], ti["channels"]) ==
          ("aac", 2.0, 32, 44100, 2), str(ti))

    p = w("gnre.m4a", mp4_file([ilst_item(b"gnre", struct.pack(">H", 18),
                                          dtype=0)]))
    check("mp4 gnre id -> genre name", mt.read_tags(p)["genre"] == "Rock",
          mt.read_tags(p)["genre"])

    p = w("alac.m4a", mp4_file([], fmt_code=b"alac"))
    check("mp4 alac codec", mt.tech_info(p)["codec"] == "alac",
          mt.tech_info(p)["codec"])


def test_mp4_payload():
    print("\n== MP4: payload_md5 = mdat only ==")
    a = w("pa.m4a", mp4_file(MP4_ITEMS))
    b = w("pb.m4a", mp4_file([ilst_item(b"\xa9nam", b"Other")]))
    c = w("pc.m4a", mp4_file(MP4_ITEMS, mdat=b"\x22" * 8000))
    check("mp4 payload equal across different ilst",
          mt.payload_md5(a) == mt.payload_md5(b) == md5(MDAT),
          f"{mt.payload_md5(a)} {mt.payload_md5(b)}")
    check("mp4 payload differs for different mdat",
          mt.payload_md5(c) != md5(MDAT), mt.payload_md5(c))


def test_mp4_corrupt():
    print("\n== MP4: corrupt atom sizes ==")
    bad_moov = atom(b"moov", struct.pack(">I", 4) + b"junk")  # size < header
    p = w("corrupt.m4a", atom(b"ftyp", b"M4A " + b"\x00" * 8) + bad_moov)
    t = mt.read_tags(p)
    ti = mt.tech_info(p)
    check("corrupt mp4: blank tags, no raise", blank_tags_ok(t), str(t))
    check("corrupt mp4: duration None, no raise",
          ti["duration_s"] is None and ti["bitrate_kbps"] is None, str(ti))


# ==================================================================== OGG

def test_ogg_vorbis():
    print("\n== OGG Vorbis: id + comment packets + granulepos ==")
    vid = (b"\x01vorbis" + struct.pack("<I", 0) + bytes([2])
           + struct.pack("<I", 44100) + struct.pack("<i", 256000)
           + struct.pack("<i", 128000) + struct.pack("<i", 64000)
           + b"\xB0\x01")
    vc = (b"\x03vorbis" + vorbis_comment(
        ["ARTIST=Ogg Artist", "ALBUMARTIST=Ogg AlbumArtist", "ALBUM=Ogg Album",
         "TITLE=Ogg Title", "TRACKNUMBER=6", "TRACKTOTAL=11", "DATE=2010",
         "GENRE=Metal"]) + b"\x01")
    data = (ogg_page(7, 0, 0, [vid], bos=True)
            + ogg_page(7, 1, 0, [vc])
            + ogg_page(7, 2, 132300, [b"\x00" * 50], eos=True))
    p = w("test.ogg", data)
    t = mt.read_tags(p)
    check("ogg vorbis tag fields",
          (t["artist"], t["albumartist"], t["album"], t["title"],
           t["trackno"], t["tracktotal"], t["year"], t["genre"]) ==
          ("Ogg Artist", "Ogg AlbumArtist", "Ogg Album", "Ogg Title",
           6, 11, 2010, "Metal"), str(t))
    ti = mt.tech_info(p)
    check("ogg vorbis tech (nominal bitrate, granule duration)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("vorbis", 3.0, 128, True, 44100, 2), str(ti))


def test_ogg_opus():
    print("\n== OGG Opus: OpusHead/OpusTags + 48kHz granule ==")
    head = (b"OpusHead" + bytes([1, 2]) + struct.pack("<H", 312)
            + struct.pack("<I", 48000) + struct.pack("<H", 0) + bytes([0]))
    tags_pkt = b"OpusTags" + vorbis_comment(
        ["ARTIST=Opus Artist", "TITLE=Opus Title", "ALBUM=Opus Album",
         "DATE=2015", "GENRE=Pop", "TRACKNUMBER=1/1"])
    data = (ogg_page(9, 0, 0, [head], bos=True)
            + ogg_page(9, 1, 0, [tags_pkt])
            + ogg_page(9, 2, 96312, [b"\x00" * 40], eos=True))
    p = w("test.opus", data)
    t = mt.read_tags(p)
    check("opus tag fields",
          (t["artist"], t["title"], t["album"], t["year"], t["genre"],
           t["trackno"], t["tracktotal"]) ==
          ("Opus Artist", "Opus Title", "Opus Album", 2015, "Pop", 1, 1),
          str(t))
    ti = mt.tech_info(p)
    expected_br = int(round(len(data) * 8 / 2.0 / 1000))
    check("opus tech (48k granule minus pre-skip)",
          (ti["codec"], ti["duration_s"], ti["samplerate"], ti["channels"],
           ti["vbr"], ti["bitrate_kbps"]) ==
          ("opus", 2.0, 48000, 2, True, expected_br), str(ti))


# ==================================================================== WAV

PCM = b"\x00" * 176400                        # 1s of 44.1k/16bit/stereo


def test_wav():
    print("\n== WAV: fmt/data + LIST-INFO + id3 chunk ==")
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 176400, 4, 16)
    info = b"INFO" + b"".join([
        wav_chunk(b"IART", b"Wav Artist\x00"),
        wav_chunk(b"INAM", b"Wav Name\x00"),
        wav_chunk(b"IPRD", b"Wav Album\x00"),
        wav_chunk(b"IGNR", b"Blues\x00"),
        wav_chunk(b"ITRK", b"8\x00"),
        wav_chunk(b"ICRD", b"1987\x00"),
    ])
    id3c = wav_chunk(b"id3 ", id3_tag(
        3, frame23("TIT2", text_payload("ID3 Wins"))))
    body = (wav_chunk(b"fmt ", fmt) + wav_chunk(b"data", PCM)
            + wav_chunk(b"LIST", info) + id3c)
    p = w("test.wav", b"RIFF" + struct.pack("<I", 4 + len(body))
          + b"WAVE" + body)
    ti = mt.tech_info(p)
    check("wav tech (pcm, byterate bitrate)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("wav", 1.0, 1411, False, 44100, 2), str(ti))
    t = mt.read_tags(p)
    check("wav LIST-INFO fields",
          (t["artist"], t["album"], t["genre"], t["trackno"], t["year"]) ==
          ("Wav Artist", "Wav Album", "Blues", 8, 1987), str(t))
    check("wav id3 chunk beats LIST-INFO", t["title"] == "ID3 Wins",
          t["title"])
    check("wav payload_md5 = data chunk",
          mt.payload_md5(p) == md5(PCM), mt.payload_md5(p))


# ==================================================================== AIFF

def test_aiff():
    print("\n== AIFF: COMM 80-bit rate + SSND ==")
    rate80 = bytes([0x40, 0x0E, 0xAC, 0x44, 0, 0, 0, 0, 0, 0])   # 44100.0
    comm = aiff_chunk(b"COMM", struct.pack(">H", 2)
                      + struct.pack(">I", 44100) + struct.pack(">H", 16)
                      + rate80)
    ssnd = aiff_chunk(b"SSND", struct.pack(">II", 0, 0) + PCM)
    body = comm + ssnd
    p = w("test.aiff", b"FORM" + struct.pack(">I", 4 + len(body))
          + b"AIFF" + body)
    ti = mt.tech_info(p)
    check("aiff tech",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("aiff", 1.0, 1411, False, 44100, 2), str(ti))
    check("aiff payload_md5 = SSND sound data",
          mt.payload_md5(p) == md5(PCM), mt.payload_md5(p))


# ==================================================================== WMA

def wma_file(is_vbr=0, data_payload=b"\x77" * 4096):
    fileprops = (b"\x00" * 16 + struct.pack("<Q", 123456)
                 + struct.pack("<Q", 0) + struct.pack("<Q", 100)
                 + struct.pack("<Q", 50000000)        # play duration: 5s
                 + struct.pack("<Q", 0)               # send duration
                 + struct.pack("<Q", 0)               # preroll ms
                 + struct.pack("<I", 2) + struct.pack("<I", 100)
                 + struct.pack("<I", 100) + struct.pack("<I", 128000))
    wfx = struct.pack("<HHIIHHH", 0x0161, 2, 44100, 16000, 4, 16, 0)
    streamprops = (ASF_AUDIO + b"\x00" * 16 + struct.pack("<Q", 0)
                   + struct.pack("<I", len(wfx)) + struct.pack("<I", 0)
                   + struct.pack("<H", 1) + struct.pack("<I", 0) + wfx)
    strings = [utf16("Wma Title"), utf16("Wma Artist"), utf16(""),
               utf16(""), utf16("")]
    content = (struct.pack("<5H", *[len(s) for s in strings])
               + b"".join(strings))
    ext = struct.pack("<H", 8) + b"".join([
        asf_desc("WM/AlbumTitle", 0, "Wma Album"),
        asf_desc("WM/AlbumArtist", 0, "Wma AlbumArtist"),
        asf_desc("WM/Year", 0, "2003"),
        asf_desc("WM/Genre", 0, "Metal"),
        asf_desc("WM/TrackNumber", 0, "7"),
        asf_desc("WM/PartOfSet", 0, "1/2"),
        asf_desc("WM/IsVBR", 3, is_vbr),
        asf_desc("WM/Picture", 1, b"\xff\xd8JPEG"),
    ])
    children = (asf_obj(ASF_FILE_PROPS, fileprops)
                + asf_obj(ASF_STREAM_PROPS, streamprops)
                + asf_obj(ASF_CONTENT_DESC, content)
                + asf_obj(ASF_EXT_CONTENT, ext))
    header = (ASF_HEADER + struct.pack("<Q", 30 + len(children))
              + struct.pack("<I", 4) + b"\x01\x02" + children)
    data_obj = asf_obj(ASF_DATA, b"\x00" * 16 + struct.pack("<Q", 1)
                       + b"\x00\x00" + data_payload)
    return header + data_obj


def test_wma():
    print("\n== WMA: ASF header objects ==")
    p = w("test.wma", wma_file())
    t = mt.read_tags(p)
    check("wma tag fields",
          (t["title"], t["artist"], t["album"], t["albumartist"],
           t["year"], t["genre"], t["trackno"], t["discno"],
           t["disctotal"], t["has_art"]) ==
          ("Wma Title", "Wma Artist", "Wma Album", "Wma AlbumArtist",
           2003, "Metal", 7, 1, 2, True), str(t))
    ti = mt.tech_info(p)
    check("wma tech (5s play duration, 128k stream)",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("wma", 5.0, 128, False, 44100, 2), str(ti))

    p2 = w("vbr.wma", wma_file(is_vbr=1))
    check("wma WM/IsVBR=1 -> vbr True", mt.tech_info(p2)["vbr"] is True,
          str(mt.tech_info(p2)))
    p3 = w("other.wma", wma_file(data_payload=b"\x88" * 4096))
    check("wma payload_md5 equal/differ via data object",
          mt.payload_md5(p) == mt.payload_md5(w("copy.wma", wma_file()))
          and mt.payload_md5(p) != mt.payload_md5(p3),
          f"{mt.payload_md5(p)} vs {mt.payload_md5(p3)}")


# ==================================================================== AAC

AAC_AUDIO = adts_frame(200) * 100


def test_aac():
    print("\n== AAC: ADTS frame walk + optional ID3v2 ==")
    p = w("test.aac", AAC_AUDIO)
    ti = mt.tech_info(p)
    dur = round(100 * 1024 / 44100, 3)
    check("aac adts tech",
          (ti["codec"], ti["duration_s"], ti["bitrate_kbps"], ti["vbr"],
           ti["samplerate"], ti["channels"]) ==
          ("aac", dur, int(round(len(AAC_AUDIO) * 8 / dur / 1000)), False,
           44100, 2), str(ti))

    tag = id3_tag(3, frame23("TIT2", text_payload("Aac Title"))
                  + frame23("TPE1", text_payload("Aac Artist")))
    p2 = w("tagged.aac", tag + AAC_AUDIO)
    t = mt.read_tags(p2)
    check("aac with prepended ID3v2",
          (t["title"], t["artist"]) == ("Aac Title", "Aac Artist"), str(t))
    check("aac payload_md5 strips ID3v2",
          mt.payload_md5(p2) == md5(AAC_AUDIO), mt.payload_md5(p2))


# =========================================================== robustness etc

def test_garbage_and_empty():
    print("\n== garbage / 0-byte / missing ==")
    g = w("garbage.mp3", b"GARBAGE-NOT-AUDIO-" * 40)
    t = mt.read_tags(g)
    ti = mt.tech_info(g)
    check("garbage: blank tags", blank_tags_ok(t), str(t))
    check("garbage: tech Nones", blank_tech_ok(ti), str(ti))
    check("garbage: payload falls back to whole-file hash",
          mt.payload_md5(g) == md5(b"GARBAGE-NOT-AUDIO-" * 40),
          mt.payload_md5(g))

    e = w("empty.mp3", b"")
    check("0-byte: blank tags", blank_tags_ok(mt.read_tags(e)), "")
    check("0-byte: tech Nones", blank_tech_ok(mt.tech_info(e)), "")
    check("0-byte: payload None", mt.payload_md5(e) is None, "")
    check("missing file: no raise, defaults",
          blank_tags_ok(mt.read_tags(os.path.join(TMP, "nope.mp3")))
          and blank_tech_ok(mt.tech_info(os.path.join(TMP, "nope.mp3")))
          and mt.payload_md5(os.path.join(TMP, "nope.mp3")) is None, "")


def test_contract_shape():
    print("\n== contract shape on a real parse ==")
    t = mt.read_tags(w("shape.mp3", id3_tag(
        3, frame23("TIT2", text_payload("S"))) + AUDIO_200))
    ti = mt.tech_info(w("shape2.mp3", AUDIO_200))
    check("read_tags key set exact",
          set(t.keys()) == {"artist", "albumartist", "album", "title",
                            "trackno", "tracktotal", "discno", "disctotal",
                            "year", "genre", "compilation", "has_art"},
          str(sorted(t.keys())))
    check("tech_info key set exact",
          set(ti.keys()) == {"codec", "duration_s", "bitrate_kbps", "vbr",
                             "samplerate", "channels"},
          str(sorted(ti.keys())))
    check("field types",
          isinstance(t["compilation"], bool) and isinstance(t["has_art"], bool)
          and isinstance(ti["duration_s"], float)
          and isinstance(ti["bitrate_kbps"], int)
          and isinstance(ti["vbr"], bool)
          and isinstance(ti["samplerate"], int)
          and isinstance(ti["channels"], int), str(ti))


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="music_tags_test_", dir=BASE)
    try:
        test_mp3_v23()
        test_mp3_v24()
        test_mp3_v22()
        test_mp3_tag_unsync()
        test_mp3_vbr_detection()
        test_mp3_apev2_and_id3v1()
        test_mp3_payload_md5()
        test_mp3_apev2_with_header()
        test_mp3_truncated()
        test_flac()
        test_flac_payload()
        test_flac_truncated()
        test_mp4()
        test_mp4_payload()
        test_mp4_corrupt()
        test_ogg_vorbis()
        test_ogg_opus()
        test_wav()
        test_aiff()
        test_wma()
        test_aac()
        test_garbage_and_empty()
        test_contract_shape()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
