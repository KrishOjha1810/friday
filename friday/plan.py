"""A plan that survives, and runs one step at a time.

This is the difference between Friday and a very good intercom. Until now every
instruction was a single shot: you said a thing, an agent did a thing, and if
you closed the tab or the Mac slept, nothing remembered what you were in the
middle of.

A plan is an ordered list of steps against one session, with a state each. It
lives in SQLite, so it survives a restart, a crash, and you going to lunch. It
advances only on evidence: a step is done when the agent has actually answered,
not when Friday sent the prompt.

Three rules, each of which is a way this goes wrong:

  One at a time. Firing five prompts at a session interleaves five half-done
  jobs, and Claude Code will happily accept all five.

  Never advance on a guess. "Sent" is not "done". A step moves on when the
  session has replied and gone quiet, which is a fact on disk.

  Stop, do not skip. A step whose agent asks a question or errors HOLDS the
  plan and tells you. Carrying on past a question means the rest of the plan
  runs on an assumption nobody made.
"""

import sqlite3
import threading
import time

from . import connectors

# States a step moves through. `held` is the one that matters: it means the
# agent wants something from you and everything after it is waiting.
PENDING, RUNNING, DONE, HELD, FAILED = ("pending", "running", "done", "held",
                                        "failed")


def _db_path():
    return connectors.CONF_DIR / "plans.db"


def _db():
    connectors.CONF_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_db_path()), timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        target TEXT NOT NULL,
        sid TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'pending',
        created REAL NOT NULL,
        updated REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        note TEXT NOT NULL DEFAULT '',
        started REAL NOT NULL DEFAULT 0,
        ended REAL NOT NULL DEFAULT 0)""")
    con.commit()
    return con


# ------------------------------------------------------------- writing ----
def create(title: str, target: str, steps: list, sid: str = "") -> int:
    """A new plan, paused. Nothing runs until you approve it."""
    steps = [s.strip() for s in steps if s and s.strip()]
    if not (title.strip() and steps):
        return 0
    now = time.time()
    con = _db()
    try:
        cur = con.execute(
            "INSERT INTO plans (title, target, sid, state, created, updated) "
            "VALUES (?,?,?,?,?,?)",
            (title.strip(), target, sid, PENDING, now, now))
        pid = cur.lastrowid
        for i, text in enumerate(steps):
            con.execute("INSERT INTO steps (plan, seq, text) VALUES (?,?,?)",
                        (pid, i, text))
        con.commit()
        return pid
    finally:
        con.close()


def set_step(step_id: int, state: str, note: str = "") -> None:
    con = _db()
    try:
        stamp = "ended" if state in (DONE, FAILED, HELD) else "started"
        con.execute(f"UPDATE steps SET state=?, note=?, {stamp}=? WHERE id=?",
                    (state, note[:400], time.time(), step_id))
        con.commit()
    finally:
        con.close()


def set_plan(plan_id: int, state: str) -> None:
    con = _db()
    try:
        con.execute("UPDATE plans SET state=?, updated=? WHERE id=?",
                    (state, time.time(), plan_id))
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------- reading ----
def get(plan_id: int) -> dict:
    con = _db()
    try:
        p = con.execute("SELECT id,title,target,sid,state,created,updated "
                        "FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not p:
            return {}
        steps = con.execute("SELECT id,seq,text,state,note FROM steps "
                            "WHERE plan=? ORDER BY seq", (plan_id,)).fetchall()
        return {"id": p[0], "title": p[1], "target": p[2], "sid": p[3],
                "state": p[4], "created": p[5], "updated": p[6],
                "steps": [{"id": s[0], "seq": s[1], "text": s[2],
                           "state": s[3], "note": s[4]} for s in steps]}
    finally:
        con.close()


def active() -> dict:
    """The plan currently in play, if any. At most one runs at a time."""
    con = _db()
    try:
        row = con.execute("SELECT id FROM plans WHERE state IN (?,?) "
                          "ORDER BY updated DESC LIMIT 1",
                          (RUNNING, HELD)).fetchone()
    finally:
        con.close()
    return get(row[0]) if row else {}


def latest() -> dict:
    con = _db()
    try:
        row = con.execute("SELECT id FROM plans ORDER BY id DESC "
                          "LIMIT 1").fetchone()
    finally:
        con.close()
    return get(row[0]) if row else {}


def next_step(plan: dict) -> dict:
    """The step to run now: the one that HELD, or the first not yet started.

    Returning only PENDING steps meant resuming a held plan silently skipped the
    very step that stopped it. The module's first rule is "stop, do not skip",
    and the resume path did exactly the thing the rule forbids: you answered the
    question and the answer went nowhere, while the plan carried on from the
    step after it."""
    for s in plan.get("steps", []):
        if s["state"] in (HELD, PENDING):
            return s
    return {}


def unfinished(plan: dict) -> list:
    """Steps that were in flight when something stopped, and are therefore of
    unknown outcome.

    A step left RUNNING by a crash is not pending and not done. Treating it as
    neither, the old code skipped it and then announced the plan finished, for a
    plan in which one step may never have run at all."""
    return [s for s in plan.get("steps", []) if s["state"] == RUNNING]


def describe(plan: dict) -> str:
    """The plan as a person would read it, with where it has got to."""
    if not plan:
        return "No plan."
    mark = {DONE: "done", RUNNING: "running now", HELD: "waiting on you",
            FAILED: "failed", PENDING: ""}
    lines = []
    for s in plan["steps"]:
        tag = mark.get(s["state"], "")
        note = f" ({s['note'][:80]})" if s["note"] and s["state"] in (HELD,
                                                                     FAILED) \
            else ""
        lines.append(f"{s['seq'] + 1}. {s['text']}"
                     + (f"  [{tag}{note}]" if tag else ""))
    done = sum(1 for s in plan["steps"] if s["state"] == DONE)
    head = (f"{plan['title']} on {plan['target']}, {done} of "
            f"{len(plan['steps'])} done")
    return head + ":\n" + "\n".join(lines)


def held_plans() -> list:
    """Plans stopped waiting for you. Something has to remember them."""
    con = _db()
    try:
        rows = con.execute("SELECT id FROM plans WHERE state=? "
                           "ORDER BY updated", (HELD,)).fetchall()
    finally:
        con.close()
    return [get(r[0]) for r in rows]


def running_plans() -> list:
    """Plans left mid-flight, which after a restart means abandoned."""
    con = _db()
    try:
        rows = con.execute("SELECT id FROM plans WHERE state=? "
                           "ORDER BY updated", (RUNNING,)).fetchall()
    finally:
        con.close()
    return [get(r[0]) for r in rows]


def sweep_on_start(announce) -> None:
    """Say what was left unfinished when Friday last stopped.

    "It survives a restart" was true of the DATA and not of the execution: a
    plan left running sat running forever, and nobody was ever told. You would
    close the tab mid-plan and it would simply cease to exist as far as you were
    concerned."""
    for plan in running_plans():
        stuck = unfinished(plan)
        set_plan(plan["id"], HELD)
        if stuck:
            which = ", ".join(str(s["seq"] + 1) for s in stuck)
            announce(f"The plan on {plan['target']} stopped while step {which} "
                     f"was in flight, so I don't know whether it finished. "
                     f"Say \"where is the plan\" to see it.")
        else:
            announce(f"The plan on {plan['target']} was interrupted. Say "
                     f"\"run the plan\" to carry on.")


class Runner:
    """Walks one plan, one step at a time, on evidence.

    It never holds the conversation: everything happens on a thread, and the
    only way it speaks is by announcing, so a plan running for an hour does not
    make Friday unresponsive for an hour."""

    POLL = 3.0
    SETTLE = 4.0              # an agent answers in stages; wait for the last one
    STEP_TIMEOUT = 900        # 15 minutes on one step, then hold and say so

    def __init__(self, announce, send, look, log=None):
        self.announce = announce      # (text, items=None)
        self.send = send              # (sid, text) -> bool
        self.look = look              # (sid) -> {"status", "question", "path"}
        self._log = log or (lambda *_: None)
        self._stop = threading.Event()
        self._thread = None

    def start(self, plan_id: int) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        set_plan(plan_id, RUNNING)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(plan_id,),
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self, plan_id: int) -> None:
        try:
            while not self._stop.is_set():
                plan = get(plan_id)
                if not plan or plan["state"] not in (RUNNING,):
                    return
                step = next_step(plan)
                if not step:
                    stuck = unfinished(plan)
                    if stuck:
                        # Something stopped while this was in flight, so nobody
                        # knows whether it ran. Saying "all done" here is the
                        # worst available answer: it is a claim about work that
                        # may not exist.
                        set_plan(plan_id, HELD)
                        which = ", ".join(str(s["seq"] + 1) for s in stuck)
                        self.announce(
                            f"Plan {plan['title']} stopped with step {which} "
                            f"already sent, so I don't know whether it "
                            f"finished. Check that one and say \"run the "
                            f"plan\" to carry on.")
                        return
                    set_plan(plan_id, DONE)
                    self.announce(f"Plan finished: {plan['title']}. All "
                                  f"{len(plan['steps'])} steps done.")
                    return
                if not self._do(plan, step):
                    return                     # held or failed; it said why
        except Exception as e:
            self._log(f"friday plan: {e}")
            set_plan(plan_id, FAILED)
            self.announce(f"The plan stopped: {e}")

    NUDGE_AFTER = 900         # 15 minutes, then remind, and keep reminding
    NUDGE_LIMIT = 4           # after an hour of silence, stop nagging

    def _nudge(self, plan_id: int, target: str, question: str) -> None:
        """Bring a held plan back up, until it is answered or you give up on it.

        A repeating reminder rather than one shot, because the whole cost of a
        held plan is that the work behind it is stopped. It stops on its own
        after an hour: a reminder that never ends is just noise with a timer."""
        def _wait():
            for _ in range(self.NUDGE_LIMIT):
                for _tick in range(int(self.NUDGE_AFTER / max(self.POLL, 0.05))):
                    if self._stop.is_set():
                        return
                    time.sleep(self.POLL)
                    plan = get(plan_id)
                    if not plan or plan["state"] != HELD:
                        return           # answered, or you stopped it
                self.announce(f"Still waiting on {target} before the plan can "
                              f"go on: {question[:120]}",
                              items=[{"sid": "", "label": target,
                                      "kind": "blocked"}])
            self.announce(f"I'll stop reminding you about the plan on {target}. "
                          f"Say \"where is the plan\" when you want it.")
        threading.Thread(target=_wait, daemon=True).start()

    def _do(self, plan: dict, step: dict) -> bool:
        """One step. True to keep going, False to stop the plan here."""
        from . import replies
        sid = plan.get("sid", "")
        info = self.look(sid) or {}
        path = info.get("path", "")
        mark = replies.mark(path) if path else ""
        set_step(step["id"], RUNNING)
        self.announce(f"Step {step['seq'] + 1} of {len(plan['steps'])}: "
                      f"{step['text']}")
        if not self.send(sid, step["text"]):
            set_step(step["id"], FAILED, "couldn't reach the session")
            set_plan(plan["id"], HELD)
            self.announce(f"I couldn't reach {plan['target']}, so the plan is "
                          f"paused at step {step['seq'] + 1}.")
            return False

        end = time.time() + self.STEP_TIMEOUT
        settled_at, last_seen = 0.0, mark
        while time.time() < end and not self._stop.is_set():
            time.sleep(self.POLL)
            live = self.look(sid) or {}
            asked = (live.get("question") or "").strip()
            if asked:
                # A question stops the plan. Running the next step would be
                # answering it by ignoring it.
                set_step(step["id"], HELD, asked)
                set_plan(plan["id"], HELD)
                self.announce(f"{plan['target']} is asking before I can go on: "
                              f"{asked}",
                              items=[{"sid": sid, "label": plan["target"],
                                      "kind": "blocked"}])
                # Say it once, then keep it alive. Announcing a hold and then
                # never mentioning it again means you go to lunch and the plan
                # quietly ceases to exist as far as you are concerned.
                self._nudge(plan["id"], plan["target"], asked)
                return False
            if path:
                said = replies.last_said(path)
                if said and said[:200] != mark:
                    # Wait for it to STOP talking, the same way the watchtower
                    # and wait_for_reply already do. Without this, an agent's
                    # first "Let me look at that" completes the step and the
                    # next one is sent on top of work still in progress.
                    if said != last_seen:
                        last_seen, settled_at = said, time.time()
                    elif (time.time() - settled_at >= self.SETTLE
                          and live.get("status") != "working"):
                        set_step(step["id"], DONE, said[:300])
                        return True
        if self._stop.is_set():
            return False
        set_step(step["id"], HELD, "no answer in fifteen minutes")
        set_plan(plan["id"], HELD)
        self.announce(f"Step {step['seq'] + 1} has been going fifteen minutes "
                      f"with no reply, so I've paused rather than piling the "
                      f"next one on top.")
        return False
