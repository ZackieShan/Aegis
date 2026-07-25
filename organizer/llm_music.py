#!/usr/bin/env python3
"""LLM supervisor for music the tag/MusicBrainz pipeline can't identify.

The hard case: untagged rips in run-on release folders —

    va_hits_2004_cdr\\07-some_band-some_song.mp3
    my old backup\\Disc 2\\13 - Track 13.flac

Tag reads give nothing, so the MusicBrainz release search (which is
tag-first) never even runs. A model reading the FOLDER NAME plus all the
FILENAMES together can usually name the artist/album — shared context is
what makes the guess possible, exactly like llm_tv does for series.

The model NEVER decides alone: music.py verifies the proposed release
against MusicBrainz (release search + track-title cross-check) and only
MusicBrainz's canonical fields are adopted — mirroring the TMDB rule the
cinema supervisor follows. Everything here is failure-safe: any problem
returns None and the caller leaves the cluster unidentified.
"""
import os
import re

import llm_core

MAX_SAMPLE = 15          # filenames shown to the model per folder
MIN_CONFIDENCE = 0.5

_PROMPT = """You identify music files from a personal library.

You get a FOLDER NAME and the FILENAMES inside it (one album folder, or a
folder of loose rips). Folder names are often run-on release text
("va_greatest_hits_2004_cdr" = "VA Greatest Hits 2004 CDR"). Filenames may
start with a track number and then carry the song title; the artist often
appears only in the folder name, sometimes nowhere.

Use the song titles and folder text as evidence. Ignore release tags
(320kbps, FLAC, V0, CDR, WEB, EAC, LOG, CUE, scene group names in
brackets).

Reply with ONLY one JSON object, no prose, no markdown:
{{"kind": "album" | "unknown",
  "artist": "<canonical album artist, or 'Various Artists'>",
  "album": "<canonical album title, else null>",
  "year": <4-digit year or null>,
  "confidence": <0.0-1.0>,
  "tracks": [{{"file": "<exact filename>", "track": <int|null>,
              "title": "<song title>"}}]}}

Rules:
- "kind":"album" only when the files clearly belong to ONE release by ONE
  artist (or one Various Artists compilation) -> fill artist/album/tracks.
- If the files are unrelated singles, or you cannot recognise the release,
  answer kind "unknown", confidence 0.0.
- "artist" is the performer, never the folder's release-group name.
- Never invent an album to be helpful.

FOLDER: "{folder}"
FILES:
{files}
JSON:"""


def build_prompt(folder, filenames):
    listed = "\n".join(f"- {n}" for n in filenames[:MAX_SAMPLE])
    return _PROMPT.replace("{{", "{").replace("}}", "}") \
                  .replace("{folder}", folder or "(none)") \
                  .replace("{files}", listed)


def _int_or_none(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n


def _clean(v, maxlen=200):
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s{2,}", " ", v).strip(" .-_")
    return v if v and len(v) <= maxlen else None


def supervise_folder(folder, filenames, model=None, cfg=None, fetcher=None):
    """Ask the model what one folder of unidentified tracks is.

    Returns None, or a dict:
      {"artist": str, "album": str, "year": int|None, "confidence": float,
       "tracks": {filename: (trackno|None, title)}}
    """
    if not filenames:
        return None
    prompt = build_prompt(folder, filenames)
    obj = llm_core.chat_json(
        [{"role": "system",
          "content": "Reply with ONLY one JSON object. /no_think"},
         {"role": "user", "content": prompt}],
        model=model or llm_core.SMART_MODEL, max_tokens=1200,
        cfg=cfg, fetcher=fetcher)
    if not isinstance(obj, dict):
        return None
    if (obj.get("kind") or "album") == "unknown":
        return None
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    artist = _clean(obj.get("artist"))
    album = _clean(obj.get("album"))
    if not artist or not album:
        return None
    year = _int_or_none(obj.get("year"))
    if year is not None and not (1870 <= year <= 2100):
        year = None
    tracks = {}
    for item in obj.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("file")
        title = _clean(item.get("title"))
        if not isinstance(fn, str) or not title:
            continue
        tn = _int_or_none(item.get("track"))
        if tn is not None and not (0 < tn < 1000):
            tn = None
        tracks[os.path.basename(fn)] = (tn, title)
    return {"artist": artist, "album": album, "year": year,
            "confidence": conf, "tracks": tracks}


_LEAD_NUM = re.compile(r"^\s*(\d{1,3})\s*[-._)\] ]")


def fallback_track(filename):
    """Leading number from a track-led filename, when the model didn't map
    that file explicitly."""
    m = _LEAD_NUM.match(os.path.splitext(os.path.basename(filename))[0])
    return int(m.group(1)) if m else None
