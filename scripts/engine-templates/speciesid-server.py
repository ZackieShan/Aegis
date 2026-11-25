#!/usr/bin/env python3
"""Species-ID server — "what living thing is in this photo?"

BioCLIP (via pybioclip) scores a photo against ~867,000 taxa of the Tree of
Life in one pass and returns the best species, genera and families with
scores. It is a specialist classifier: far better than a general vision
model at telling one mushroom, plant or beetle from the next, and useless for
anything that is not an organism — so Aegis treats its output as *candidates*
for the vision model to weigh, never as the answer.

    GET  /health                      {"status": "ok", "loaded": bool, ...}
    POST /classify   multipart: file=<image>  [k=5]
         -> {"model", "species": [...], "genus": [...], "family": [...],
             "kingdom": [...], "elapsed_ms"}

Runs behind llama-swap like the TTS servers (alias ``species-id``, cpu group):
started on first use, stopped after its ttl. CPU torch is plenty — one photo
takes about a second after the ~15 s load. Model + text embeddings (~5 GB)
come from the Hugging Face cache; fetch them once while online:

    venv/bin/python server.py --prefetch

After that the server loads with the hub switched off (HF_HUB_OFFLINE=1), so
it starts the same with or without internet; pass --online to let it check
the hub.
"""

import argparse
import io
import logging
import os
import threading
import time

logger = logging.getLogger("speciesid")

_classifier = None
_load_lock = threading.Lock()
_predict_lock = threading.Lock()
_model_str = None
_config = {"model": None, "device": "cpu"}      # set from the command line in main()


def _load():
    """Build the classifier once. Thread-safe; later calls return the same one."""
    global _classifier, _model_str
    with _load_lock:
        if _classifier is None:
            started = time.time()
            import bioclip
            from bioclip.predict import TreeOfLifeClassifier

            model_str = _config["model"]
            _model_str = model_str or bioclip.BIOCLIP_MODEL_STR
            kwargs = {"device": _config["device"]}
            if model_str:
                kwargs["model_str"] = model_str
            _classifier = TreeOfLifeClassifier(**kwargs)
            _classifier.eval()
            logger.info("classifier ready in %.1fs (%s, %d taxa)",
                        time.time() - started, _model_str, len(_classifier.txt_names))
    return _classifier


def _open_image(data: bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)        # phone photos carry rotation in EXIF
    return image.convert("RGB")


def _views(image):
    """The whole frame plus a centre crop: phone photos usually have the
    subject in the middle with a lot of background around it, and the model's
    own preprocessing would shrink that subject to a few dozen pixels."""
    width, height = image.size
    side = int(min(width, height) * 0.62)
    left, top = (width - side) // 2, (height - side) // 2
    return [image, image.crop((left, top, left + side, top + side))]


def classify(data: bytes, k: int = 5) -> dict:
    import torch
    from bioclip import Rank

    classifier = _load()
    started = time.time()
    views = _views(_open_image(data))
    with _predict_lock, torch.no_grad():
        probs = classifier.create_batched_probabilities_for_images(
            views, classifier.get_txt_embeddings(), batch_size=len(views))
        merged = None
        for index, view in enumerate(views):
            row = probs[classifier.make_key(view, index)].cpu()
            merged = row if merged is None else merged + row
        merged = merged / len(views)
        key = "photo"
        species = classifier.format_species_probs(key, merged, k)
        grouped = {
            name: classifier.format_grouped_probs(key, merged, rank, 1e-4, k)
            for name, rank in (("genus", Rank.GENUS), ("family", Rank.FAMILY), ("kingdom", Rank.KINGDOM))
        }

    def clean(rows):
        out = []
        for row in rows:
            item = {name: value for name, value in row.items() if name != "file_name" and value not in ("", None)}
            if "score" in item:
                item["score"] = round(float(item["score"]), 4)
            out.append(item)
        return out

    return {
        "model": _model_str,
        "species": clean(species),
        "genus": clean(grouped["genus"]),
        "family": clean(grouped["family"]),
        "kingdom": clean(grouped["kingdom"]),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def build_app():
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile

    app = FastAPI(title="species-id")

    @app.get("/health")
    def health():
        return {"status": "ok", "loaded": _classifier is not None, "model": _model_str}

    # llama-swap probes these while the process starts.
    @app.get("/")
    def root():
        return {"status": "ok"}

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": "species-id", "object": "model"}]}

    @app.post("/classify")
    def classify_route(file: UploadFile = File(...), k: int = Form(5)):
        data = file.file.read()
        if not data:
            raise HTTPException(400, "empty upload")
        if len(data) > 40 * 1024 * 1024:
            raise HTTPException(413, "image too large")
        try:
            return classify(data, k=max(1, min(int(k), 20)))
        except HTTPException:
            raise
        except Exception as exc:  # undecodable image, out of memory, …
            logger.exception("classification failed")
            raise HTTPException(500, f"{type(exc).__name__}: {exc}")

    return app


def main():
    parser = argparse.ArgumentParser(description="BioCLIP species-ID server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--model", default=None,
                        help="open_clip model string (default: pybioclip's current BioCLIP)")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--prefetch", action="store_true",
                        help="download model + embeddings into the Hugging Face cache, then exit")
    parser.add_argument("--online", action="store_true",
                        help="let the Hugging Face libraries contact the hub at load time")
    parser.add_argument("--eager", action="store_true", help="load the model at startup, not on first request")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    _config.update(model=args.model, device=args.device)

    # Everything this server needs is in the Hugging Face cache after one
    # --prefetch. Loading offline skips the hub's "is there a newer file?"
    # round-trips, which otherwise stall for their full timeout with no
    # internet — exactly when this feature matters most.
    if not (args.prefetch or args.online):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    if args.prefetch:
        _load()
        print("model and embeddings are cached")
        return

    if args.eager:
        _load()
    else:
        # Start answering /health immediately; warm the model in the background
        # so the first photo does not pay the whole load.
        threading.Thread(target=_load, daemon=True).start()

    import uvicorn
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
