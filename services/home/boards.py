"""Boards: pictures, links and notes collected around one idea — a room to
redecorate, a party, recipes to try.

A pin is a picture (uploaded, or fetched from a link), a link (with the
page's title and lead picture when the internet is reachable) or a plain
note. Pictures are kept on this server, resized, in a folder per account,
and served only to the account that pinned them.

Pages and pictures named by a link are fetched through the same guarded
fetcher the web tools use: public addresses only, DNS pinned for every
redirect, size capped. With no internet a link is pinned as a link without
its picture.
"""

from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from core.database import SessionLocal

from .common import HomeError, best_match, iso, new_id, owner_of, scoped, squash
from .models import HomeBoard, HomePin

logger = logging.getLogger(__name__)

MAX_BOARDS = 100
MAX_PINS_PER_BOARD = 600
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_FETCH_BYTES = 12 * 1024 * 1024
FULL_SIDE, THUMB_SIDE = 1600, 640


def pins_dir(owner: Optional[str]) -> Path:
    from src.constants import DATA_DIR
    safe = re.sub(r"[^a-z0-9._-]", "_", str(owner or "_single_user").lower()).strip(".") or "_single_user"
    return Path(DATA_DIR) / "home" / "pins" / safe


def _store_image(owner: Optional[str], pin_id: str, data: bytes) -> Tuple[str, int, int]:
    """Keep a picture as a resized JPEG plus a thumbnail. Returns
    (file name, width, height). Anything that is not a picture is refused."""
    from PIL import Image, ImageOps

    if len(data) > MAX_UPLOAD_BYTES:
        raise HomeError("That picture is too large (20 MB at most).")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise HomeError("That file is not a picture I can read.")
    if getattr(image, "is_animated", False):
        image.seek(0)
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    image = image.convert("RGB")
    folder = pins_dir(owner)
    folder.mkdir(parents=True, exist_ok=True)
    full = image.copy()
    full.thumbnail((FULL_SIDE, FULL_SIDE))
    full.save(folder / f"{pin_id}.jpg", "JPEG", quality=86, optimize=True)
    thumb = image.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    thumb.save(folder / f"{pin_id}.t.jpg", "JPEG", quality=80, optimize=True)
    for name in (f"{pin_id}.jpg", f"{pin_id}.t.jpg"):
        try:
            os.chmod(folder / name, 0o600)
        except OSError:
            pass
    return f"{pin_id}.jpg", full.width, full.height


def _drop_image(owner: Optional[str], pin: HomePin) -> None:
    if not pin.image:
        return
    folder = pins_dir(owner)
    for name in (pin.image, pin.image.replace(".jpg", ".t.jpg")):
        try:
            (folder / name).unlink()
        except OSError:
            pass


def image_path(owner: Optional[str], pin_id: str, thumb: bool = False) -> Optional[Path]:
    """The stored picture of one of this account's pins, or None."""
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        pin = scoped(db.query(HomePin), HomePin, owner).filter(HomePin.id == pin_id).first()
        if pin is None or not pin.image:
            return None
        name = pin.image.replace(".jpg", ".t.jpg") if thumb else pin.image
        path = pins_dir(owner) / name
        return path if path.is_file() else None
    finally:
        db.close()


def _pin_view(pin: HomePin) -> dict:
    host = ""
    if pin.url:
        try:
            host = (urlparse(pin.url).hostname or "").removeprefix("www.")
        except ValueError:
            host = ""
    return {
        "id": pin.id, "board_id": pin.board_id, "kind": pin.kind, "title": pin.title or "", "note": pin.note or "",
        "url": pin.url or "", "site": host, "has_image": bool(pin.image),
        "image": f"/api/home/pins/{pin.id}/image" if pin.image else "",
        "thumb": f"/api/home/pins/{pin.id}/image?thumb=1" if pin.image else "",
        "width": pin.width or 0, "height": pin.height or 0, "created": iso(pin.created_at),
    }


def _board_view(board: HomeBoard, pins: List[HomePin], full: bool = False) -> dict:
    pins = sorted(pins, key=lambda p: (p.sort_order or 0, p.created_at), reverse=True)
    view = {
        "id": board.id, "name": board.name, "description": board.description or "", "pin_count": len(pins),
        "covers": [f"/api/home/pins/{p.id}/image?thumb=1" for p in pins if p.image][:4],
        "updated": iso(board.updated_at),
    }
    if full:
        view["pins"] = [_pin_view(p) for p in pins]
    return view


def _get_board(db, owner: Optional[str], board_id: str) -> HomeBoard:
    board = scoped(db.query(HomeBoard), HomeBoard, owner).filter(HomeBoard.id == board_id).first()
    if board is None:
        raise HomeError("That board no longer exists.")
    return board


def _pins_of(db, board_id: str) -> List[HomePin]:
    return db.query(HomePin).filter(HomePin.board_id == board_id).all()


def all_boards(owner: Optional[str]) -> List[dict]:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        boards = scoped(db.query(HomeBoard), HomeBoard, owner).all()
        boards.sort(key=lambda b: b.updated_at, reverse=True)
        return [_board_view(b, _pins_of(db, b.id)) for b in boards]
    finally:
        db.close()


def get_board(owner: Optional[str], board_id: str) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        return _board_view(board, _pins_of(db, board.id), full=True)
    finally:
        db.close()


def find_board(owner: Optional[str], name: str) -> Optional[dict]:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        boards = scoped(db.query(HomeBoard), HomeBoard, owner).all()
        match = best_match(boards, name) or best_match(boards, re.sub(r"\s+board$", "", squash(name), flags=re.I))
        return _board_view(match, _pins_of(db, match.id)) if match is not None else None
    finally:
        db.close()


def create_board(owner: Optional[str], name: str, description: str = "") -> dict:
    owner = owner_of(owner)
    name = squash(name)[:80]
    if not name:
        raise HomeError("Give the board a name.")
    db = SessionLocal()
    try:
        boards = scoped(db.query(HomeBoard), HomeBoard, owner).all()
        same = next((b for b in boards if b.name.lower() == name.lower()), None)
        if same is not None:
            return dict(_board_view(same, _pins_of(db, same.id)), duplicate=True)
        if len(boards) >= MAX_BOARDS:
            raise HomeError(f"There can be at most {MAX_BOARDS} boards.")
        board = HomeBoard(id=new_id(), owner=owner, name=name, description=squash(description)[:500])
        db.add(board)
        db.commit()
        return _board_view(board, [])
    finally:
        db.close()


def update_board(owner: Optional[str], board_id: str, name: Optional[str] = None,
                 description: Optional[str] = None) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        if name is not None and squash(name):
            board.name = squash(name)[:80]
        if description is not None:
            board.description = squash(description)[:500]
        db.commit()
        return _board_view(board, _pins_of(db, board.id))
    finally:
        db.close()


def delete_board(owner: Optional[str], board_id: str) -> bool:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        for pin in _pins_of(db, board.id):
            _drop_image(owner, pin)
            db.delete(pin)
        db.delete(board)
        db.commit()
        return True
    finally:
        db.close()


def _new_pin(db, owner: Optional[str], board: HomeBoard, **fields) -> HomePin:
    count = db.query(HomePin).filter(HomePin.board_id == board.id).count()
    if count >= MAX_PINS_PER_BOARD:
        raise HomeError(f"A board holds at most {MAX_PINS_PER_BOARD} pins.")
    pin = HomePin(id=fields.pop("id", None) or new_id(), board_id=board.id, owner=owner, sort_order=count, **fields)
    db.add(pin)
    from datetime import datetime
    board.updated_at = datetime.utcnow()
    return pin


def add_note_pin(owner: Optional[str], board_id: str, text: str, title: str = "") -> dict:
    owner = owner_of(owner)
    text = str(text or "").strip()[:2000]
    if not text and not squash(title):
        raise HomeError("Write something to pin.")
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        pin = _new_pin(db, owner, board, kind="note", title=squash(title)[:160], note=text)
        db.commit()
        return _pin_view(pin)
    finally:
        db.close()


def add_image_pin(owner: Optional[str], board_id: str, data: bytes, title: str = "", note: str = "",
                  url: str = "") -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        pin_id = new_id()
        name, width, height = _store_image(owner, pin_id, data)
        pin = _new_pin(db, owner, board, id=pin_id, kind="image", title=squash(title)[:160],
                       note=str(note or "").strip()[:2000], url=squash(url)[:1000], image=name, width=width, height=height)
        db.commit()
        return _pin_view(pin)
    finally:
        db.close()


def _fetch(url: str) -> Tuple[dict, Optional[bytes]]:
    """(page facts, picture bytes or None) for a link. Never raises: with no
    internet, or a page that will not load, both come back empty."""
    facts = {"title": "", "description": "", "image_url": ""}
    try:
        from services.search import content as web
    except Exception:
        return facts, None
    headers = {"User-Agent": getattr(web, "WEB_FETCH_USER_AGENT", "Mozilla/5.0"), "Accept": "*/*"}
    picture: Optional[bytes] = None
    try:
        head = web._get_public_url(url, headers=headers, timeout=12, max_bytes=MAX_FETCH_BYTES)
        kind = str(head.headers.get("content-type") or "").lower()
        if kind.startswith("image/"):
            return facts, bytes(head.content)                       # the link is itself a picture
    except Exception as exc:
        logger.info("board pin: could not fetch %s: %s", url[:80], exc)
        return facts, None
    try:
        page = web.fetch_webpage_content(url, timeout=12)
        if page.get("success"):
            facts["title"] = squash(page.get("title"))[:160]
            facts["description"] = squash(page.get("meta_description"))[:400]
            facts["image_url"] = str(page.get("og_image") or "")
    except Exception as exc:
        logger.info("board pin: could not read %s: %s", url[:80], exc)
    if facts["image_url"]:
        try:
            got = web._get_public_url(facts["image_url"], headers=headers, timeout=12, max_bytes=MAX_FETCH_BYTES)
            if str(got.headers.get("content-type") or "").lower().startswith("image/"):
                picture = bytes(got.content)
        except Exception as exc:
            logger.info("board pin: could not fetch the picture for %s: %s", url[:80], exc)
    return facts, picture


def add_link_pin(owner: Optional[str], board_id: str, url: str, title: str = "", note: str = "") -> dict:
    """Pin a link. Blocking (it goes out to the web): call it off the event loop."""
    owner = owner_of(owner)
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I) else url
    if not re.match(r"^https?://[^\s]+$", url, re.I) or len(url) > 1000:
        raise HomeError("That does not look like a web address.")
    db = SessionLocal()
    try:
        _get_board(db, owner, board_id)                       # fail before going out to the web
    finally:
        db.close()
    facts, picture = _fetch(url)
    db = SessionLocal()
    try:
        board = _get_board(db, owner, board_id)
        pin_id = new_id()
        image, width, height = "", 0, 0
        if picture:
            try:
                image, width, height = _store_image(owner, pin_id, picture)
            except HomeError:
                image = ""
        pin = _new_pin(db, owner, board, id=pin_id, kind="image" if image and not facts["title"] and not title else "link",
                       title=squash(title)[:160] or facts["title"], note=str(note or "").strip()[:2000] or facts["description"],
                       url=url, image=image, width=width, height=height)
        db.commit()
        return _pin_view(pin)
    finally:
        db.close()


def update_pin(owner: Optional[str], pin_id: str, title: Optional[str] = None, note: Optional[str] = None,
               board_id: Optional[str] = None) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        pin = scoped(db.query(HomePin), HomePin, owner).filter(HomePin.id == pin_id).first()
        if pin is None:
            raise HomeError("That pin no longer exists.")
        if title is not None:
            pin.title = squash(title)[:160]
        if note is not None:
            pin.note = str(note).strip()[:2000]
        if board_id and board_id != pin.board_id:
            pin.board_id = _get_board(db, owner, board_id).id
        db.commit()
        return _pin_view(pin)
    finally:
        db.close()


def delete_pin(owner: Optional[str], pin_id: str) -> bool:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        pin = scoped(db.query(HomePin), HomePin, owner).filter(HomePin.id == pin_id).first()
        if pin is None:
            raise HomeError("That pin is already gone.")
        _drop_image(owner, pin)
        db.delete(pin)
        db.commit()
        return True
    finally:
        db.close()
