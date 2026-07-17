"""Gallery sections + style-transfer default model (2026-07-13).

Covers the two fixes from the "failed to stylize" + "no video/generated
sections" report:
1. `_default_gallery_model` — when the editor sends no model, the style route
   must pick a routable image model (llama-swap 404s a model-less payload),
   preferring an edit-capable one.
2. The library `kind` filter predicates — Photos / Generated / Videos must
   partition the gallery exactly (no row in two sections, none dropped).
"""
import json
import uuid

from routes.gallery.gallery_routes import (
    UPLOAD_MODEL_SENTINELS,
    VIDEO_EXTS,
    _default_gallery_model,
)


def _kind_predicates(GalleryImage):
    """The exact predicates the library route's `kind` filter uses."""
    from sqlalchemy import and_, or_
    from routes.gallery.gallery_routes import AUDIO_EXTS
    vid = or_(*[GalleryImage.filename.ilike(f"%.{e}") for e in sorted(VIDEO_EXTS)])
    aud = or_(*[GalleryImage.filename.ilike(f"%.{e}") for e in sorted(AUDIO_EXTS)])
    upload = or_(GalleryImage.model == None,  # noqa: E711
                 GalleryImage.model == "",
                 GalleryImage.model.in_(UPLOAD_MODEL_SENTINELS))
    return vid, aud, upload


class _Ep:
    def __init__(self, models):
        self.cached_models = json.dumps(models)


# ── default model for style transfer ─────────────────────────────────────────
def test_default_prefers_edit_capable_model():
    ep = _Ep(["qwen3-coder-30b", "qwen-image", "qwen-image-edit", "qwen-vl"])
    assert _default_gallery_model(ep) == "qwen-image-edit"


def test_default_falls_back_to_generation_model():
    ep = _Ep(["qwen3-coder-30b", "qwen-image-rapid-nsfw"])
    assert _default_gallery_model(ep) == "qwen-image-rapid-nsfw"


def test_default_empty_when_no_image_model():
    assert _default_gallery_model(_Ep(["qwen3-coder-30b", "supergemma4-26b"])) == ""


def test_default_ignores_vision_models():
    # 'qwen-vl' must not be mistaken for an image *generation* model.
    assert _default_gallery_model(_Ep(["qwen-vl", "qwen2.5-vl-3b"])) == ""


def test_default_tolerates_bad_cached_models():
    class Broken:
        cached_models = "not json"
    assert _default_gallery_model(Broken()) == ""


# ── kind filter partition ─────────────────────────────────────────────────────
def _mk(db, GalleryImage, filename, model):
    row = GalleryImage(id=uuid.uuid4().hex[:12], filename=filename,
                       prompt="p", model=model, is_active=True)
    db.add(row)
    return row


def test_kind_predicates_partition_gallery():
    """Uses rows the REAL handlers create: gallery uploads stamp
    model="imported", chat uploads stamp model="chat-upload" — never NULL.
    The Photos section must catch those sentinels (the original predicate
    only checked NULL/'' and classified every upload as Generated)."""
    from sqlalchemy import and_
    from core.database import GalleryImage, SessionLocal

    db = SessionLocal()
    try:
        marker = uuid.uuid4().hex[:8]
        rows = [
            _mk(db, GalleryImage, f"{marker}-a.png", "qwen-image"),      # generated image
            _mk(db, GalleryImage, f"{marker}-b.webm", "wan2.2-t2v"),     # generated video
            _mk(db, GalleryImage, f"{marker}-c.jpg", "imported"),        # gallery upload (real sentinel)
            _mk(db, GalleryImage, f"{marker}-d.mp4", "imported"),        # uploaded video
            _mk(db, GalleryImage, f"{marker}-e.png", "chat-upload"),     # chat attachment
            _mk(db, GalleryImage, f"{marker}-f.png", None),              # legacy row
            _mk(db, GalleryImage, f"{marker}-g.mp3", "ace-step-1.5"),    # generated song
        ]
        db.commit()

        base = db.query(GalleryImage).filter(GalleryImage.filename.like(f"{marker}%"))
        vid, aud, upload = _kind_predicates(GalleryImage)

        videos = {r.filename for r in base.filter(vid)}
        music = {r.filename for r in base.filter(aud)}
        generated = {r.filename for r in base.filter(and_(~upload, ~vid, ~aud))}
        photos = {r.filename for r in base.filter(and_(upload, ~vid, ~aud))}

        assert videos == {f"{marker}-b.webm", f"{marker}-d.mp4"}
        assert music == {f"{marker}-g.mp3"}
        assert generated == {f"{marker}-a.png"}
        assert photos == {f"{marker}-c.jpg", f"{marker}-e.png", f"{marker}-f.png"}
        # exact partition: disjoint and complete
        assert videos | music | generated | photos == {r.filename for r in rows}
        assert not (videos & generated) and not (videos & photos) and not (generated & photos)
        assert not (music & videos) and not (music & generated) and not (music & photos)
    finally:
        try:
            db.query(GalleryImage).filter(GalleryImage.filename.like(f"{marker}%")).delete(
                synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
        db.close()
