#!/usr/bin/env python3
"""Create the cinema_fixtures/ test tree (21 files, tiny dummy bytes).

Files 1 and 3 are byte-identical (exact duplicate). Re-run any time; the
folder is deleted and recreated from scratch.
"""
import os
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "cinema_fixtures")

BYTES_A = b"MATRIX-1080P-DUMMY-BYTES-A" * 100
BYTES_B = b"MATRIX-720P-DUMMY-BYTES-B" * 100
GENERIC = b"DUMMY-VIDEO-BYTES" * 100
SMALL = b"tiny"

FILES = [
    ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv", BYTES_A),
    ("The.Matrix.1999.720p.WEB-DL.mkv", BYTES_B),
    ("The Matrix (1999) 1080p.mkv", BYTES_A),          # exact dupe of #1
    ("Inception (2010) 720p.mp4", GENERIC + b"inception"),
    ("Alien Romulus 2024 2160p WEB-DL.mkv", GENERIC + b"alien"),
    ("Some.Show.S01E01.1080p.WEB-DL.mkv", GENERIC + b"e01-hd"),
    ("Some.Show.S01E02.1080p.WEB-DL.mkv", GENERIC + b"e02"),
    ("Some.Show.S01E01.720p.HDTV.mkv", GENERIC + b"e01-sd"),
    ("Old.Rock.1x02.DVDRip.avi", GENERIC + b"oldrock"),
    ("Some.Show.S01.1080p.WEB-DL.mkv", GENERIC + b"s01pack"),
    ("The.Matrix.1999.1080p.BluRay.x264-GROUP.sample.mkv", SMALL),
    ("random_home_video.mkv", GENERIC + b"home"),
    ("Oceans Eleven.mkv", GENERIC + b"oceans"),        # no year -> unknown
    ("The.Matrix.1999.1080p.BluRay.x264-GROUP.nfo", b"NFO " + SMALL),
    ("screenshot01.jpg", b"JPG " + SMALL),
    ("poster.jpg", b"JPG2 " + SMALL),
    ("evil_screenshot.exe", b"MZ " + SMALL),
    ("setup.exe", b"MZ-not-clutter " + SMALL),         # NOT clutter
    ("readme.txt", b"just a readme"),                  # NOT clutter
    ("The.Matrix.1999.1080p.BluRay.x264-GROUP.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"),
    ("Some.Show.S01E01.1080p.WEB-DL.srt", b"1\n00:00:01,000 --> 00:00:02,000\nYo\n"),
]


def main():
    if os.path.isdir(FIX):
        shutil.rmtree(FIX)
    os.makedirs(FIX)
    for name, data in FILES:
        with open(os.path.join(FIX, name), "wb") as f:
            f.write(data)
    print(f"created {len(FILES)} files in {FIX}")


if __name__ == "__main__":
    main()
