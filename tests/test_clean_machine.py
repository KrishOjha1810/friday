"""Friday on a machine that is not this one.

Everything else here runs against a checkout that has been lived in: a config
directory full of tokens, a `gh` already signed in, voicebridge installed,
sessions on disk. None of that is true for the next person, and every one of
those is something Friday could be silently depending on without anybody
noticing.

So this copies the source somewhere else, points HOME at an empty directory,
and starts it the way the README says to. The bar is not that everything works,
because nothing is configured. The bar is that it starts, serves the page,
answers a question, and says what is missing rather than failing oddly.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8841


def _fresh():
    """The source, and nothing else. No config, no tokens, no history."""
    tmp = Path(tempfile.mkdtemp(prefix="friday-clean-"))
    dest = tmp / "friday"
    dest.mkdir()
    for item in ("friday", "static", "run.py", "README.md", "START-HERE.md"):
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, dest / item)
        else:
            shutil.copy2(src, dest / item)
    home = tmp / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    # A stranger has none of these, and a token leaking in from the developer's
    # shell would make this test pass for the wrong reason.
    for k in ("SLACK_TOKEN", "LINEAR_TOKEN", "JIRA_TOKEN", "SENTRY_TOKEN",
              "GITLAB_TOKEN", "GITHUB_TOKEN", "PYTHONPATH"):
        env.pop(k, None)
    return dest, home, env


def test_the_doctor_runs_before_anything_is_set_up():
    """It is the first thing a new person types, so it has to work when nothing
    else does, and it must not exit non-zero for missing optional pieces: that
    reads as "setup failed" when Friday genuinely runs without them."""
    dest, _home, env = _fresh()
    r = subprocess.run([sys.executable, "run.py", "--check"], cwd=str(dest),
                       env=env, capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "python 3.9+" in out, out
    for line in out.splitlines():
        if line.strip().startswith("NO "):
            assert "fix:" in out, "said what was missing but not how to fix it"
    assert "run.py" in out, "did not say what to do next"


def test_it_starts_and_serves_the_page_with_nothing_configured():
    dest, home, env = _fresh()
    p = subprocess.Popen([sys.executable, "run.py", str(PORT)], cwd=str(dest),
                         env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    try:
        up = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/health", timeout=1) as r:
                    up = r.status == 200
                    break
            except Exception:
                if p.poll() is not None:
                    out = p.stdout.read().decode(errors="ignore")
                    raise AssertionError("it exited on a clean machine:\n" + out)
                time.sleep(0.25)
        assert up, "never came up"

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5) as r:
            page = r.read().decode(errors="ignore")
        assert "<title" in page.lower() and len(page) > 2000, len(page)

        # It answers. "help" specifically, because it is the likeliest first
        # word and it is the one that must work with no model loaded.
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/say",
            data=json.dumps({"text": "help"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            said = json.loads(r.read())
        assert said.get("reply", "").strip(), said
        assert "what should I work on" in said["reply"], said["reply"]

        # And it wrote its config into the fresh HOME rather than the real one.
        assert (home / ".friday").exists(), "no config directory created"
    finally:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            p.terminate()
        time.sleep(0.5)
        shutil.rmtree(dest.parent, ignore_errors=True)


def test_it_does_not_reach_into_the_developers_home():
    """The config path is built from HOME. If any of it were hard-coded to this
    machine, a stranger's Friday would read tokens that are not theirs, or
    more likely find nothing and behave strangely with no explanation."""
    dest, home, env = _fresh()
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "from friday import connectors, server;"
         "print(connectors.CONF_DIR); print(server.SECRET_FILE)"],
        cwd=str(dest), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for line in r.stdout.strip().splitlines():
        assert line.startswith(str(home)), f"{line} is not under the fresh HOME"
    shutil.rmtree(dest.parent, ignore_errors=True)


def test_the_readme_commands_are_the_ones_that_exist():
    """A start guide whose first command is wrong costs you the reader."""
    for doc in ("README.md", "START-HERE.md"):
        text = (ROOT / doc).read_text()
        for flag in ("--app", "--phone", "--check"):
            if f"run.py {flag}" in text:
                got = subprocess.run(
                    [sys.executable, "-c",
                     f"import pathlib,sys;"
                     f"src=pathlib.Path('run.py').read_text();"
                     f"sys.exit(0 if {flag!r} in src else 1)"],
                    cwd=str(ROOT), capture_output=True)
                assert got.returncode == 0, f"{doc} documents {flag}, run.py has no such flag"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  clean machine: starts, serves, answers, with nothing set up")
