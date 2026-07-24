# Organizer — Photos, Movies & TV, Music

The Personal Organization Assistant: three Aegis tools (Photos, Movies & TV,
Music) that scan a library read-only, find duplicates, identify media, and
build a **previewed, undoable** plan before a single file moves.

- **Photos** — EXIF scan, exact + perceptual dedupe, date repair, folder
  schemes (date / camera / place), AI captioning & tagging (local vision
  model), Explore search.
- **Movies & TV** — filename parsing + TMDB identification (optional key),
  Plex-standard or genre-tree layouts, `Movies\` / `TV\` split, `Specials\`
  for season 0, posters/NFO/subtitles travel with their video, `.nfo`
  metadata generation, movies-in-a-TV-library quarantined to `_Movies\`
  (and vice versa).
- **Music** — tag-driven album clustering, 5-stage dedupe ladder
  (byte → payload → fingerprint → tags → filename), MusicBrainz / AcoustID /
  Discogs identification (optional keys), Artist › Album or genre-tree
  layouts, cover-art backfill, and **Fix missing tags** — writes identified
  values into empty tag fields only, with backups, payload verification, and
  one-click undo (needs `mutagen`).

Every organize is plan → preview (with an optional local-LLM plain-English
summary) → execute → undo manifest. Duplicates are quarantined, never
deleted.

Runs embedded in Aegis (each tool is its own window) or standalone:
`python server.py` then open http://127.0.0.1:7100. Audio fingerprinting
needs `fpcalc` (Chromaprint) on PATH or beside `server.py`.
