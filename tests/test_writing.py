"""Friday can act, and the whole point is the distance between an inference and
an action.

Reading somebody's Slack is a privacy question. Writing to it is a question about
what arrives, under your name, in a channel of your colleagues. So writing is a
separate capability with its own switch, off by default, revocable on its own,
and there is no path from a sentence to a sent message that does not pass through
you reading the exact words first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import connectors, conversation as C  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


def _friday(writing=False):
    connectors.allow_write(writing)
    connectors.gh_allow_write(writing)
    f = Friday()
    f.announce = lambda *a, **k: None
    f.inbox.last = {"C1": {"where": "#eng", "who": "Sam", "channel": "C1",
                           "text": "can you confirm Thursday?"}}
    return f


def test_writing_is_off_until_you_say_otherwise():
    connectors.allow_write(False)
    assert connectors.can_write() is False
    assert connectors.gh_can_write() is False
    sl = connectors.Slack()
    assert sl.post("C1", "hello")["error"] == "writing_not_enabled"


def test_the_permission_is_only_requested_when_you_ask_for_it():
    """The Slack app is built with read scopes only. chat:write appears in the
    manifest only after you have turned writing on, so a default install cannot
    post even if something later went wrong here."""
    connectors.allow_write(False)
    assert "chat:write" not in connectors._manifest("http://x/y")
    connectors.allow_write(True)
    assert "chat:write" in connectors._manifest("http://x/y")
    connectors.allow_write(False)


def test_sending_is_never_inferred_from_a_sentence():
    """"send the report to Sam" is a thing you might say while thinking out
    loud. Only a bare yes to a draft on screen means send."""
    for said in ("send the report to sam", "I should send that email",
                 "can you send this to the team later"):
        assert classify(said)[0] != "send", said
    for said in ("send it", "post it", "go ahead and send it"):
        assert classify(said)[0] == "send", said


def test_nothing_goes_out_without_a_draft_you_have_seen():
    f = _friday(writing=True)
    r = f.handle("send it")
    assert "nothing drafted" in r["reply"].lower(), r["reply"]


def test_a_draft_is_sent_exactly_as_written():
    """Not paraphrased, not regenerated. What you approved is what arrives."""
    f = _friday(writing=True)
    f._last_draft = {"text": "Thursday works, 4pm.", "where": "#eng",
                     "channel": "C1", "who": "Sam"}
    sent = {}
    connectors.get("slack").post = lambda ch, text, thread_ts="": (
        sent.update(ch=ch, text=text) or {"ok": True})
    r = f.handle("send it")
    assert sent == {"ch": "C1", "text": "Thursday works, 4pm."}, sent
    assert "sent to #eng" in r["reply"].lower(), r["reply"]


def test_with_writing_off_a_draft_is_never_sent():
    f = _friday(writing=False)
    f._last_draft = {"text": "hello", "where": "#eng", "channel": "C1",
                     "who": "Sam"}
    sent = []
    connectors.get("slack").post = lambda *a, **k: sent.append(1) or {"ok": True}
    r = f.handle("send it")
    assert not sent, "posted while writing was switched off"
    assert "let yourself post" in r["reply"], r["reply"]


def test_a_refusal_from_slack_says_nothing_was_sent():
    """"It didn't go" and "it might have gone" are different, and only one is
    safe to act on."""
    f = _friday(writing=True)
    f._last_draft = {"text": "hi", "where": "#eng", "channel": "C1",
                     "who": "M"}
    connectors.get("slack").post = lambda *a, **k: {"ok": False,
                                                    "error": "missing_scope"}
    r = f.handle("send it")
    assert "nothing was sent" in r["reply"].lower(), r["reply"]
    assert "connect slack" in r["reply"].lower(), "no way to fix it offered"


def test_it_can_be_switched_off_again_on_its_own():
    """Revoking writing must not tear down reading: those are different risks
    and you should be able to change your mind about one of them."""
    f = _friday(writing=True)
    f.handle("turn off posting")
    assert connectors.can_write() is False
    assert connectors.gh_can_write() is False
    sl = connectors.get("slack")
    assert hasattr(sl, "read_channel"), "reading was taken away too"


def test_what_friday_says_it_can_do_tracks_the_switch():
    """A fixed list goes stale the moment the setting changes, and a stale list
    is how it starts claiming it cannot do something it just did."""
    f = _friday(writing=False)
    _can, cannot = f._abilities()
    assert any("post or send" in c for c in cannot), cannot
    connectors.allow_write(True)
    can, _cannot = f._abilities()
    assert any("send a Slack message" in c for c in can), can
    connectors.allow_write(False)


def test_github_comments_are_behind_the_same_switch():
    connectors.gh_allow_write(False)
    gh = connectors.get("github")
    assert gh.comment("42", "looks good")["error"] == "writing_not_enabled"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    connectors.allow_write(False)
    connectors.gh_allow_write(False)
    print("ok  writing: off by default, shown before sending, sent verbatim")
