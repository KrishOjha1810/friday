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

import re
import sqlite3
import threading
import time

from . import connectors

# ------------------------------------------------- turning prose into steps ----
# What an agent's answer looks like when you ask it for a plan: some preamble,
# a numbered or bulleted list, and then caveats. Only the list is the plan.
_NUMBERED = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*\u2022])\s+(.+)$")
_HEADING = re.compile(r"^\s*#{1,6}\s|^\s*\*\*[^*]+\*\*\s*:?\s*$")
# Markdown decoration, which is for the eye and not for a terminal prompt.
_DECOR = re.compile(r"[`*_]+")
MAX_STEPS = 10
MIN_STEP_CHARS = 12


# An agent's answer does not reach here with its line breaks intact: the
# transcript readers normalise whitespace, so a numbered plan arrives as one
# long line. Finding the enumerators inside it needs a stronger signal than
# "there is a number here", because "1." also appears in prose and in version
# numbers. The signal used is that the numbers run 1, 2, 3 in order.
_INLINE = re.compile(r"(?:(?<=\s)|^)(\d{1,2})[.)]\s+")


def _unwrap(text: str) -> str:
    """Put the line breaks back, when a list has been flattened into a line."""
    if len([l for l in text.splitlines() if _NUMBERED.match(l)]) >= 2:
        return text                     # the breaks are still there
    found = list(_INLINE.finditer(text))
    runs, current = [], []
    for m in found:
        n = int(m.group(1))
        if n == (int(current[-1].group(1)) + 1 if current else 1):
            current.append(m)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [m] if n == 1 else []
    if len(current) >= 2:
        runs.append(current)
    if not runs:
        return text
    # The longest ascending run is the list; anything else is prose that
    # happened to contain a number.
    best = max(runs, key=len)
    out, at = [], 0
    for m in best:
        out.append(text[at:m.start()])
        at = m.start()
        out.append("\n")
    out.append(text[at:])
    return "".join(out)


def steps_from_answer(text: str) -> list:
    """The steps an agent proposed, and nothing else it said.

    Deliberately conservative, and the reason is asymmetric cost. A missed step
    means you type one line. An invented step means an agent is told to do
    something nobody asked for, and it will do it. So this only ever takes lines
    that are explicitly enumerated, never sentences it thinks look actionable.

    It also refuses rather than guessing: an answer with no list at all returns
    nothing, and the caller shows you what the agent actually said instead of
    manufacturing a plan out of prose.
    """
    if not text:
        return []
    text = _unwrap(text)
    out, seen = [], set()
    fenced = False
    for raw in text.splitlines():
        # Code blocks are the answer's example, not its plan, and a plan made of
        # half a diff sent back as a prompt is worse than no plan.
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or _HEADING.match(raw):
            continue
        m = _NUMBERED.match(raw)
        if not m:
            continue
        step = _DECOR.sub("", m.group(1)).strip().rstrip(":")
        # A question in the list is the agent asking you something, not a step
        # it can carry out. Sending it back as an instruction is a loop.
        if step.endswith("?"):
            continue
        if len(step) < MIN_STEP_CHARS:
            continue
        key = step.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(step[:280])
        if len(out) >= MAX_STEPS:
            break
    if out:
        out[-1] = _trim_tail(out[-1])
    return out


# What an answer says after the list: "Caveats: ...", "Note: ...", "Let me know
# if ...". Once the line breaks are gone, that tail is glued to the final step,
# and the final step is the one thing here that gets sent to an agent verbatim.
_TAIL = re.compile(r"\s+(?=(?:[A-Z][a-z]+:)|(?:Let me know|Once you|Note that))"
                   r"|(?<=[.!?])\s+(?=[A-Z])")


def _trim_tail(step: str) -> str:
    """Cut the trailing prose off the last step.

    Only the last one can pick it up, and only when the answer arrived
    flattened. Kept narrow on purpose: a step cut short is something you notice
    in the approval list and fix, a step with a paragraph of caveats glued on is
    something an agent tries to carry out."""
    cut = _TAIL.split(step, 1)[0].strip()
    return cut if len(cut) >= MIN_STEP_CHARS else step


ASK_FOR_PLAN = (
    "Before you do anything: give me a short numbered plan for this, and do "
    "not start yet. One line per step, at most {n} steps, each a concrete "
    "action. No preamble, no code. The task is: {task}")


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
    # Added after the fact, so existing plans on disk keep working. A step
    # carries its own target now: the plan-level one is the default for steps
    # that do not name somebody.
    for col, decl in (("target", "TEXT NOT NULL DEFAULT ''"),
                      ("sid", "TEXT NOT NULL DEFAULT ''"),
                      ("kind", "TEXT NOT NULL DEFAULT 'agent'")):
        try:
            con.execute(f"ALTER TABLE steps ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass          # already there
    con.commit()
    return con


# ------------------------------------------------------------- writing ----
def create(title: str, target: str, steps: list, sid: str = "") -> int:
    """A new plan, paused. Nothing runs until you approve it.

    A step is a string, or a dict with `text` and optionally `target`, `sid` and
    `kind`. Strings inherit the plan's target, which is what every plan written
    before this did, so nothing on disk changes meaning."""
    rows = []
    for st in steps:
        if isinstance(st, dict):
            text = (st.get("text") or "").strip()
            if not text:
                continue
            rows.append({"text": text,
                         "target": (st.get("target") or target or "").strip(),
                         # A step naming the SAME target as the plan is the
                         # same session, so it inherits the sid. Blanking it
                         # made two steps for one terminal look like two
                         # separate agents to the queue.
                         "sid": st.get("sid") or (
                             sid if not st.get("target")
                             or (st.get("target") or "").strip().lower()
                             == (target or "").strip().lower() else ""),
                         "kind": st.get("kind") or "agent"})
        elif str(st).strip():
            rows.append({"text": str(st).strip(), "target": target,
                         "sid": sid, "kind": "agent"})
    steps = rows
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
        for i, st in enumerate(steps):
            con.execute("INSERT INTO steps (plan, seq, text, target, sid, kind)"
                        " VALUES (?,?,?,?,?,?)",
                        (pid, i, st["text"], st["target"], st["sid"],
                         st["kind"]))
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
        steps = con.execute(
            "SELECT id,seq,text,state,note,target,sid,kind FROM steps "
            "WHERE plan=? ORDER BY seq", (plan_id,)).fetchall()
        return {"id": p[0], "title": p[1], "target": p[2], "sid": p[3],
                "state": p[4], "created": p[5], "updated": p[6],
                "steps": [{"id": s[0], "seq": s[1], "text": s[2],
                           "state": s[3], "note": s[4],
                           "target": s[5] or p[2], "sid": s[6] or p[3],
                           "kind": s[7] or "agent"} for s in steps]}
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


def track_of(step: dict) -> str:
    """Which queue a step belongs to.

    The SESSION id when there is one, and only the name for a person. It was
    the name, which meant "one at a time per agent" was enforced against a
    display string a human typed: two steps saying "api" and "API server" for
    the same terminal counted as two independent agents and both fired at once.
    The guarantee has to be about the thing being typed into."""
    sid = (step.get("sid") or "").strip()
    if sid:
        return f"sid:{sid}"
    return "who:" + ((step.get("target") or "").strip().lower() or "_")


def runnable(plan: dict) -> list:
    """Every step that could start right now, one per track.

    This is the mechanic the whole product was pitched on: do this now,
    meanwhile ask that, hold the rest until a reply comes back. Before this a
    plan had a single target and ran one step at a time against it, which is a
    very good intercom with a queue.

    Two rules, and they are the same two as before, applied per track rather
    than globally:

      One at a time PER AGENT. Firing five prompts at one session interleaves
      five half-done jobs and Claude Code will accept all five. Firing one
      prompt at each of five sessions is just five agents working, which is
      the entire point of having five.

      Stop, do not skip. A held step blocks ITS track and nothing else. Holding
      the whole plan because one agent asked a question would mean a question
      about the docs stops the migration, and the person who has to answer it is
      the one waiting on the migration.
    """
    out, blocked = [], set()
    for st in plan.get("steps", []):
        track = track_of(st)
        if track in blocked:
            continue
        if st["state"] in (RUNNING, HELD, FAILED):
            # Whatever this track is doing, it is not free for the next thing.
            # HELD included: the answer you give belongs to this step, and
            # running the one after it first would apply your answer to the
            # wrong work.
            blocked.add(track)
            if st["state"] == HELD:
                out.append(st)      # resumable, and the reason the track stopped
            continue
        if st["state"] == PENDING:
            out.append(st)
            blocked.add(track)
    return out


def tracks(plan: dict) -> list:
    """The distinct targets in a plan, in the order they first appear."""
    seen = []
    for st in plan.get("steps", []):
        t = st.get("target") or plan.get("target", "")
        if t and t not in seen:
            seen.append(t)
    return seen


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


def _release(plan_id: int) -> list:
    """Put steps left in flight back where they can run.

    A step interrupted by a stop, a crash or a closed laptop stayed RUNNING,
    and nothing anywhere reset it. `runnable` treats RUNNING as blocking its
    track, so the plan was wedged for good while the interface went on offering
    "run the plan to carry on", which did nothing at all, forever.

    Returned to PENDING rather than DONE, and the note says so, because whether
    it actually finished is exactly what nobody knows. Re-sending a step you may
    have already sent is recoverable and visible; skipping one silently is
    neither."""
    plan = get(plan_id)
    stuck = [s for s in (plan or {}).get("steps", []) if s["state"] == RUNNING]
    for st in stuck:
        set_step(st["id"], PENDING,
                 "was in flight when the plan stopped, so this may have "
                 "already run")
    return stuck


def sweep_on_start(announce) -> None:
    """Say what was left unfinished when Friday last stopped.

    "It survives a restart" was true of the DATA and not of the execution: a
    plan left running sat running forever, and nobody was ever told. You would
    close the tab mid-plan and it would simply cease to exist as far as you were
    concerned."""
    for plan in running_plans():
        stuck = unfinished(plan)
        _release(plan["id"])
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

    def __init__(self, announce, send, look, log=None, tell_person=None):
        # Guards start(), which is otherwise read-then-act on a threaded server.
        self._gate = threading.Lock()
        self.announce = announce      # (text, items=None)
        self.send = send              # (sid, text) -> bool
        self.look = look              # (sid) -> {"status", "question", "path"}
        # (who, text) -> bool. Absent means Friday says so and waits for you,
        # rather than dropping the step.
        self.tell_person = tell_person
        self._log = log or (lambda *_: None)
        self._stop = threading.Event()
        self._thread = None

    def start(self, plan_id: int) -> bool:
        """One runner loop, ever.

        The check was read-then-act with no lock, and the server is threaded, so
        saying "run the plan" twice quickly produced TWO loops with separate
        in-flight bookkeeping. Neither could see the other's steps, so every
        step was sent twice into the same session. That is the one rule this
        module exists to enforce, broken by a double-tap."""
        with self._gate:
            if self._thread and self._thread.is_alive():
                return False
            set_plan(plan_id, RUNNING)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(plan_id,),
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self, plan_id: int = 0) -> None:
        """Stop the loop, and leave the stored state telling the truth.

        It used to set the event and nothing else, so the plan went on claiming
        RUNNING while nothing ran, and the step in flight stayed RUNNING
        forever. Nothing anywhere reset it, so the plan was wedged: `runnable`
        treats RUNNING as blocking its track, and "run the plan to carry on"
        did nothing, permanently."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        for plan in ([get(plan_id)] if plan_id else running_plans()):
            if not plan:
                continue
            _release(plan["id"])
            set_plan(plan["id"], HELD)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self, plan_id: int) -> None:
        """Keep every free track moving until nothing can move.

        One thread per running step rather than one thread per plan. The plan
        is finished when no step can start and none is in flight; it is held
        when the only thing stopping it is somebody who has not answered."""
        live = {}                       # step id -> thread
        try:
            while not self._stop.is_set():
                plan = get(plan_id)
                if not plan or plan["state"] != RUNNING:
                    break
                for st in runnable(plan):
                    if st["state"] != PENDING or st["id"] in live:
                        continue
                    t = threading.Thread(
                        target=self._one, args=(plan_id, st["id"]), daemon=True)
                    live[st["id"]] = t
                    t.start()
                    if len(live) >= self.MAX_AT_ONCE:
                        break
                for sid_, t in list(live.items()):
                    if not t.is_alive():
                        live.pop(sid_, None)
                if not live:
                    plan = get(plan_id)
                    if not plan or plan["state"] != RUNNING:
                        break
                    if not [st for st in runnable(plan)
                            if st["state"] == PENDING]:
                        self._finish(plan)
                        break
                self._stop.wait(self.POLL)
        except Exception as e:
            self._log(f"friday plan: {e}")
            set_plan(plan_id, FAILED)
            self.announce(f"The plan stopped: {e}")

    MAX_AT_ONCE = 6           # more agents than anybody is actually running

    def _one(self, plan_id: int, step_id: int) -> None:
        """One step, in its own thread, so a slow track blocks only itself."""
        try:
            plan = get(plan_id)
            step = next((s for s in plan.get("steps", [])
                         if s["id"] == step_id), None)
            if plan and step:
                self._do(plan, step)
        except Exception as e:
            self._log(f"friday plan step: {e}")
            set_step(step_id, FAILED, str(e)[:120])

    def _finish(self, plan: dict) -> None:
        stuck = unfinished(plan)
        held = [s for s in plan.get("steps", []) if s["state"] == HELD]
        failed = [s for s in plan.get("steps", []) if s["state"] == FAILED]
        if stuck:
            # Something stopped while this was in flight, so nobody knows
            # whether it ran. Saying "all done" here is the worst available
            # answer: it is a claim about work that may not exist. The step goes
            # back to pending so that saying "run the plan" can actually retry
            # it, which it could not before.
            _release(plan["id"])
            set_plan(plan["id"], HELD)
            which = ", ".join(str(s["seq"] + 1) for s in stuck)
            self.announce(
                f"Plan {plan['title']} stopped with step {which} already sent, "
                f"so I don't know whether it finished. Check that one and say "
                f"\"run the plan\" to carry on.")
            return
        if failed:
            # A failed step used to be counted as neither done nor waiting, so
            # a plan with one unreachable agent announced "All 2 steps done".
            # Claiming work happened that did not is the worst thing this file
            # can do, and it is worse than crashing, because you stop looking.
            set_plan(plan["id"], HELD)
            which = ", ".join(f"{s.get('target') or 'a session'} "
                              f"(step {s['seq'] + 1})" for s in failed)
            done = len([s for s in plan.get("steps", []) if s["state"] == DONE])
            rest = (f" The other {done} finished." if done else "")
            self.announce(f"Plan {plan['title']} could not finish: {which} "
                          f"didn't run.{rest} Say \"run the plan\" to retry "
                          f"those.")
            return
        if held:
            set_plan(plan["id"], HELD)
            who = ", ".join(sorted({s.get("target") or "a session"
                                    for s in held}))
            self.announce(f"Everything I could do on {plan['title']} is done. "
                          f"The rest is waiting on {who}.")
            return
        set_plan(plan["id"], DONE)
        self.announce(f"Plan finished: {plan['title']}. All "
                      f"{len(plan['steps'])} steps done.")

    NUDGE_AFTER = 900         # 15 minutes, then remind, and keep reminding
    NUDGE_LIMIT = 4           # after an hour of silence, stop nagging

    def _nudge(self, plan_id: int, target: str, question: str,
               step_id: int = 0) -> None:
        """Bring a held STEP back up, until it is answered or you give up.

        Watches the step, not the plan. It watched the plan, and with more than
        one track the plan is usually still RUNNING when a step holds, because
        the other tracks are fine. So the reminder loop exited on its first tick
        and the question was mentioned exactly once; by the time the other
        tracks finished and the plan did go HELD, no reminder thread existed.
        Multi-agent plans are precisely the ones where you are least likely to
        notice one line scrolling past.

        A repeating reminder rather than one shot, because the whole cost of a
        held step is that the work behind it is stopped. It stops on its own
        after an hour: a reminder that never ends is just noise with a timer."""
        def _still_held():
            plan = get(plan_id)
            if not plan:
                return False
            if step_id:
                st = next((x for x in plan.get("steps", [])
                           if x["id"] == step_id), None)
                return bool(st and st["state"] == HELD)
            return plan["state"] == HELD

        def _wait():
            for _ in range(self.NUDGE_LIMIT):
                for _tick in range(int(self.NUDGE_AFTER / max(self.POLL, 0.05))):
                    if self._stop.is_set():
                        return
                    time.sleep(self.POLL)
                    if not _still_held():
                        return           # answered, or you stopped it
                self.announce(f"Still waiting on {target} before the plan can "
                              f"go on: {question[:120]}",
                              items=[{"sid": "", "label": target,
                                      "kind": "blocked"}])
            if _still_held():
                self.announce(f"I'll stop reminding you about {target}. Say "
                              f"\"where is the plan\" when you want it.")
        threading.Thread(target=_wait, daemon=True).start()

    def _do(self, plan: dict, step: dict) -> bool:
        """One step, on its own track. True if the track may carry on.

        Everything here is per step rather than per plan now. A step names the
        agent it is for, so five of these run at once against five sessions,
        and one of them stopping stops its own queue and nothing else."""
        from . import agents
        if step.get("kind") == "person":
            return self._ask_person(plan, step)
        sid = step.get("sid") or plan.get("sid", "")
        target = step.get("target") or plan.get("target", "")
        info = self.look(sid) or {}
        info.setdefault("sid", sid)
        path = info.get("path", "")
        # Counted, and read through the vendor seam. Both matter. It used to
        # call the Claude transcript parser directly, so a Codex or any other
        # agent replied and the plan saw nothing, timed out after fifteen
        # minutes and blamed the agent for silence. And it compared TEXT, so an
        # agent answering "Done." to two steps in a row was invisible the second
        # time.
        before = agents.tally(info)
        before_text = agents.last_said(info)
        # A question the session was ALREADY sitting on is not an answer to a
        # prompt that has not been sent yet. Taken as one, the first poll after
        # the send held the step instantly, with the prompt already delivered
        # and nobody watching the agent work on it.
        was_asking = (info.get("question") or "").strip()
        set_step(step["id"], RUNNING)
        # Named, because with several running at once "step 3" no longer tells
        # you who is doing it.
        self.announce(f"{target}, step {step['seq'] + 1} of "
                      f"{len(plan['steps'])}: {step['text']}")
        if not self.send(sid, step["text"]):
            set_step(step["id"], FAILED, "couldn't reach the session")
            self._maybe_hold(plan["id"])
            self.announce(f"I couldn't reach {target}, so its part of the plan "
                          f"is paused at step {step['seq'] + 1}. The rest "
                          f"carries on.")
            return False

        end = time.time() + self.STEP_TIMEOUT
        settled_at, last_seen = 0.0, ""
        while time.time() < end and not self._stop.is_set():
            time.sleep(self.POLL)
            live = self.look(sid) or {}
            live.setdefault("sid", sid)
            asked = (live.get("question") or "").strip()
            if asked and asked == was_asking:
                asked = ""          # the same one it was already sitting on
            if asked:
                # A question stops the plan. Running the next step would be
                # answering it by ignoring it.
                set_step(step["id"], HELD, asked)
                self._maybe_hold(plan["id"])
                self.announce(f"{target} is asking before it can go on: "
                              f"{asked}",
                              items=[{"sid": sid, "label": target,
                                      "kind": "blocked"}])
                # Say it once, then keep it alive. Announcing a hold and then
                # never mentioning it again means you go to lunch and the plan
                # quietly ceases to exist as far as you are concerned.
                self._nudge(plan["id"], target, asked, step_id=step["id"])
                return False
            if path:
                said = agents.last_said(live)
                # CHANGED, not merely greater. Transcripts get rotated and
                # truncated, and a count that goes DOWN is still the file
                # changing under us; requiring an increase meant a rotation
                # mid-step waited the full fifteen minutes and then blamed the
                # agent for silence. The text is the second signal, for a file
                # rewritten to the same length.
                if agents.tally(live) != before or (said and said != before_text):
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
        self._maybe_hold(plan["id"])
        self.announce(f"{target} has been on step {step['seq'] + 1} for fifteen "
                      f"minutes with no reply, so I've paused that one rather "
                      f"than piling the next on top.")
        return False

    def _ask_person(self, plan: dict, step: dict) -> bool:
        """A step aimed at a human being.

        The pitch said "hold the rest until a human reply comes back", and a
        person is just another track that can block. What makes this different
        from an agent is honesty about detection: Friday cannot reliably tell
        which Slack message is an answer to which question, so it does not
        pretend to. It sends, holds, and says plainly what it is waiting for.
        The hold is released by that person messaging you, which the inbox
        already notices, or by you saying so."""
        who = step.get("target") or "somebody"
        set_step(step["id"], RUNNING)
        sent, why = False, ""
        if self.tell_person:
            try:
                got = self.tell_person(who, step["text"])
                if isinstance(got, tuple):
                    sent, why = got
                else:
                    sent = bool(got)
            except Exception:
                sent = False
        if not sent:
            set_step(step["id"], HELD, f"ask {who}: {step['text']}")
            self._maybe_hold(plan["id"])
            self.announce(
                (f"I can't message {who} myself ({why}), so that part of the "
                 f"plan is waiting on you: {step['text']}") if why else
                (f"I can't message {who} myself, so that part of the plan is "
                 f"waiting on you: {step['text']}"),
                items=[{"sid": "", "label": who, "kind": "blocked"}])
            return False
        set_step(step["id"], HELD, f"waiting on {who}")
        self._maybe_hold(plan["id"])
        self.announce(f"Asked {who}: {step['text']}. That part of the plan "
                      f"waits for their reply; the rest carries on.",
                      items=[{"sid": "", "label": who, "kind": "blocked"}])
        return False

    def _maybe_hold(self, plan_id: int) -> None:
        """Hold the PLAN only when every track is stuck.

        One agent's question used to stop everything, which meant a question
        about the docs stopped the migration, and the person who had to answer
        it was the one waiting on the migration."""
        plan = get(plan_id)
        if not plan:
            return
        alive = [st for st in plan.get("steps", []) if st["state"] == PENDING]
        free = [st for st in runnable(plan) if st["state"] == PENDING]
        if not free and not any(st["state"] == RUNNING
                                for st in plan.get("steps", [])):
            set_plan(plan_id, HELD)
        elif alive:
            set_plan(plan_id, RUNNING)
