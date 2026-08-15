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


# ------------------------------------------------------------- the fleet ----
VENDORS = [Claude(), Codex()]


def available() -> list:
    """Vendors that could actually report something on this machine."""
    live = []
    for v in VENDORS:
        try:
            if v.name == "claude" and engine.AVAILABLE:
                live.append(v)
            elif v.name == "codex" and Codex.ROOT.exists():
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
