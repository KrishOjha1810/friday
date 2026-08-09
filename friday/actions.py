"""Things Friday can actually do to your machine.

Everything here is confirmed by the user first (conversation.py proposes, this
performs), and everything reports honestly whether it worked. A silent no-op is
the worst outcome for an assistant: you believe a thing happened, and it did
not.

These live in Friday rather than in voicebridge's adapters because they are
about the human's attention (bring this to the front) rather than about
controlling an agent (send it a prompt). If a second target ever needs them,
they should move into the adapter seam.
"""

import subprocess
from pathlib import Path

from . import engine


def _osa(script: str, timeout: float = 6.0) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def focus_session(sid: str) -> bool:
    """Bring the terminal tab running this session to the front.

    Works by tty rather than by window title, because titles are unreliable and
    a tty is exactly the session. Returns False (never raises) when the session
    has no known tty, which is the honest answer: we could not do it."""
    if not (sid and engine.AVAILABLE):
        return False
    try:
        from vb import talkd
        tty = talkd.tty_for_sid(sid)
    except Exception:
        tty = ""
    if not tty:
        return False

    # Terminal.app first, then iTerm2. Both are addressed the same way: find the
    # tab whose tty matches, select it, raise its window, activate the app.
    esc = tty.replace('"', '\\"')
    term = _osa(f'''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{esc}" then
        set selected tab of w to t
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "no"''')
    if term == "ok":
        return True

    iterm = _osa(f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "{esc}" then
          select t
          select w
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"''')
    return iterm == "ok"


def resume_session(sid: str, cwd: str = "") -> bool:
    """Reopen a CLOSED session in a new terminal window, where you left it.

    focus_session only raises a window that already exists. This is the other
    half: 'open that session' about something you finished last Tuesday has to
    actually bring the conversation back, which is `claude --resume <id>` in a
    fresh window."""
    if not sid:
        return False
    where = cwd or str(Path.home())
    cmd = f"cd {_q(where)} && claude --resume {_q(sid)}"
    return _open_terminal(cmd)


def new_session(prompt: str = "", cwd: str = "") -> bool:
    """Start a fresh session, optionally handing it an opening instruction.

    This is how a Slack thread becomes work: Friday read the thread, you said
    'start something on this', and the new session opens already knowing what
    it is for instead of you retyping it."""
    where = cwd or str(Path.home())
    cmd = f"cd {_q(where)} && claude"
    if prompt:
        # Pass it as the opening prompt rather than typing into the TUI, which
        # is racy: the process is not ready the instant the window appears.
        cmd += " " + _q(prompt)
    return _open_terminal(cmd)


def _q(s: str) -> str:
    """Single-quote for the shell. Everything here can contain a person's
    words, so nothing is ever interpolated raw."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _open_terminal(command: str) -> bool:
    """A new Terminal window running `command`. Returns whether it opened."""
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    out = _osa(f'tell application "Terminal"\n'
               f'  do script "{esc}"\n'
               f'  activate\n'
               f'  return "ok"\n'
               f'end tell', timeout=10)
    return out == "ok"


def send_to_session(sid: str, text: str) -> bool:
    """Deliver a prompt to a specific session, using voicebridge's adapter so
    it lands in THAT session's tab rather than whatever is focused."""
    if not (sid and text and engine.AVAILABLE):
        return False
    try:
        return bool(engine.adapters.for_sid(sid).send(sid, text))
    except Exception:
        return False


def interrupt_session(sid: str) -> bool:
    """Stop what an agent is doing (the same Escape you would press)."""
    if not (sid and engine.AVAILABLE):
        return False
    try:
        return bool(engine.adapters.for_sid(sid).interrupt(sid))
    except Exception:
        return False
