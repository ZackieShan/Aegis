"""Notifications on a person's own phone, with the app closed (Web Push).

A phone or browser that turns notifications on hands over a *subscription*:
an address at its vendor's push service (Google, Mozilla, Apple, Microsoft)
plus two keys. To notify it, this server encrypts a small message for that
device alone (RFC 8291, ``aes128gcm``), signs the request with its own key
(VAPID, RFC 8292) and posts it to that address. The push service wakes the
device; it cannot read the message.

What that means in practice:

* It needs the internet at the moment of sending. With none, the reminder
  still shows inside the app; nothing is queued for later.
* The push service learns that this server sent something to that device and
  when — not what. Nothing identifying the person is sent: the contact
  address in the signature is the ``push_contact`` setting, a placeholder by
  default.
* Subscriptions are per account and are only ever sent to one of the known
  push services, over https.

No extra dependency: the two primitives come from ``cryptography``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000                  # bytes of JSON; well inside one record
MAX_DEVICES = 12                    # per account
# Where a subscription may point. Anything else is refused: the address comes
# from the browser, and this server will POST to it.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
              "web.push.apple.com", "notify.windows.com")
DEFAULT_CONTACT = "mailto:notifications@example.org"

_lock = threading.Lock()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    text = str(text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def enabled() -> bool:
    return not os.environ.get("AEGIS_PUSH_DISABLED")


# ── this server's signing key ─────────────────────────────────────────────

def _key_path() -> str:
    return os.path.join(_data_dir(), "push_vapid.json")


def _load_key():
    """The server's P-256 key pair, created on first use."""
    from cryptography.hazmat.primitives.asymmetric import ec

    path = _key_path()
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            return ec.derive_private_key(int.from_bytes(unb64u(stored["private"]), "big"), ec.SECP256R1())
        except (OSError, ValueError, KeyError):
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        number = key.private_numbers().private_value.to_bytes(32, "big")
        from core.atomic_io import atomic_write_json
        atomic_write_json(path, {"private": b64u(number), "public": b64u(_point(key)), "created": time.time()}, mode=0o600)
        return key


def _point(private_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """What a browser needs to subscribe (the ``applicationServerKey``)."""
    return b64u(_point(_load_key()))


def _contact() -> str:
    try:
        from src.settings import load_settings
        value = str(load_settings().get("push_contact") or "").strip()
    except Exception:
        value = ""
    return value if value.startswith(("mailto:", "https://")) else DEFAULT_CONTACT


def vapid_authorization(endpoint: str, key=None, now: Optional[float] = None) -> str:
    """The Authorization header value that proves the request is this server's."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = key or _load_key()
    parsed = urlparse(endpoint)
    claims = {"aud": f"{parsed.scheme}://{parsed.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": _contact()}
    signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "."
                     + b64u(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={b64u(_point(key))}"


# ── encrypting for one device ─────────────────────────────────────────────

def encrypt(payload: bytes, device_public: bytes, auth_secret: bytes, sender_key=None,
            salt: Optional[bytes] = None) -> bytes:
    """The request body for one push message (RFC 8291, a single record).

    ``sender_key`` and ``salt`` are fresh for every message; they are only
    passed in by the test that checks this against the RFC's worked example.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(device_public) != 65 or len(auth_secret) != 16:
        raise ValueError("not a Web Push subscription key")
    sender_key = sender_key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    sender_public = _point(sender_key)
    shared = sender_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device_public))

    def derive(length: int, hkdf_salt: bytes, info: bytes, material: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=hkdf_salt, info=info).derive(material)

    ikm = derive(32, auth_secret, b"WebPush: info\x00" + device_public + sender_public, shared)
    content_key = derive(16, salt, b"Content-Encoding: aes128gcm\x00", ikm)
    nonce = derive(12, salt, b"Content-Encoding: nonce\x00", ikm)
    sealed = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)       # 0x02: last (only) record
    return salt + struct.pack(">I", RECORD_SIZE) + bytes([len(sender_public)]) + sender_public + sealed


# ── who has notifications turned on ───────────────────────────────────────

def _store_path() -> str:
    return os.path.join(_data_dir(), "push_subscriptions.json")


def _read() -> Dict[str, List[dict]]:
    try:
        with open(_store_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {k: [d for d in v if isinstance(d, dict)] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


def _write(data: Dict[str, List[dict]]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(_store_path(), data, mode=0o600)


def endpoint_allowed(endpoint: str) -> bool:
    try:
        parsed = urlparse(str(endpoint or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in PUSH_HOSTS)


def subscribe(owner: Optional[str], subscription: Dict[str, Any], agent: str = "") -> bool:
    """Remember a device for an account. False when it is not a usable
    subscription or points somewhere it should not."""
    endpoint = str((subscription or {}).get("endpoint") or "")
    keys = (subscription or {}).get("keys") or {}
    try:
        device_public, auth_secret = unb64u(keys.get("p256dh")), unb64u(keys.get("auth"))
    except Exception:
        return False
    if not endpoint_allowed(endpoint) or len(endpoint) > 2000 or len(device_public) != 65 or len(auth_secret) != 16:
        return False
    key = owner or ""
    with _lock:
        data = _read()
        # A device belongs to whoever turned it on last.
        for name in list(data):
            data[name] = [d for d in data[name] if d.get("endpoint") != endpoint]
        devices = data.setdefault(key, [])
        devices.append({"endpoint": endpoint, "p256dh": b64u(device_public), "auth": b64u(auth_secret),
                        "agent": str(agent or "")[:200], "created": time.time(), "failures": 0})
        data[key] = devices[-MAX_DEVICES:]
        _write({k: v for k, v in data.items() if v})
    return True


def unsubscribe(owner: Optional[str], endpoint: str) -> bool:
    key = owner or ""
    with _lock:
        data = _read()
        before = len(data.get(key, []))
        data[key] = [d for d in data.get(key, []) if d.get("endpoint") != endpoint]
        if len(data[key]) == before:
            return False
        _write({k: v for k, v in data.items() if v})
    return True


def devices(owner: Optional[str]) -> List[dict]:
    """This account's devices, without their keys."""
    return [{"agent": d.get("agent") or "", "created": d.get("created"), "last_ok": d.get("last_ok"),
             "host": urlparse(d.get("endpoint") or "").hostname or ""} for d in _read().get(owner or "", [])]


def forget(owner: Optional[str]) -> None:
    with _lock:
        data = _read()
        if data.pop(owner or "", None) is not None:
            _write(data)


def rename(old_owner: str, new_owner: str) -> bool:
    """Carry an account's devices over to its new name."""
    with _lock:
        data = _read()
        if not data.get(old_owner):
            return False
        data[new_owner] = (data.get(new_owner) or []) + data.pop(old_owner)
        data[new_owner] = data[new_owner][-MAX_DEVICES:]
        _write(data)
    return True


# ── sending ───────────────────────────────────────────────────────────────

async def send(owner: Optional[str], title: str, body: str = "", url: str = "/home", tag: Optional[str] = None) -> int:
    """Notify every device of one account. Returns how many accepted it.
    Never raises: no internet, a device that unsubscribed and a push service
    having a bad day are all ordinary."""
    if not enabled():
        return 0
    mine = _read().get(owner or "", [])
    if not mine:
        return 0
    message = {"title": str(title or "Aegis")[:120], "body": str(body or "")[:600], "url": url}
    if tag:
        message["tag"] = str(tag)[:120]
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")[:MAX_PAYLOAD]
    delivered, gone, failed = 0, [], []
    try:
        import httpx

        key = _load_key()
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for device in mine:
                endpoint = device.get("endpoint") or ""
                if not endpoint_allowed(endpoint):
                    gone.append(endpoint)
                    continue
                try:
                    sealed = encrypt(payload, unb64u(device["p256dh"]), unb64u(device["auth"]))
                    response = await client.post(endpoint, content=sealed, headers={
                        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                        "TTL": "86400", "Urgency": "high", "Authorization": vapid_authorization(endpoint, key),
                    })
                except Exception as exc:
                    logger.info("push not sent (%s): %s", urlparse(endpoint).hostname, exc)
                    failed.append(endpoint)
                    continue
                if response.status_code in (200, 201, 202, 204):
                    delivered += 1
                elif response.status_code in (404, 410):
                    gone.append(endpoint)                      # the device unsubscribed
                else:
                    logger.info("push refused by %s: %s", urlparse(endpoint).hostname, response.status_code)
                    failed.append(endpoint)
    except Exception as exc:
        logger.warning("push: sending failed: %s", exc)
        return delivered
    if delivered or gone or failed:
        with _lock:
            data = _read()
            kept = []
            for device in data.get(owner or "", []):
                endpoint = device.get("endpoint")
                if endpoint in gone:
                    continue
                if endpoint in failed:
                    device["failures"] = int(device.get("failures") or 0) + 1
                    if device["failures"] >= 20:
                        continue                              # dead for a long time: stop trying
                elif endpoint_allowed(endpoint or ""):
                    device["failures"] = 0
                    device["last_ok"] = time.time()
                kept.append(device)
            data[owner or ""] = kept
            _write({k: v for k, v in data.items() if v})
    return delivered
