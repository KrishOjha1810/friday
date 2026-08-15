"""The page's own logic, run rather than eyeballed.

A browser file usually gets checked by looking at it, which is how a regex that
turns three options into one button survives. Node is present on this machine,
so the parts with real rules get run.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _js() -> str:
    s = PAGE.read_text()
    return s[s.index("<script>") + 8:s.rindex("</script>")]


def test_the_page_is_valid_javascript():
    """A syntax error here is a blank screen with nothing in any log Friday
    can see."""
    if not shutil.which("node"):
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_js())
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


def _suggest(question: str):
    if not shutil.which("node"):
        return None
    js = _js()
    body = js[js.index("function suggestionsFor"):js.index("function openPeek")]
    prog = body + f"\nconsole.log(JSON.stringify(suggestionsFor({json.dumps(question)})));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(prog)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    return json.loads(r.stdout.strip())


def test_options_the_agent_offered_come_back_as_separate_answers():
    got = _suggest("Pick one: 1) rebase 2) merge 3) leave it")
    if got is None:
        return
    assert got == ["rebase", "merge", "leave it"], got


def test_a_yes_no_question_gets_yes_and_no():
    got = _suggest("Should I force-push the rebase?")
    if got is None:
        return
    assert got[:2] == ["Yes", "No"], got


def test_a_statement_gets_no_buttons():
    """Not everything an agent says is a question, and inventing answers to a
    statement puts a button under something nobody asked."""
    got = _suggest("I finished the migration and all tests pass.")
    if got is None:
        return
    assert got == [], got


def test_no_answer_is_offered_that_was_never_on_the_table():
    """This is the one that matters. A suggested reply is a thing you will tap
    without rereading, so an invented option is worse than no options."""
    q = "Pick one: 1) rebase 2) merge"
    got = _suggest(q)
    if got is None:
        return
    low = q.lower()
    for opt in got:
        assert opt.lower().split()[0] in low, f"invented: {opt}"


def test_never_more_than_four():
    got = _suggest("Choose: 1) a thing 2) b thing 3) c thing 4) d thing "
                   "5) e thing 6) f thing")
    if got is None:
        return
    assert len(got) <= 4, got


def test_the_panel_and_target_chip_exist_in_the_markup():
    """The known-gaps list said these were missing; this is what says they are
    not, and fails if either is deleted."""
    html = PAGE.read_text()
    for needed in ('id="peek"', 'id="peekAsk"', 'id="peekSuggest"',
                   'id="peekReply"', 'id="target"'):
        assert needed in html, needed


def test_tapping_a_session_opens_the_panel_rather_than_asking_the_chat():
    """It used to type "what does api need?" and wait for a round trip to be
    told something Friday already knew."""
    js = _js()
    assert "openPeek(r)" in js, "the fleet chip no longer opens the panel"
    assert "what does ' + r.label" not in js, "still round-tripping the question"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  page: parses, and suggests only what was actually offered")


def test_it_is_an_installable_app_not_just_a_page():
    """"Open a browser tab and keep it open" is not a thing people adopt.
    Friday already had the two hard parts, a service worker and Web Push; what
    it lacked was the manifest and icons that make it something with an icon on
    your home screen."""
    import json
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text()
    assert 'rel="manifest"' in html, "no manifest linked"
    assert "apple-touch-icon" in html, "iOS would install a blank square"
    manifest = json.loads((root / "static" / "manifest.json").read_text())
    assert manifest["display"] == "standalone", manifest["display"]
    for icon in manifest["icons"]:
        assert (root / "static" / icon["src"]).exists(), icon["src"]
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"]), \
        "Android will letterbox the icon without a maskable one"


def test_the_home_screen_shortcuts_actually_go_somewhere():
    """A shortcut that opens a blank conversation is worse than no shortcut."""
    import json
    import re as _re
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "static" / "manifest.json").read_text())
    js = _js()
    assert "get('ask')" in js, "the page ignores the shortcut parameter"
    for short in manifest.get("shortcuts", []):
        assert "ask=" in short["url"], short
