"""Telling the transcriber the names it is about to hear.

whisper takes an initial prompt that biases decoding, and voicebridge has always
read one from ~/.voicebridge/vocab. Nothing ever wrote that file. Friday is the
only process that knows the real names, and it kept them to itself while speech
recognition mangled every one of them: a channel called moonshot came back as
"Munsheer", "moon shot" and "moon of shot" on consecutive tries.

Fixing it afterwards, which nearest.py does, is strictly worse than not mangling
it in the first place.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import vocab  # noqa: E402

vocab.VOCAB = TMP / "vocab"           # never the real one


class _Slack:
    ok = True

    def ready(self):
        return self.ok

    def channel_names(self, limit=30):
        return ["moonshot", "general", "it-and-network", "random",
                "mpdm-alice--bob--carol-1", "dev-requests"]


def _sources(labels=("voicebridge", "api"), slack=True):
    vocab.fleetcache = type("F", (), {"snapshot": staticmethod(
        lambda: {str(i): {"label": l} for i, l in enumerate(labels)})})
    vocab.memory = type("M", (), {"project_names": staticmethod(
        lambda with_sessions=False: ["promptguard", "jobhunt"])})
    vocab.connectors = type("C", (), {"get": staticmethod(
        lambda n: _Slack() if slack else None)})


def test_the_names_you_would_actually_say_are_offered():
    _sources()
    got = vocab.names()
    for want in ("voicebridge", "api", "promptguard", "jobhunt", "moonshot"):
        assert want in got, f"{want} missing from {got}"


def test_ordinary_english_is_left_out():
    """whisper already knows "general" and "random". Those slots belong to the
    words it actually gets wrong, and an unlikely term measurably degrades
    recognition of everything else."""
    _sources()
    got = [g.lower() for g in vocab.names()]
    assert "general" not in got and "random" not in got, got


def test_slack_group_dm_names_are_left_out():
    """"mpdm-alice--bob--carol-1" is never said out loud and would burn several
    slots."""
    _sources()
    assert not [g for g in vocab.names() if g.startswith("mpdm")], vocab.names()


def test_the_list_stays_under_the_prompt_window():
    """whisper's window is about 224 tokens and overflow is dropped from the
    FRONT, so an over-long list silently truncates the terms you just added."""
    _sources(labels=[f"session-number-{i}" for i in range(80)])
    text = vocab.line()
    assert len(text) <= vocab.MAX_CHARS, len(text)
    assert len(vocab.names()) <= vocab.MAX_NAMES


def test_what_is_running_comes_before_what_is_merely_known():
    """The list is cut at the end, so order is priority, and the session you
    are looking at right now is likelier to be said than a project from March."""
    _sources()
    got = vocab.names()
    assert got.index("voicebridge") < got.index("promptguard"), got


def test_it_is_a_sentence_rather_than_a_word_dump():
    _sources()
    text = vocab.line()
    assert text.startswith("Names that may be said:") and text.endswith("."), text


def test_writing_twice_does_not_touch_the_file_twice():
    """A rewrite for no reason invalidates whisper's prompt cache."""
    _sources()
    vocab.write(force=True)
    first = vocab.VOCAB.stat().st_mtime_ns
    vocab.write(force=True)
    assert vocab.VOCAB.stat().st_mtime_ns == first, "rewrote identical content"


def test_nothing_to_say_writes_nothing():
    """An empty vocabulary file would be worse than none: it is a prompt that
    says a list is coming and then does not deliver one."""
    _sources(labels=(), slack=False)
    vocab.memory = type("M", (), {"project_names": staticmethod(
        lambda with_sessions=False: [])})
    assert vocab.line() == ""


def test_a_broken_source_never_stops_the_rest():
    _sources()
    vocab.connectors = type("C", (), {"get": staticmethod(
        lambda n: (_ for _ in ()).throw(RuntimeError("slack is down")))})
    got = vocab.names()
    assert "voicebridge" in got, got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  vocab: the real names reach the decoder, and nothing else does")
