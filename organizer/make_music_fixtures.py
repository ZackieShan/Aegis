#!/usr/bin/env python3
"""Create the music_fixtures/ test tree for the Music Organizer module.

28 hand-synthesized audio files + 3 sidecars, all built with the stdlib:

  * real WAVs via the `wave` module (distinct sine pitch + duration per song)
  * tiny parseable MP3s: repeated MPEG1 Layer III frame headers with bodies,
    preceded by hand-built ID3v2.3 tags (TPE1/TPE2/TALB/TIT2/TRCK/TPOS/TCON/
    TYER, one file also TCMP=1); the retagged-dupe copy also carries an
    ID3v1 footer so payload-md5 must strip both ends
  * one minimal FLAC: fLaC + STREAMINFO + vorbis-comment block (no audio
    frames), paired with a 128k MP3 of the "same song" for best-copy tests

Scenarios covered:
  * two exact-byte dupes of one song (across folders)
  * a retagged dupe (identical audio payload, different ID3v2 + ID3v1)
  * a 12-track compilation, 5 track artists, ALBUMARTIST blank, one TCMP=1
  * a single-artist WAV album with 2 "(feat. ...)" filenames
  * garbage-named untagged files (track01.mp3, asdf123.mp3)
  * a 2-disc album (Disc 1/Disc 2 folders, TPOS in tags)
  * FLAC + 128k MP3 "same song" (same tags + duration, different codec)
  * sidecars (.cue, folder.jpg, .lrc) beside the compilation
  * a 128k file and a 320k file (bitrate ladder coverage)

Re-run any time; the folder is deleted and recreated from scratch. After
writing, every fixture is re-parsed (structs read back by this script only -
imports nothing from the project) and the run fails loudly if any check
does not hold. Prints a manifest of the tree.
"""
import hashlib
import math
import os
import shutil
import struct
import sys
import wave
from array import array

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "music_fixtures")

SR = 44100  # every fixture uses 44.1 kHz so durations compare cleanly

# MPEG1 Layer III CBR bitrate index table (subset we use).
BITRATE_INDEX = {128: 0x9, 160: 0xA, 192: 0xB, 224: 0xC, 256: 0xD, 320: 0xE}
INDEX_BITRATE = {v: k for k, v in BITRATE_INDEX.items()}

VA_COMPILATION_ARTISTS = {
    1: "Artist Alpha", 2: "Artist Alpha", 3: "Artist Alpha", 4: "Artist Alpha",
    5: "Beat Forge", 6: "Beat Forge",
    7: "Coral Reef", 8: "Coral Reef",
    9: "DJ Monsoon", 10: "DJ Monsoon",
    11: "El Sol", 12: "El Sol",
}
VA_TITLES = {
    1: "Sunny Days", 2: "Beach Bonfire", 3: "Sunset Boulevard",
    4: "Palm Shade", 5: "Neon Circuit", 6: "Voltage",
    7: "Tide Pools", 8: "Salt Air", 9: "Raindance",
    10: "Monsoon Season", 11: "Deserto", 12: "Medianoche",
}
TCMP_TRACK = 7  # the one compilation track carrying TCMP=1

WAV_ALBUM = [  # (track no, title, sine Hz, seconds)
    (1, "Midnight Drive", 220.0, 1.60),
    (2, "City Lights", 262.0, 1.90),
    (3, "Afterglow (feat. Luna Ray)", 330.0, 2.20),
    (4, "Night Rain (feat. The Echoes)", 392.0, 2.50),
]

# Best-copy pair: one duration, expressed as MP3 frames == FLAC samples.
BESTCOPY_FRAMES = 96
BESTCOPY_SAMPLES = BESTCOPY_FRAMES * 1152  # 110592

EXACT_DUPE = dict(artist="Stellar Winds", title="Aurora",
                  album="Northern Lights", track="3/10", genre="Ambient",
                  year="2020", bitrate=192, frames=84, seed=101)
RETAG = dict(artist="Crystal Waves", title="Ocean Drift",
             bitrate=320, frames=90, seed=77)

FAILURES = []


# ---------------------------------------------------------------- builders

def _syncsafe(n):
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F,
                  (n >> 7) & 0x7F, n & 0x7F))


def _id3v23(frames):
    """frames: list of (frame-id, text). Latin-1 text frames, v2.3 sizes."""
    body = b""
    for fid, text in frames:
        data = b"\x00" + text.encode("latin-1")  # encoding byte 0
        body += (fid.encode("ascii") + struct.pack(">I", len(data))
                 + b"\x00\x00" + data)
    return b"ID3\x03\x00\x00" + _syncsafe(len(body)) + body


def _id3v1(title, artist, album, year):
    def field(s, n):
        return s.encode("latin-1")[:n].ljust(n, b"\x00")
    return (b"TAG" + field(title, 30) + field(artist, 30)
            + field(album, 30) + field(year, 4) + field("", 30)
            + b"\xff")


def _mp3_frames(bitrate, nframes, seed):
    """nframes CBR MPEG1-L3 frames, 44.1 kHz stereo, body = constant byte."""
    idx = BITRATE_INDEX[bitrate]
    header = bytes((0xFF, 0xFB, idx << 4, 0x00))  # sr idx 0, no padding
    flen = 144 * bitrate * 1000 // SR + 0
    body = bytes((seed & 0xFF,)) * (flen - 4)
    return (header + body) * nframes


def make_mp3(path, bitrate, nframes, seed, tags=None, id3v1=None):
    data = b""
    if tags:
        data += _id3v23(tags)
    data += _mp3_frames(bitrate, nframes, seed)
    if id3v1:
        data += _id3v1(**id3v1)
    with open(path, "wb") as f:
        f.write(data)


def make_wav(path, freq, seconds):
    n = int(SR * seconds)
    amp = int(0.6 * 32767)
    pcm = array("h", (int(amp * math.sin(2.0 * math.pi * freq * i / SR))
                      for i in range(n)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _vorbis_comment(comments):
    vendor = b"music-fixtures/1.0"
    out = struct.pack("<I", len(vendor)) + vendor
    out += struct.pack("<I", len(comments))
    for key, val in comments:
        entry = ("%s=%s" % (key, val)).encode("utf-8")
        out += struct.pack("<I", len(entry)) + entry
    return out


def make_flac(path, total_samples, comments, channels=2, bps=16):
    packed = ((SR << 44) | ((channels - 1) << 41)
              | ((bps - 1) << 36) | total_samples)
    streaminfo = (struct.pack(">HH", 4096, 4096) + b"\x00" * 6
                  + packed.to_bytes(8, "big") + b"\x00" * 16)
    vc = _vorbis_comment(comments)
    data = (b"fLaC"
            + b"\x00" + len(streaminfo).to_bytes(3, "big") + streaminfo
            + bytes((0x80 | 4,)) + len(vc).to_bytes(3, "big") + vc)
    with open(path, "wb") as f:
        f.write(data)


def make_jpg_stub(path):
    com = b"fixture cover art stub"
    data = (b"\xff\xd8"
            b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xfe" + struct.pack(">H", len(com) + 2) + com
            + b"\xff\xd9")
    with open(path, "wb") as f:
        f.write(data)


# -------------------------------------------------------------- re-parsers

def parse_mp3(path):
    """Read back our own structs: (tags, bitrate, frames, duration, payload_md5)."""
    data = open(path, "rb").read()
    tags = {}
    off = 0
    if data[:3] == b"ID3":
        if data[3] != 3:
            raise ValueError("%s: not ID3v2.3" % path)
        size = ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14
                | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        end, p = 10 + size, 10
        while p + 10 <= end:
            fid = data[p:p + 4].decode("ascii", "replace")
            if not fid.strip("\x00"):
                break
            flen = struct.unpack(">I", data[p + 4:p + 8])[0]
            payload = data[p + 10:p + 10 + flen]
            if payload and payload[0] == 0:
                tags[fid] = payload[1:].decode("latin-1")
            p += 10 + flen
        off = end
    body_end = len(data)
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        body_end = len(data) - 128
    if off >= body_end:
        raise ValueError("%s: no audio frames" % path)
    bitrate = INDEX_BITRATE[data[off + 2] >> 4]
    p, nframes = off, 0
    while p + 4 <= body_end:
        if data[p] != 0xFF or (data[p + 1] & 0xFB) != 0xFB:
            raise ValueError("%s: sync lost at %d" % (path, p))
        flen = 144 * INDEX_BITRATE[data[p + 2] >> 4] * 1000 // SR
        p += flen
        nframes += 1
    if p != body_end:
        raise ValueError("%s: frame walk ended at %d, body ends %d"
                         % (path, p, body_end))
    payload_md5 = hashlib.md5(data[off:body_end]).hexdigest()
    return tags, bitrate, nframes, nframes * 1152.0 / SR, payload_md5


def parse_flac(path):
    """Read back our own structs: (duration, comments dict)."""
    data = open(path, "rb").read()
    if data[:4] != b"fLaC":
        raise ValueError("%s: bad magic" % path)
    p, comments, duration = 4, {}, None
    while True:
        hdr = data[p]
        btype, blen = hdr & 0x7F, int.from_bytes(data[p + 1:p + 4], "big")
        block = data[p + 4:p + 4 + blen]
        if btype == 0:
            x = int.from_bytes(block[10:18], "big")
            sr, total = x >> 44, x & ((1 << 36) - 1)
            duration = total / sr
        elif btype == 4:
            vlen = struct.unpack("<I", block[:4])[0]
            q = 4 + vlen
            n = struct.unpack("<I", block[q:q + 4])[0]
            q += 4
            for _ in range(n):
                elen = struct.unpack("<I", block[q:q + 4])[0]
                q += 4
                key, _, val = block[q:q + elen].decode("utf-8").partition("=")
                comments[key.upper()] = val
                q += elen
        p += 4 + blen
        if hdr & 0x80:
            break
    return duration, comments


def parse_wav(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def check(ok, label):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        FAILURES.append(label)


# ------------------------------------------------------------------ build

def build():
    if os.path.isdir(FIX):
        shutil.rmtree(FIX)
    os.makedirs(FIX)
    written = []

    def w(rel, maker, *args):
        path = os.path.join(FIX, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        maker(path, *args)
        written.append(rel)

    # -- single-artist WAV album, 2 feat.-artist tracks in filenames
    for no, title, freq, secs in WAV_ALBUM:
        w(os.path.join("Neon Skyline - Midnight Drive (2019)",
                       "%02d - %s.wav" % (no, title)), make_wav, freq, secs)

    # -- 12-track compilation, 5 artists, blank ALBUMARTIST, one TCMP=1
    comp_dir = "Summer Vibes 2021"
    for no in range(1, 13):
        frames = [(u"TPE1", VA_COMPILATION_ARTISTS[no]),
                  (u"TALB", u"Summer Vibes 2021"),
                  (u"TIT2", VA_TITLES[no]),
                  (u"TRCK", u"%d/12" % no),
                  (u"TCON", u"Dance"), (u"TYER", u"2021")]
        if no == TCMP_TRACK:
            frames.append((u"TCMP", u"1"))
        w(os.path.join(comp_dir, "%02d - %s - %s.mp3"
                       % (no, VA_COMPILATION_ARTISTS[no], VA_TITLES[no])),
          make_mp3, 192, 40 + 5 * no, 200 + no, frames)

    # -- sidecars next to the compilation
    cue = ['FILE "%02d - %s - %s.mp3" MP3' % (n, VA_COMPILATION_ARTISTS[n],
                                              VA_TITLES[n]) for n in range(1, 13)]
    cue_path = os.path.join(FIX, comp_dir, "Summer Vibes 2021.cue")
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write('PERFORMER "Various Artists"\nTITLE "Summer Vibes 2021"\n'
                + "\n".join(cue) + "\n")
    written.append(os.path.join(comp_dir, "Summer Vibes 2021.cue"))
    lrc_path = os.path.join(FIX, comp_dir, "01 - Artist Alpha - Sunny Days.lrc")
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write("[ti:Sunny Days]\n[ar:Artist Alpha]\n"
                "[00:01.00]Sun comes up over the water\n"
                "[00:02.00]Fixture lyrics line two\n")
    written.append(os.path.join(comp_dir, "01 - Artist Alpha - Sunny Days.lrc"))
    w(os.path.join(comp_dir, "folder.jpg"), make_jpg_stub)

    # -- 2-disc album, TPOS in tags
    two_disc = [("Disc 1", "1/2", [("Anchor's Weight", 60),
                                   ("Sirens Call", 66)]),
                ("Disc 2", "2/2", [("Driftwood", 72),
                                   ("Homeward", 78)])]
    for folder, tpos, tracks in two_disc:
        for idx, (title, nfr) in enumerate(tracks, 1):
            w(os.path.join("The Wandering - Across the Sea (2018)", folder,
                           "%02d - The Wandering - %s.mp3" % (idx, title)),
              make_mp3, 256, nfr, 150 + nfr,
              [(u"TPE1", u"The Wandering"), (u"TPE2", u"The Wandering"),
               (u"TALB", u"Across the Sea"), (u"TIT2", title),
               (u"TRCK", u"%d/2" % idx), (u"TPOS", tpos),
               (u"TCON", u"Folk"), (u"TYER", u"2018")])

    # -- best-copy pair: FLAC + 128k MP3, same tags + duration
    bc_dir = "Amber Vale - Golden Hour EP (2023)"
    bc_tags = [(u"TPE1", u"Amber Vale"), (u"TPE2", u"Amber Vale"),
               (u"TALB", u"Golden Hour EP"), (u"TIT2", u"Golden Hour"),
               (u"TRCK", u"1/5"), (u"TCON", u"Pop"), (u"TYER", u"2023")]
    w(os.path.join(bc_dir, "01 - Amber Vale - Golden Hour.flac"),
      make_flac, BESTCOPY_SAMPLES,
      [("ARTIST", "Amber Vale"), ("ALBUMARTIST", "Amber Vale"),
       ("ALBUM", "Golden Hour EP"), ("TITLE", "Golden Hour"),
       ("TRACKNUMBER", "1"), ("GENRE", "Pop"), ("DATE", "2023")])
    w(os.path.join(bc_dir, "01 - Amber Vale - Golden Hour.mp3"),
      make_mp3, 128, BESTCOPY_FRAMES, 55, bc_tags)

    # -- exact-byte dupes of one song, across folders
    ex_tags = [(u"TPE1", EXACT_DUPE["artist"]), (u"TPE2", EXACT_DUPE["artist"]),
               (u"TALB", EXACT_DUPE["album"]), (u"TIT2", EXACT_DUPE["title"]),
               (u"TRCK", EXACT_DUPE["track"]), (u"TCON", EXACT_DUPE["genre"]),
               (u"TYER", EXACT_DUPE["year"])]
    ex_args = (EXACT_DUPE["bitrate"], EXACT_DUPE["frames"],
               EXACT_DUPE["seed"], ex_tags)
    w(os.path.join("Stellar Winds - Northern Lights (2020)",
                   "03 - Stellar Winds - Aurora.mp3"), make_mp3, *ex_args)
    w(os.path.join("Dupes", "Aurora (copy).mp3"), make_mp3, *ex_args)

    # -- retagged dupe: identical payload, different ID3v2 (+ ID3v1 footer)
    rt_a = os.path.join("Crystal Waves - Ocean Drift - Single (2022)",
                        "01 - Crystal Waves - Ocean Drift.mp3")
    rt_b = os.path.join("Dupes", "Crystal Waves - Ocean Drift (retagged).mp3")
    w(rt_a, make_mp3, RETAG["bitrate"], RETAG["frames"], RETAG["seed"],
      [(u"TPE1", RETAG["artist"]), (u"TPE2", RETAG["artist"]),
       (u"TALB", u"Ocean Drift - Single"), (u"TIT2", RETAG["title"]),
       (u"TRCK", u"1/1"), (u"TCON", u"Electronic"), (u"TYER", u"2022")])
    w(rt_b, make_mp3, RETAG["bitrate"], RETAG["frames"], RETAG["seed"],
      [(u"TPE1", RETAG["artist"]), (u"TIT2", RETAG["title"]),
       (u"TALB", u"Chillhop Essentials"), (u"TRCK", u"7/18"),
       (u"TCON", u"Chill")],
      dict(title=RETAG["title"], artist=RETAG["artist"],
           album="Chillhop Essentials", year="2023"))

    # -- garbage-named untagged files
    w(os.path.join("Loose", "track01.mp3"), make_mp3, 128, 30, 11)
    w(os.path.join("Loose", "asdf123.mp3"), make_mp3, 128, 36, 12)

    return written


# ------------------------------------------------------------------ verify

def verify():
    print("verifying fixtures...")
    md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest()
    j = lambda *a: os.path.join(FIX, *a)

    # WAV album
    for no, title, freq, secs in WAV_ALBUM:
        dur = parse_wav(j("Neon Skyline - Midnight Drive (2019)",
                          "%02d - %s.wav" % (no, title)))
        check(abs(dur - secs) < 0.001,
              "wav %02d duration %.3fs" % (no, dur))

    # compilation
    comp_artists, tcmp_count = set(), 0
    for no in range(1, 13):
        tags, br, nfr, dur, _ = parse_mp3(
            j("Summer Vibes 2021", "%02d - %s - %s.mp3"
              % (no, VA_COMPILATION_ARTISTS[no], VA_TITLES[no])))
        comp_artists.add(tags.get("TPE1"))
        tcmp_count += tags.get("TCMP") == "1"
        if no == 1:
            check(tags.get("TALB") == "Summer Vibes 2021"
                  and tags.get("TRCK") == "1/12" and "TPE2" not in tags,
                  "compilation track 01 tags (no TPE2)")
    check(len(comp_artists) == 5, "compilation has 5 distinct artists")
    check(tcmp_count == 1, "exactly one TCMP=1 in compilation")

    # 2-disc album
    for folder, expect in (("Disc 1", "1/2"), ("Disc 2", "2/2")):
        tags, br, _, _, _ = parse_mp3(
            j("The Wandering - Across the Sea (2018)", folder,
              "01 - The Wandering - %s.mp3"
              % ("Anchor's Weight" if folder == "Disc 1" else "Driftwood")))
        check(tags.get("TPOS") == expect and tags.get("TPE2") == "The Wandering",
              "%s TPOS=%s" % (folder, expect))

    # best-copy pair
    flac_dur, vc = parse_flac(j("Amber Vale - Golden Hour EP (2023)",
                                "01 - Amber Vale - Golden Hour.flac"))
    mtags, mbr, _, mp3_dur, _ = parse_mp3(
        j("Amber Vale - Golden Hour EP (2023)",
          "01 - Amber Vale - Golden Hour.mp3"))
    check(abs(flac_dur - mp3_dur) < 0.02,
          "best-copy durations match (flac %.4f / mp3 %.4f)"
          % (flac_dur, mp3_dur))
    check(vc.get("ARTIST") == mtags.get("TPE1") == "Amber Vale"
          and vc.get("TITLE") == mtags.get("TIT2") == "Golden Hour"
          and mbr == 128,
          "best-copy same tags, mp3 is 128k")

    # exact dupes
    ex_a = j("Stellar Winds - Northern Lights (2020)",
             "03 - Stellar Winds - Aurora.mp3")
    ex_b = j("Dupes", "Aurora (copy).mp3")
    check(md5(ex_a) == md5(ex_b), "exact dupes byte-identical across folders")

    # retagged dupe
    rt_a = j("Crystal Waves - Ocean Drift - Single (2022)",
             "01 - Crystal Waves - Ocean Drift.mp3")
    rt_b = j("Dupes", "Crystal Waves - Ocean Drift (retagged).mp3")
    ta, bra, _, _, pa = parse_mp3(rt_a)
    tb, brb, _, _, pb = parse_mp3(rt_b)
    check(md5(rt_a) != md5(rt_b), "retagged dupe full-file md5 differs")
    check(pa == pb, "retagged dupe payload md5 equal (ID3v2+ID3v1 stripped)")
    check(ta.get("TALB") != tb.get("TALB"), "retagged dupe album tag differs")
    check(bra == brb == 320, "retagged dupe is 320k")

    # loose garbage files: no tags at all
    for name in ("track01.mp3", "asdf123.mp3"):
        tags, br, _, _, _ = parse_mp3(j("Loose", name))
        check(not tags and br == 128, "%s untagged 128k" % name)

    # sidecars
    jpg = open(j("Summer Vibes 2021", "folder.jpg"), "rb").read()
    check(jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9",
          "folder.jpg SOI..EOI")
    cue = open(j("Summer Vibes 2021", "Summer Vibes 2021.cue"),
               encoding="utf-8").read()
    check(cue.count('FILE "') == 12, "cue sheet lists 12 files")
    lrc = open(j("Summer Vibes 2021", "01 - Artist Alpha - Sunny Days.lrc"),
               encoding="utf-8").read()
    check("[00:01.00]" in lrc, "lrc has timestamps")


def manifest():
    print("\nmanifest (%s):" % FIX)
    total = 0
    for root, dirs, files in os.walk(FIX):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            total += 1
            rel = os.path.relpath(path, FIX)
            ext = os.path.splitext(name)[1].lower()
            try:
                if ext == ".mp3":
                    tags, br, nfr, dur, _ = parse_mp3(path)
                    who = tags.get("TPE1", "<untagged>")
                    note = "%s - %s | %dk %dfr %.2fs" % (
                        who, tags.get("TIT2", "<no title>"), br, nfr, dur)
                elif ext == ".flac":
                    dur, vc = parse_flac(path)
                    note = "%s - %s | flac %.2fs" % (
                        vc.get("ARTIST"), vc.get("TITLE"), dur)
                elif ext == ".wav":
                    note = "wav %.2fs" % parse_wav(path)
                else:
                    note = "sidecar"
            except Exception as exc:  # manifest must survive a bad fixture
                note = "PARSE ERROR: %s" % exc
            print("  %-62s %7d  %s" % (rel, size, note))
    print("\n%d files total" % total)


def main():
    written = build()
    print("created %d files in %s" % (len(written), FIX))
    verify()
    manifest()
    if FAILURES:
        print("\n%d CHECK(S) FAILED" % len(FAILURES))
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
