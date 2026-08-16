"""Reach the phone when it is locked.

Everything Friday does well happens while a tab is open on a Mac you are sitting
at. Your phone is locked most of the day, which is exactly when "api is waiting
on you" matters. Until this existed, Friday was a thing you checked rather than a
thing that reached you, and that is a different product.

Web Push, done properly:

  VAPID    an ES256 JWT identifying this Friday to the push service, so a
           subscription cannot be used by anybody else.
  aes128gcm  the message is encrypted for that one browser (RFC 8291), so
           Apple and Google relay bytes they cannot read. Friday reports what is
           in your Slack and your sessions; that must not become somebody's
           server log.

The keypair is generated once into ~/.friday and never leaves. Subscriptions
live beside it, owner-only.

Push is deliberately rare: only things that need YOU, only when you are not
already looking at the page, never when quiet, and never twice for the same
thing. A phone that buzzes for everything gets its permission revoked, and there
is no second chance at that.
"""

import base64
import json
import os
import struct
import threading
import time
import urllib.parse
import urllib.request

# Optional, and it has to be genuinely optional rather than nearly optional.
# These were plain imports, so on a machine without `cryptography` the failure
# was not "no phone alerts", it was `friday.conversation` failing to import,
# which is every part of Friday including the ones that have nothing to do with
# push. A clean-machine test found it: the README said Friday runs without this
# and says what is missing, and that was simply untrue.
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives.hmac import HMAC
    HAVE_CRYPTO = True
except Exception:                   # pragma: no cover - depends on the machine
    hashes = serialization = ec = AESGCM = HKDFExpand = HMAC = None
    HAVE_CRYPTO = False

from . import connectors            # for CONF_DIR, so tests can redirect it

TTL = 600                # seconds the push service should hold it if offline
TIMEOUT = 8
CONTACT = "mailto:friday@localhost"


def _dir():
    return connectors.CONF_DIR


def _b64(raw: bytes) -> str:
    """base64url, no padding, which is what every part of Web Push uses."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- keys ----
def keys() -> tuple:
    """(private_key, public_bytes). Generated once, then reused forever.

    A new keypair invalidates every existing subscription, so this must be
    stable: regenerating it silently is how push stops working for everybody
    with no error anywhere."""
    path = _dir() / "vapid_key.pem"
    try:
        if path.exists():
            priv = serialization.load_pem_private_key(path.read_bytes(),
                                                      password=None)
        else:
            priv = ec.generate_private_key(ec.SECP256R1())
            _dir().mkdir(parents=True, exist_ok=True)
            path.write_bytes(priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            path.chmod(0o600)
    except Exception:
        priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return priv, pub


def available() -> bool:
    """Whether phone alerts can work here at all."""
    return HAVE_CRYPTO


def public_key() -> str:
    """The applicationServerKey the browser needs, base64url.

    Empty when there is no crypto library: the page reads that as "alerts are
    not available" and hides the bell, rather than offering a button that
    cannot do anything."""
    if not HAVE_CRYPTO:
        return ""
    return _b64(keys()[1])


# ------------------------------------------------------- subscriptions ----
def _subs_path():
    return _dir() / "push_subs.json"


def subscriptions() -> list:
    try:
        return json.loads(_subs_path().read_text())
    except Exception:
        return []


def _save(subs: list) -> None:
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        p = _subs_path()
        p.write_text(json.dumps(subs))
        p.chmod(0o600)
    except Exception:
        pass


def subscribe(sub: dict) -> bool:
    if not HAVE_CRYPTO:
        return False
    """Remember a browser. Returns whether anything changed."""
    if not (isinstance(sub, dict) and sub.get("endpoint")
            and (sub.get("keys") or {}).get("p256dh")
            and (sub.get("keys") or {}).get("auth")):
        return False
    subs = [s for s in subscriptions() if s.get("endpoint") != sub["endpoint"]]
    subs.append({"endpoint": sub["endpoint"], "keys": sub["keys"],
                 "added": time.time()})
    _save(subs)
    return True


def unsubscribe(endpoint: str) -> bool:
    subs = subscriptions()
    left = [s for s in subs if s.get("endpoint") != endpoint]
    if len(left) == len(subs):
        return False
    _save(left)
    return True


# ------------------------------------------------------------ sending ----
def _jwt(endpoint: str, priv) -> str:
    """The VAPID token: this Friday, for this push service, for the next hour."""
    origin = urllib.parse.urlsplit(endpoint)
    aud = f"{origin.scheme}://{origin.netloc}"
    header = _b64(json.dumps({"typ": "JWT", "alg": "ES256"},
                             separators=(",", ":")).encode())
    body = _b64(json.dumps({"aud": aud, "exp": int(time.time()) + 3600,
                            "sub": CONTACT},
                           separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JOSE wants r||s fixed-width, not the DER envelope OpenSSL hands back.
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature)
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{body}.{_b64(raw)}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    h = HMAC(salt, hashes.SHA256())
    h.update(ikm)
    prk = h.finalize()
    return HKDFExpand(algorithm=hashes.SHA256(), length=length,
                      info=info).derive(prk)


def encrypt(plaintext: bytes, p256dh: str, auth: str,
            salt: bytes = None, server_priv=None) -> bytes:
    """RFC 8291 aes128gcm, so only that browser can read it.

    The push service is Apple's or Google's. Friday reports what is in your
    Slack and your sessions, and that must not become somebody else's server
    log, so the relay only ever carries ciphertext."""
    ua_pub_bytes = _unb64(p256dh)
    auth_secret = _unb64(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ua_pub_bytes)
    server_priv = server_priv or ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = server_priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    shared = server_priv.exchange(ec.ECDH(), ua_pub)

    ikm = _hkdf(auth_secret, shared,
                b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02 is the final-record delimiter; one record is enough for a
    # notification, and multi-record framing would be complexity for nothing.
    body = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!I", 4096) + bytes([len(as_pub_bytes)]) \
        + as_pub_bytes
    return header + body


def send_one(sub: dict, payload: dict, urgency: str = "high") -> int:
    """POST one notification. Returns the HTTP status, or 0 if it never left.

    404 and 410 mean the browser threw the subscription away, so it is dropped
    here too rather than retried forever."""
    priv, pub = keys()
    endpoint = sub.get("endpoint", "")
    try:
        body = encrypt(json.dumps(payload).encode(),
                       sub["keys"]["p256dh"], sub["keys"]["auth"])
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("TTL", str(TTL))
        req.add_header("Content-Encoding", "aes128gcm")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Urgency", urgency)
        req.add_header("Authorization", f"vapid t={_jwt(endpoint, priv)},"
                                        f"k={_b64(pub)}")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            unsubscribe(endpoint)
        return e.code
    except Exception:
        return 0


def send(title: str, body: str, url: str = "/", tag: str = "",
         urgency: int = 0) -> int:
    """Notify every subscribed browser. Returns how many were reached.

    `urgency` reaches the push service as its own header, which decides whether
    a phone may hold the message until it next wakes. Sending everything as
    high, which is what this used to do, spends the one lever that keeps a
    locked phone from buzzing over something that could have waited."""
    if not HAVE_CRYPTO:
        return 0
    payload = {"title": title, "body": body[:300], "url": url,
               "tag": tag or "friday"}
    level = "high" if urgency <= 0 else "normal"
    ok = 0
    for sub in subscriptions():
        if send_one(sub, payload, urgency=level) in (200, 201, 202):
            ok += 1
    return ok


def send_async(title: str, body: str, url: str = "/", tag: str = "",
               urgency: int = 0) -> None:
    """Never make a conversation wait on Apple's servers."""
    threading.Thread(target=lambda: send(title, body, url, tag, urgency),
                     daemon=True).start()
