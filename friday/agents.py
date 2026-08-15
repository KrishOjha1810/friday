"""Every coding agent on this machine, whoever made it.

Friday conducted Claude Code and nothing else, on a Mac that has Codex installed
with its sessions sitting on disk. The plan always said vendor-neutral, and the
risk register says the defence against Anthropic shipping this themselves is
supporting the competition, so this is not a nicety.

Sensing is the hard half and it is genuinely per-vendor: everybody stores their
transcripts differently. Conducting is not, because it ends in the same place
either way, a prompt typed into a terminal that owns that session.

So a vendor answers three questions and nothing else:

    sessions()      what exists, in the shape the rest of Friday already uses
    last_said(row)  the last thing the agent said, not what was said to it
    resume(row)     the command that brings a closed one back

Everything above this file is vendor-blind. Adding the next agent is a file
format, not a redesign.
"""

import json
import os
import re
import time
from pathlib import Path

from . import engine

# The shape every source returns, which is voicebridge's fleet row plus a
# `vendor`, so nothing downstream had to change to gain a second vendor.
#   sid, label, status, path, question, topic, cwd, mtime, vendor


# ---------------------------------------------------------------- Claude ----
class Claude:
    """Through voicebridge, which already senses Claude Code properly."""

    name = "claude"

    def sessions(self) -> list:
        if not engine.AVAILABLE:
            return []
        try:
            rows = list(engine.fleet.snapshot().values())
        except Exception:
            return []
        for r in rows:
            r.setdefault("vendor", self.name)
        return rows

    def last_said(self, row: dict) -> str:
        from . import replies
        return replies.last_said(row.get("path", ""))

    def resume(self, row: dict) -> list:
        return ["claude", "--resume", row.get("sid", "")]


# ----------------------------------------------------------------- Codex ----
class Codex:
    """Codex CLI, read from its rollout files.

    `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl` holds one JSON
    object per line: `session_meta` carries the id and working directory,
    `response_item` carries the messages, and `event_msg` marks a turn starting
    and finishing. That is everything needed to say what a session is, what it
    last said, and whether it is still going."""

    name = "codex"
    ROOT = Path.home() / ".codex" / "sessions"
    INDEX = Path.home() / ".codex" / "session_index.jsonl"
    WORKING_FOR = 90        # seen in the last minute and a half means live

    def _names(self) -> dict:
        """What Codex calls its own threads.

        It keeps a proper name for each ("Apply to Bitpanda role") while the
        working directory gives you things like
        "2026-05-31-senior-software-engineer-react-broker-web", which is not a
        name anybody would say out loud, and Friday is a thing you talk to."""
        out = {}
        try:
            for line in self.INDEX.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("id") and d.get("thread_name"):
                    out[d["id"]] = str(d["thread_name"]).strip()
        except Exception:
            return {}
        return out

    def _files(self, limit: int = 40) -> list:
        try:
            files = [(p.stat().st_mtime, p)
                     for p in self.ROOT.glob("*/*/*/rollout-*.jsonl")]
        except Exception:
            return []
        files.sort(reverse=True)
        return [p for _m, p in files[:limit]]

    def _read(self, path: Path) -> dict:
        """One session, summarised from its rollout. Bounded: these grow."""
        meta, said, asked, first_user = {}, "", "", ""
        last_event = ""
        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    kind = d.get("type")
                    payload = d.get("payload") or {}
                    if kind == "session_meta":
                        meta = payload
                    elif kind == "event_msg":
                        last_event = payload.get("type") or last_event
                    elif kind == "response_item" and payload.get("type") == "message":
                        role = payload.get("role") or ""
                        text = " ".join(
                            b.get("text", "") for b in (payload.get("content") or [])
                            if isinstance(b, dict) and b.get("text"))
                        text = " ".join(text.split())
                        if not text or text.startswith("<"):
                            continue
                        if role == "assistant":
                            said = text
                        elif role == "user":
                            if not first_user:
                                first_user = text[:110]
        except Exception:
            return {}
        if not meta:
            return {}
        return {"meta": meta, "said": said, "asked": asked,
                "about": first_user, "event": last_event}

    def sessions(self) -> list:
        rows = []
        named = self._names()
        for path in self._files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            got = self._read(path)
            if not got:
                continue
            meta = got["meta"]
            # A turn that started and never ended, recently, is still running.
            working = (got["event"] == "task_started"
                       and time.time() - mtime < self.WORKING_FOR)
            cwd = meta.get("cwd") or ""
            sid = meta.get("id") or path.stem
            # Its own name first, then the folder, then the id: in that order
            # because that is the order of how likely you are to recognise it.
            label = named.get(sid) or (Path(cwd).name if cwd else sid[:8])
            if len(label) > 34:
                label = label[:34].rstrip(" -_") + "…"
            rows.append({
                "sid": sid,
                "label": label or "codex",
                "status": "working" if working else "idle",
                "path": str(path),
                "question": "",          # Codex does not mark these on disk
                "topic": got["about"],
                "cwd": cwd,
                "mtime": mtime,
                "vendor": self.name,
            })
        return rows

    def last_said(self, row: dict) -> str:
        got = self._read(Path(row.get("path", "")))
        return got.get("said", "") if got else ""

    def resume(self, row: dict) -> list:
        return ["codex", "resume", row.get("sid", "")]


# ----------------------------------------------------- Google Antigravity ----
class Antigravity:
    """Google's Antigravity, read from the markdown it leaves on disk.

    The conversation itself lives in a SQLite file per thread whose payloads are
    protobuf with no schema shipped alongside, and guessing at protobuf wire
    format to read somebody's chat is the kind of thing that works until the day
    it silently does not. But Antigravity also writes three markdown files per
    conversation, on purpose, for you to read:

        implementation_plan.md   what it intends to do, and what it needs to ask
        task.md                  a checklist, with [x] as it goes
        walkthrough.md           what it ended up doing

    That is a better sensing surface than either Claude or Codex offers, not a
    worse one. A transcript tells you what an agent said; a checklist tells you
    how far through it is, which is the thing you actually wanted to know when
    you asked what it was doing. So this vendor can say "4 of 7 done" where the
    others can only say "still going".

    The plan file also carries `> [!QUESTION]` blocks, which is Antigravity
    stating plainly that it is blocked on you. That maps straight onto the one
    category Friday treats as urgent.
    """

    name = "antigravity"
    ROOT = Path.home() / ".gemini" / "antigravity" / "brain"
    WORKING_FOR = 300        # touched in the last five minutes
    # Antigravity keeps every conversation forever, and Friday's fleet is meant
    # to be what is live right now. Codex is bounded the same way, by taking the
    # forty newest rollouts; this is the same bound expressed in days, because
    # these are directories rather than a sorted list of files.
    STALE_AFTER = 7 * 86400

    def _title(self, d: Path) -> str:
        """What the thread is about.

        Its own summary first: the metadata file carries a written one ("Task
        checklist for Hero section enhancements") while the directory is a
        UUID, and a UUID is not something you can say out loud to a thing you
        talk to."""
        for f in ("task.md", "implementation_plan.md", "walkthrough.md"):
            try:
                got = json.loads((d / (f + ".metadata.json")).read_text())
                if got.get("summary"):
                    return str(got["summary"]).strip()
            except Exception:
                continue
        try:
            for line in (d / "implementation_plan.md").read_text(
                    errors="ignore").splitlines():
                if line.startswith("# "):
                    return line[2:].strip()
        except Exception:
            pass
        return ""

    def _progress(self, d: Path) -> tuple:
        """How far through its own checklist it is: (done, total)."""
        try:
            text = (d / "task.md").read_text(errors="ignore")
        except Exception:
            return (0, 0)
        done = len(re.findall(r"`?\[x\]`?", text, re.I))
        todo = len(re.findall(r"`?\[ \]`?", text))
        return (done, done + todo)

    def _asking(self, d: Path) -> str:
        """What it is waiting on you for, if anything.

        Guarded on file order, and that guard is the whole difficulty here.
        Questions stay in the plan file after you answer them in the app, so
        reading the file alone would report a question you settled days ago as
        blocking, forever, at the one urgency level that is exempt from the
        budget. If the checklist or the walkthrough has been written SINCE the
        plan, work moved on and the questions went with it."""
        plan = d / "implementation_plan.md"
        try:
            when = plan.stat().st_mtime
        except OSError:
            return ""
        for other in ("task.md", "walkthrough.md"):
            try:
                if (d / other).stat().st_mtime > when + 1:
                    return ""
            except OSError:
                continue
        try:
            text = plan.read_text(errors="ignore")
        except Exception:
            return ""
        if "[!QUESTION]" not in text and "[!IMPORTANT]" not in text:
            return ""
        # The lines after the marker, minus the blockquote furniture.
        out, taking = [], False
        for line in text.splitlines():
            bare = line.lstrip("> ").strip()
            if "[!QUESTION]" in line or "[!IMPORTANT]" in line:
                taking = True
                continue
            if taking:
                if not bare or not line.lstrip().startswith(">"):
                    if out:
                        break
                    continue
                out.append(re.sub(r"^\d+\.\s*", "", bare))
        return " ".join(out)[:300]

    def sessions(self) -> list:
        rows = []
        try:
            dirs = [d for d in self.ROOT.iterdir() if d.is_dir()]
        except Exception:
            return []
        for d in dirs:
            files = [f for f in ("implementation_plan.md", "task.md",
                                 "walkthrough.md") if (d / f).exists()]
            if not files:
                continue
            mtime = max((d / f).stat().st_mtime for f in files)
            if time.time() - mtime > self.STALE_AFTER:
                continue
            asked = self._asking(d)
            done, total = self._progress(d)
            label = self._title(d) or d.name[:8]
            if len(label) > 34:
                label = label[:34].rstrip(" -_") + "…"
            topic = label
            if total:
                topic = f"{label} ({done} of {total} done)"
            # Every box ticked means done, whatever the clock says. For the
            # other two vendors "recently touched" is the only evidence there
            # is, so a finished agent looks busy for five minutes after it
            # stops. Antigravity states its own completion, and stated beats
            # inferred.
            finished = total > 0 and done == total
            rows.append({
                "sid": d.name,
                "label": label,
                "status": ("needs" if asked else
                           "idle" if finished else
                           "working" if time.time() - mtime < self.WORKING_FOR
                           else "idle"),
                "path": str(d / "walkthrough.md"),
                "question": asked,
                "topic": topic,
                "cwd": "",
                "mtime": mtime,
                "vendor": self.name,
            })
        return rows

    def last_said(self, row: dict) -> str:
        """The walkthrough, which is Antigravity's own account of what it did,
        falling back to the plan when it has not finished anything yet."""
        d = Path(row.get("path", "")).parent
        for f in ("walkthrough.md", "implementation_plan.md"):
            try:
                text = (d / f).read_text(errors="ignore")
            except Exception:
                continue
            # Prose only: the headings, images and admonition markers are
            # layout, and reading layout aloud is how a summary becomes noise.
            body = [l.strip() for l in text.splitlines()
                    if l.strip() and not l.lstrip().startswith(
                        ("#", "!", ">", "|", "```", "---"))]
            if body:
                return " ".join(body)[:1200]
        return ""

    def resume(self, row: dict) -> list:
        """Antigravity is an IDE, not a terminal, so there is no session to
        resume into. Opening the app is the honest best effort, and Friday's
        conducting verbs check this: a vendor that cannot be typed into is
        reported as such rather than being sent a prompt that goes nowhere."""
        return ["open", "-a", "Antigravity"]

    conducts = False        # can be read, cannot be told things


# ------------------------------------------------------------- the fleet ----
VENDORS = [Claude(), Codex(), Antigravity()]


def available() -> list:
    """Vendors that could actually report something on this machine."""
    live = []
    for v in VENDORS:
        try:
            if v.name == "claude" and engine.AVAILABLE:
                live.append(v)
            elif v.name == "codex" and Codex.ROOT.exists():
                live.append(v)
            elif v.name == "antigravity" and Antigravity.ROOT.exists():
                live.append(v)
        except Exception:
            continue
    return live


def sessions() -> dict:
    """Everything, keyed by session id, in one shape.

    A vendor that fails is skipped rather than taking the others down: one
    agent's file format changing under us must not blind Friday to the rest."""
    out = {}
    for v in available():
        try:
            for row in v.sessions():
                if row.get("sid"):
                    out[row["sid"]] = row
        except Exception as e:
            engine.log(f"friday agents ({v.name}): {e}")
    return out


def vendor_of(row: dict):
    want = (row or {}).get("vendor", "claude")
    for v in VENDORS:
        if v.name == want:
            return v
    return VENDORS[0]


def last_said(row: dict) -> str:
    """What that agent last said, whoever made it."""
    try:
        return vendor_of(row).last_said(row) or ""
    except Exception:
        return ""


def resume_command(row: dict) -> list:
    return vendor_of(row).resume(row)


def can_conduct(row: dict) -> bool:
    """Whether Friday can type into this one, as opposed to only read it.

    Not every agent has a terminal. Antigravity is an IDE, and pretending to
    send it a message would produce the worst failure Friday has: a confident
    "sent" for something that went nowhere, discovered hours later."""
    return bool(getattr(vendor_of(row), "conducts", True))
