"""Phone notifications (Web Push): the encryption is the standard's, a
subscription can only point at a real push service, each account's devices
are its own, and a device that has gone is forgotten."""
import asyncio
import json

import pytest

from services import push


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "_data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("AEGIS_PUSH_DISABLED", raising=False)
    return tmp_path


def _keys():
    from cryptography.hazmat.primitives.asymmetric import ec

    device = ec.generate_private_key(ec.SECP256R1())
    return device, {"p256dh": push.b64u(push._point(device)), "auth": push.b64u(b"0123456789abcdef")}


def test_encryption_matches_the_worked_example_in_rfc_8291():
    from cryptography.hazmat.primitives.asymmetric import ec

    sender = ec.derive_private_key(int.from_bytes(push.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = push.encrypt(
        b"When I grow up, I want to be a watermelon",
        push.unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
        push.unb64u("BTBZMqHH6r4Tts7J_aSIgg"), sender_key=sender, salt=push.unb64u("DGv6ra1nlYgDCS1FRnbzlw"))
    assert push.b64u(body[:86]) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLo"
                                    "cInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    assert push.b64u(body[86:]) == "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    # Every real message uses a fresh key and salt.
    device, keys = _keys()
    first = push.encrypt(b"hello", push.unb64u(keys["p256dh"]), push.unb64u(keys["auth"]))
    assert first != push.encrypt(b"hello", push.unb64u(keys["p256dh"]), push.unb64u(keys["auth"]))
    with pytest.raises(ValueError):
        push.encrypt(b"hello", b"short", b"short")


def test_the_request_is_signed_by_this_servers_own_key(store):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    header = push.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", now=1_000_000)
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == push.public_key() and (store / "push_vapid.json").stat().st_mode & 0o077 == 0
    head, claims, signature = token.split(".")
    assert json.loads(push.unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    said = json.loads(push.unb64u(claims))
    assert said["aud"] == "https://fcm.googleapis.com" and said["exp"] == 1_000_000 + 12 * 3600
    assert said["sub"] == push.DEFAULT_CONTACT                      # nothing about the person
    raw = push.unb64u(signature)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64u(key))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert push.public_key() == key                                 # the same key next time


def test_a_subscription_must_point_at_a_real_push_service(store):
    _, keys = _keys()
    good = "https://fcm.googleapis.com/fcm/send/device-1"
    for endpoint in ("http://fcm.googleapis.com/fcm/send/x", "https://127.0.0.1/hook", "https://192.168.1.10/x",
                     "https://evil.example/fcm.googleapis.com", "https://fcm.googleapis.com.evil.example/x",
                     "https://user:pw@fcm.googleapis.com/x", "file:///etc/passwd", "", None):
        assert push.endpoint_allowed(endpoint) is False, endpoint
        assert push.subscribe("mara", {"endpoint": endpoint, "keys": keys}) is False
    for endpoint in (good, "https://updates.push.services.mozilla.com/wpush/v2/abc", "https://web.push.apple.com/abc",
                     "https://db5p.notify.windows.com/w/?token=abc"):
        assert push.endpoint_allowed(endpoint) is True
    assert push.subscribe("mara", {"endpoint": good, "keys": {"p256dh": "AAAA", "auth": keys["auth"]}}) is False
    assert push.subscribe("mara", {"endpoint": good, "keys": keys}, agent="Phone") is True
    assert (store / "push_subscriptions.json").stat().st_mode & 0o077 == 0
    assert push.devices("mara") == [{"agent": "Phone", "created": push.devices("mara")[0]["created"], "last_ok": None,
                                     "host": "fcm.googleapis.com"}]
    assert "p256dh" not in json.dumps(push.devices("mara"))          # keys never leave the server
    # The same device, turned on by someone else, now belongs to them alone.
    assert push.subscribe("zed", {"endpoint": good, "keys": keys}) and push.devices("mara") == []
    assert len(push.devices("zed")) == 1 and push.unsubscribe("mara", good) is False
    assert push.unsubscribe("zed", good) is True and push.devices("zed") == []


def test_sending_reaches_only_that_accounts_devices_and_forgets_dead_ones(store, monkeypatch):
    device, keys = _keys()
    push.subscribe("mara", {"endpoint": "https://fcm.googleapis.com/fcm/send/alive", "keys": keys})
    push.subscribe("mara", {"endpoint": "https://fcm.googleapis.com/fcm/send/gone", "keys": keys})
    push.subscribe("zed", {"endpoint": "https://fcm.googleapis.com/fcm/send/zeds", "keys": keys})
    posted = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs.get("follow_redirects") is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            posted.append((url, content, headers))
            return type("Response", (), {"status_code": 410 if url.endswith("gone") else 201})()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    sent = asyncio.run(push.send("mara", "Call the vet", "Tomorrow at 3", url="/home#reminders", tag="reminder-1"))
    assert sent == 1 and sorted(url.rsplit("/", 1)[1] for url, _, _ in posted) == ["alive", "gone"]   # never Zed's
    url, body, headers = posted[0]
    assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith("vapid t=")
    assert b"Call the vet" not in body and b"vet" not in body                    # sealed for the device alone
    assert [d["host"] for d in push.devices("mara")] == ["fcm.googleapis.com"]   # the dead one is forgotten
    assert push.devices("mara")[0]["last_ok"] and len(push.devices("zed")) == 1

    # The device can read it back with its own key: what was sent is what was meant.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    salt, sender_public, sealed = body[:16], body[21:86], body[86:]
    shared = device.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
    hk = lambda n, s, info, m: HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=info).derive(m)
    ikm = hk(32, b"0123456789abcdef", b"WebPush: info\x00" + push._point(device) + sender_public, shared)
    plain = AESGCM(hk(16, salt, b"Content-Encoding: aes128gcm\x00", ikm)).decrypt(hk(12, salt, b"Content-Encoding: nonce\x00", ikm), sealed, None)
    assert plain.endswith(b"\x02") and json.loads(plain[:-1]) == {
        "title": "Call the vet", "body": "Tomorrow at 3", "url": "/home#reminders", "tag": "reminder-1"}

    assert asyncio.run(push.send("nobody", "x")) == 0
    monkeypatch.setenv("AEGIS_PUSH_DISABLED", "1")
    posted.clear()
    assert asyncio.run(push.send("mara", "x")) == 0 and posted == []


def test_no_internet_is_not_an_error(store, monkeypatch):
    _, keys = _keys()
    push.subscribe("mara", {"endpoint": "https://fcm.googleapis.com/fcm/send/alive", "keys": keys})

    class Offline:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise OSError("Network is unreachable")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Offline)
    assert asyncio.run(push.send("mara", "Call the vet")) == 0
    assert len(push.devices("mara")) == 1                           # kept: it may work next time


def test_a_delivered_reminder_is_also_pushed_to_the_persons_phones():
    import inspect

    from routes import note_routes

    source = inspect.getsource(note_routes.dispatch_reminder)
    assert "_push.send(" in source and 'url="/home#reminders"' in source and "if owner:" in source
    worker = open("static/lite/home-sw.js", encoding="utf-8").read()
    assert "addEventListener('push'" in worker and "addEventListener('fetch'" not in worker   # never touches page loads
