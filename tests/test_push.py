"""Reaching a locked phone, and the four reasons not to.

Push is the one part of Friday that can lose its own permission. A phone that
buzzes for things you are already reading, or twice for the same thing, or at
all while you asked for quiet, gets its notifications switched off, and there is
no second chance at that. So the gates matter more than the sending.
"""

import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from friday import conversation as C, push  # noqa: E402
from friday.conversation import Friday  # noqa: E402


def _browser():
    """A subscription, as a real browser would produce one."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return priv, pub, auth, {
        # A real push service host: an arbitrary endpoint is refused now,
        # because anything accepted here receives every future alert.
        "endpoint": "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "keys": {"p256dh": push._b64(pub), "auth": push._b64(auth)}}


def _decrypt(blob, ua_priv, ua_pub, auth):
    salt, idlen = blob[:16], blob[20]
    as_pub, ct = blob[21:21 + idlen], blob[21 + idlen:]
    shared = ua_priv.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), as_pub))
    ikm = push._hkdf(auth, shared,
                     b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = push._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = push._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, ct, None).rstrip(b"\x02")


def test_only_the_phone_can_read_the_message():
    """Friday reports what is in your Slack and your sessions. The relay is
    Apple's or Google's, so it must carry ciphertext they cannot open."""
    priv, pub, auth, sub = _browser()
    secret = json.dumps({"title": "api", "body": "the staging password is hunter2"})
    blob = push.encrypt(secret.encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    assert b"hunter2" not in blob, "the payload went out in the clear"
    assert json.loads(_decrypt(blob, priv, pub, auth)) == json.loads(secret)


def test_the_identity_token_is_well_formed():
    """A malformed VAPID token is rejected by the push service with a 401 and
    no other explanation, which is a miserable thing to debug later."""
    priv, _pub = push.keys()
    tok = push._jwt("https://web.push.apple.com/xyz", priv)
    head, body, sig = tok.split(".")
    assert json.loads(push._unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(push._unb64(body))
    assert claims["aud"] == "https://web.push.apple.com", claims
    assert claims["exp"] > time.time(), "already expired"
    # JOSE wants raw r||s, not the DER envelope OpenSSL returns
    assert len(push._unb64(sig)) == 64, "signature is DER, not JOSE"


def test_the_key_survives_a_restart():
    """A new keypair silently invalidates every subscription, so push stops
    working for everybody with no error anywhere."""
    first = push.public_key()
    assert push.public_key() == first
    assert len(push._unb64(first)) == 65, "not an uncompressed P-256 point"


def test_a_subscription_must_actually_be_one():
    assert push.subscribe({}) is False
    assert push.subscribe({"endpoint": "https://x/y"}) is False, "no keys"
    _p, _u, _a, sub = _browser()
    assert push.subscribe(sub) is True


def test_only_a_real_push_service_may_be_registered():
    """Nothing checked where a subscription pointed, so any endpoint at all
    could be registered and would then receive every alert Friday sends,
    encrypted to a key the registrant chose. Slack contents, what an agent is
    asking, all of it, and it survived a restart."""
    _p, _u, _a, good = _browser()
    for bad in ("http://127.0.0.1:8908/relay",          # not https
                "https://evil.example/relay",           # not a push service
                "https://fcm.googleapis.com.evil.test/x",   # lookalike host
                "ftp://push.example/x", ""):
        assert push.subscribe(dict(good, endpoint=bad)) is False, bad
    for ok in ("https://updates.push.services.mozilla.com/wpush/v2/a",
               "https://fcm.googleapis.com/fcm/send/a",
               "https://web.push.apple.com/a"):
        assert push.subscribe(dict(good, endpoint=ok)) is True, ok


def test_the_same_browser_is_stored_once():
    push.forget_all() if hasattr(push, "forget_all") else None
    for s in list(push.subscriptions()):
        push.unsubscribe(s.get("endpoint", ""))
    _p, _u, _a, sub = _browser()
    assert push.subscribe(sub) is True
    assert push.subscribe(sub) is True and len(push.subscriptions()) == 1, \
        "subscribing twice stored the same browser twice"


# ---- the gates -------------------------------------------------------------

def _friday():
    f = Friday()
    sent = []
    push.send_async = lambda title, body, url="/", tag="", urgency=0: (
        sent.append((title, body, tag, urgency)))
    return f, sent


def test_something_you_are_looking_at_is_not_pushed():
    """The fastest way to lose notification permission is to buzz a phone about
    a message already on the screen in front of the person."""
    f, sent = _friday()
    f.watching, f.watching_at = True, time.time()
    f.announce("api says: blocked", items=[{"sid": "s", "label": "api",
                                            "kind": "blocked"}])
    assert sent == [], sent


def test_quiet_covers_the_phone_too():
    f, sent = _friday()
    f.quiet = True
    f.announce("api says: blocked", items=[{"sid": "s", "label": "api",
                                            "kind": "blocked"}])
    assert sent == [], sent


def test_only_things_that_need_you_reach_the_phone():
    """An agent finishing a thought is worth a line in the thread and is not
    worth a locked phone lighting up."""
    f, sent = _friday()
    f.announce("api says: done with the refactor",
               items=[{"sid": "s", "label": "api", "kind": "spoke"}])
    assert sent == [], "pushed a routine update"
    f.announce("api says: blocked. It's asking: force-push?",
               items=[{"sid": "s", "label": "api", "kind": "blocked"}])
    assert len(sent) == 1, sent
    assert "api" in sent[0][0].lower(), sent[0]
    assert sent[0][3] == 0, "an agent blocked on you was sent as low priority"


def test_the_same_thing_does_not_buzz_twice():
    """An agent that repeats itself must not become a phone that repeats
    itself."""
    f, sent = _friday()
    for _ in range(5):
        f.announce("api says: blocked", items=[{"sid": "s", "label": "api",
                                                "kind": "blocked"}])
    assert len(sent) == 1, sent


def test_a_slack_message_reaches_you_but_a_fleet_summary_does_not():
    f, sent = _friday()
    f.announce("Sam in #eng: are you free Thursday?",
               items=[{"sid": "", "label": "#eng", "kind": "slack"}])
    assert len(sent) == 1, sent
    f.announce("3 repos have unpushed commits",
               items=[{"sid": "", "label": "git", "kind": "git"}])
    assert len(sent) == 1, "a background note buzzed the phone"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  push: encrypted end to end, and it stays quiet unless it matters")


def test_urgency_reaches_the_push_service():
    """The header decides whether a locked phone may hold the message until it
    next wakes. Sending everything as high, which is what this used to do,
    spends the one lever that keeps a phone from buzzing over something that
    could have waited."""
    seen = []
    real = push.send_one
    push.send_one = lambda sub, payload, urgency="high": seen.append(urgency) or 201
    _p, _u, _a, sub = _browser()
    push.subscribe(sub)
    try:
        push.send("api needs you", "blocked", urgency=0)
        push.send("a slack message", "later", urgency=1)
    finally:
        push.send_one = real
    assert seen == ["high", "normal"], seen


def test_a_person_in_slack_is_not_as_urgent_as_a_blocked_agent():
    f, sent = _friday()
    f.announce("Sam in #eng: are you free Thursday?",
               items=[{"sid": "", "label": "#eng", "kind": "slack"}])
    assert len(sent) == 1, sent
    assert sent[0][3] == 1, "a chat message was sent at maximum urgency"
