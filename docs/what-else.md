# What else Friday should do

Written before building, so the choices are visible and can be argued with.

The test for every idea below is the same one: **does it remove a place a
developer currently has to go?** Friday's whole claim is to be the one place.
Anything that adds a surface rather than absorbing one does not belong here,
however impressive it is.

---

## The gap, stated plainly

Friday reads well and acts barely. It can tell you a colleague wants a meeting
on Thursday and cannot put it in your calendar. It can tell you a ticket exists
and cannot move it. It conducts Claude Code and nothing else, on a machine that
has Codex installed with sessions sitting on disk.

So the work splits three ways: **more agents**, **more verbs**, **more places**.

---

## 1. More agents (the biggest one)

Friday conducts Claude Code. The plan always said vendor-neutral, and the risk
register says the defence against Anthropic shipping this themselves is
supporting the competition.

Codex is on this machine and writes `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
with `session_meta` (id, cwd), `response_item` (role, content) and `event_msg`
(task started/ended). Everything Friday needs to sense a session is there:
what it is, what it last said, whether it is working.

**Decision: build a vendor seam and put Claude and Codex behind it.** Sensing is
the hard half and it is done per-vendor; conducting reuses the same terminal
targeting that already exists.

What this buys beyond Codex: the seam is the thing. Cursor, Gemini, Amp,
opencode and whatever ships next all keep transcripts somewhere, and each
becomes a file-format problem rather than a redesign.

**Antigravity is the third, and it proved the seam twice over.** Once the
easy way: it stores conversations as protobuf in SQLite, which is unreadable
without the schema, but it also writes `implementation_plan.md`, `task.md` and
`walkthrough.md` per conversation, deliberately, for a human to read. That is a
BETTER surface than either of the other two, not a worse one, because a
checklist says how far through it is and a transcript only says what it said.

Once the hard way: Antigravity is an IDE, so it can be read and not typed into,
which breaks the half of the seam that assumed conducting is the same
everywhere. Rather than paper over it, `can_conduct()` is now part of the seam
and Friday says plainly that it cannot reach that one. A confident "sent" for a
message that went nowhere is the worst failure available here.

## 2. More verbs

Reading is solved. Acting is one Slack message and one GitHub comment, both
behind a switch. The honest list of what a developer actually does in a day:

- **Tickets**: read, create, comment, move. Friday has a Jira connector that has
  never been authorised and cannot create anything. This is the front half of
  Phase 2: Friday reads your tickets, has an opinion about which to start with,
  and can open one when a Slack thread turns out to be work.
- **Calendar**: create an event. You have asked for this twice, in the same
  sentence both times, and it is the one that makes "Sam wants a meeting
  Thursday" finishable rather than merely reportable.
- **GitHub**: comment exists. Approving a review and merging are the two that
  matter and both are genuinely dangerous, so they stay behind the same
  show-me-first confirmation as everything else.

## 3. More places

Ranked by how often a developer has to go and look:

1. **Linear**, which for many teams replaced Jira entirely.
2. **Sentry** or whatever error tracker: production is on fire is the one alert
   that outranks everything Friday currently watches. **Built.** The connector
   was the easy half; the restraint is the feature. It volunteers only issues it
   has never seen, unhandled, that happened more than once, and says nothing at
   all on first connect. Asked directly it reports everything, because a
   month-old error is noise when volunteered and exactly what you wanted when
   requested.
3. **Calendar**, already read, needs writing.
4. **PagerDuty**, if you are on call, though for one person this is Sentry with
   a phone number.

Deliberately refused: anything that is a feed rather than a request. Twitter,
Hacker News, RSS. They are not places you *have* to go, and a thing that shows
them becomes a thing you check, which is the opposite of the product.

## 4. The app

"Open a browser tab and keep it open" is not a product a person adopts. Friday
already has the two hard parts of an installable app: a service worker and Web
Push. What it lacks is a manifest, icons, and an offline shell, which is the
difference between a page and something with an icon on your home screen and
your dock.

**Decision: a PWA, not a native app.** Native buys a marginally better icon and
costs a build pipeline, a signing identity, a review process and a second
codebase. The push notifications, which were the real reason to want native,
already work.

---

## What this does NOT include, and why

- **Writing code.** Friday conducts agents that write code. The moment it starts
  writing code it is competing with the thing it conducts.
- **Its own model.** The local model summarises and classifies. Making it answer
  technical questions puts a 4B model in front of a frontier one, which is worse
  than the thing you already have open.
- **A team edition.** In the plan, and correctly Phase 4. One user is not yet
  convinced.
- **Auto-approving anything.** Every irreversible verb keeps its confirmation.
  The moment Friday sends something you did not read, you stop trusting all of
  it, and that trust is the entire product.
