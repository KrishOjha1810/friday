# Friday

A conductor for your coding agents, and for the tools around them.

You run several Claude Code sessions at once. Each one works, asks you
something, finishes, or gets stuck, and the only way to find out which is to go
and look at its window. Meanwhile Slack, GitHub and your calendar are all
holding things you need to know. Friday is the single place all of that arrives,
in one conversation you can type into or talk to, and the place you answer from.

It is not a dashboard. A dashboard tells you there are three unread things. The
whole point here is the third part of every report: what you can **do** about it.

```
* api says: Rebase has conflicts in two files. It's asking: Should I force-push?
* voicebridge says: Fixed the Kokoro crash in vb/core.py line 812. Two tests
                    were failing and both pass now.
* U_MAN in #moonshot: are you free Thursday?
    tell me a time and I'll put it in the draft, or "ask <session> about this"
```

Nobody asked for those. Friday brought them, urgent first, and the answer you
type next goes to whichever one asked.

---

## Start here

Requires macOS, Python 3.9+, and
[voicebridge](https://github.com/cc-vb/voicebridge) installed (Friday uses it
for speech, session sensing and its local model).

```
cd ~/friday
python3 run.py
```

Open **http://127.0.0.1:8765**. That is the whole setup.

From your phone:

```
python3 run.py --phone
```

It prints a URL containing a key. Open it on your phone, same wifi or over
Tailscale. **Keep that URL private:** Friday can open windows and type into your
running agents, so anyone holding the link can drive your machine.

### First five things to try

```
what's running?
brief me
ask everyone what they are working on
what did I miss
who am I talking to
```

Then connect Slack, which is the one integration needing a setup step:

```
connect slack
```

Friday walks you through it in two moves: generate an App Configuration Token at
api.slack.com/apps (one button), paste it back, then press Allow in the tab that
opens. Friday builds the Slack app itself with the ten read scopes it needs, so
you never touch a scope list. GitHub needs nothing at all; it uses the `gh` CLI
you are already signed in to.

---

## What it does today

### Conducting agents

| Say this | What happens |
|---|---|
| `what's running?` | the fleet in plain English |
| `who needs me?` | only the ones blocked on you |
| `open <name>` | raises that window; instantly reversible, so no confirmation |
| `tell <name> to <thing>` | routes into that session |
| `ask <name> for a summary of changes` | sends it, waits, reports the answer here |
| `ask everyone what they are working on` | one question to every session at once |
| `ask it to also run the tests` | keeps talking to the same one, no renaming |
| `ask <project> to ...` | reopens a **closed** project and sends it |
| `tell me when it is done` | watches, reports when it stops |
| `stop <name>` | the same Escape you would press |
| `say more` | that session's exact words, not the summary |
| `who am I talking to` | where a bare reply would land |

`tell` means do this. `ask` means bring me the answer. Only one of them waits.

### Watching, unprompted

Friday reports without being asked, sessions waiting on you first:

- **Agents.** Every reply, summarised to a couple of sentences.
- **Slack.** A new message with who sent it, what they want, and what you can do.
- **GitHub.** Review requests, mentions, broken builds. Fifty notifications
  become two lines.
- **Your repos.** Uncommitted changes and unpushed commits, which nothing else
  watches and which is the quietest way to lose a day.
- **Calendar.** A meeting starting in the next fifteen minutes, once macOS has
  granted access.

Controls: `quiet` / `resume`, `ignore <name> for now` / `unmute <name>`, and
`what did I miss` for everything since you last spoke.

### Memory and reading

```
find the session where I set up redis
what was I working on recently?
what was said in #moonshot yesterday        (real date bounds, not "the last 15")
what was discussed in #eng on Friday
what did Sam say in #moonshot
did we ever talk about this?                  (searches your past sessions)
brief me                                      (everything, in one answer)
draft a reply                                 (writes it; you send it)
are you using claude for this?                (a real answer: a local model)
```

### What it deliberately cannot do

Stated plainly because Friday states it too, and never offers what it cannot
perform:

- Post or send anything in Slack **until you turn it on**. Say `let yourself
  post` and Friday asks Slack for `chat:write`; until then the app holds read
  scopes only. Even with it on, nothing goes out without Friday showing you the
  exact words and waiting for a yes, and it sends them verbatim rather than
  regenerating. `turn off posting` revokes it without touching reading.
- Put anything in your calendar, or schedule a meeting.
- Comment on a pull request until you turn writing on, and merge or change
  anything at all, ever. `let yourself post` covers Slack messages and GitHub
  comments, and nothing else.
- Write code, or start work by itself.
- See inside another person's sessions on this Mac. It can see that they exist.

---

## How it is built

Friday is a small Python server, one HTML page, and no build step. It leans on
voicebridge through a single seam (`friday/engine.py`) for speech, fleet sensing
and the local model, so neither project reaches into the other's internals.

```
run.py                  entry point
friday/
  server.py             one page, one conversation, live (SSE)
  conversation.py       what Friday does when you say something  <- the big one
  engine.py             the only bridge to voicebridge
  actions.py            things that touch your machine (disarmable, see below)

  watchtower.py         watches agents, reports what each one said
  inbox.py              watches Slack for messages from people
  feeds.py              watches everything else, with one set of noise rules
  replies.py            waits for an agent to answer, reads it off the transcript
  fleetcache.py         one warm reading of the fleet, shared by all callers

  connectors.py         Slack, GitHub, Gmail, Jira, and Slack self-setup
  mcp.py                Friday as an MCP client (PKCE, write-gated)
  memory.py             your past sessions and projects, searchable
  nearest.py            matching what you said to the names that exist
  when.py               "yesterday", "on Friday" -> a real range of time
static/index.html       the page
tests/                  11 suites, all runnable with plain python3
```

### The three watchers, and why they are separate

- **watchtower** reads session transcripts off disk. No hook, no attaching to a
  process, no cooperation needed from the agent.
- **inbox** polls Slack for messages from people.
- **feeds** is a dispatcher, not a watcher: sources answer one question, "what is
  new?", and everything deciding whether you hear about it lives in one place.

That last split is the important one. Adding GitHub, then git, then a calendar,
each with its own loop and its own idea of what is worth saying, is how an
assistant becomes a firehose: every source gets the rate limiting slightly wrong
in a different way, and the result is muted. So sources are dumb and the
dispatcher is smart.

### The rules the code lives by

Every one of these is here because breaking it produced a real, specific
failure. They are not style preferences.

**1. Never offer what you cannot perform.**
A source writes its own "offers" and must not include one Friday cannot carry
out. Offering to reply and then not sending is worse than not offering.

**2. Never act on a guess; ask instead.**
Names get misheard. `moonshot` arrives as "Munsheer", "moon shot",
"moon of shot". A clear match is acted on, a plausible one is asked about, and
otherwise Friday says what does exist. `friday/nearest.py` owns the thresholds so
every caller judges by the same standard. A pronoun (`stop it`) resolves from
context and **never** by sound: "it" scores 0.8 against "api", and stopping the
wrong agent is not a small mistake.

**3. Say each thing once, and admit what you held back.**
Polling sees the same message repeatedly. A silent cap reads as "nothing
happened", so anything not read out is counted and mentioned.

**4. Wait for an agent to stop talking.**
Answers arrive in stages. Reporting the first one reports "Let me look at that".

**5. A summary is checked before you see it.**
Asked to compress "the parser broke on page 3 of the PDF", the local model
produced "retry with the file named report_2024_q3.pdf, error code
PDF_PARSE_003". Both invented, both exactly the kind of detail you would act on.
Any path, code or number in a summary that is not in the source throws the whole
summary away in favour of the agent's own words. Brevity is worth less than being
true.

**6. Failing is not the same as empty.**
"Nothing is running" and "I could not read the fleet" are different answers, and
only one of them is safe to act on. Same for Slack refusing versus a quiet
channel, and for an empty calendar versus never having been granted access.

**7. Degrade out loud.**
A subsystem that is present but failing is the case nobody writes code for and
the one that happens. An exception reaching the request handler is a 500, which
tells you nothing at all.

**8. Nothing blocks on a subprocess.**
Reading the fleet costs 3.2 seconds of CLI startup. Two callers each doing it per
poll made `/state` take eight seconds and the status strip permanently stale. One
warm reading is shared and refreshed behind you: now 0.3ms.

### Testing

```
for t in tests/test_*.py; do PYTHONPATH=. python3 "$t"; done
```

Eleven suites, plain Python, no pytest needed, each printing one line.

Two tests of mine escaped into the real machine, and both shaped how the suite
works now. One wrote a fake token over a live Slack credential and deleted it in
cleanup, which looked like Slack disconnecting at random for hours. One typed
prompts into a terminal somebody was working in, because a session id that
matches nothing does not fail safe: it falls through to the default adapter and
types into whatever window is in front of you.

So `tests/sandbox.py` is imported first by every suite. It redirects the config
directory and your project history to a throwaway path, and **disarms
`actions.py`**, which then records what it would have done instead of doing it.
`tests/test_no_escape.py` asserts the guards are on. Being careful is not a
mechanism.

---

## Where this is going

The full plan lives in `~/voicebridge/docs/friday-master-plan.md`. The short
version, honestly assessed.

### Done

Phase 1 (supervisor mode) and the 1.5 split, which is why Friday is its own app
rather than a branch of voicebridge. Orchestration went further than the plan
asked: ask-and-answer, broadcast, sticky targets, watch, stop, reopening closed
sessions.

### Next, in order

1. **Web push.** The fleet board is useless when your phone is locked, which is
   most of the time. This is the biggest single gap between Friday and its pitch.
2. **Write actions, behind confirmation.** Reply in Slack, comment on a PR, move a
   ticket. This is the line between reporting and conducting, and it is a
   decision about risk rather than a coding problem.
3. **Gmail and Jira actually connected.** Both connectors exist and neither is
   authorized. Slack needed its own app because the hosted MCP has no dynamic
   client registration; Atlassian and Google will have the same shape, so the
   app-building trick generalizes.
4. **Phase 2, the conductor core.** Friday reads your tickets, has an opinion
   ("start with the login bug, it is high priority and small"), you pick, an agent
   plans, Friday speaks the plan, you approve by voice, it runs the steps with a
   checkpoint at each one. Needs a plan runner, a small SQLite state machine. This
   is where Friday stops being a very good intercom.
5. **Phase 3, the signature trick.** A plan step that holds until a human replies:
   Friday asks a colleague in Slack, the step releases when they answer. A mailbox
   on the plan runner plus one integration, not new science.
6. **Phase 4.** Team edition, more agent vendors, morning briefings, native app.

### The one thing that gates all of it

Phase 2 does not start until a kill test passes: 20 to 30 developers running 3+
agents daily, two weeks, nothing new built during it, and **at least 40% still
have proactive alerts on at the end**. If they mute it, the conductor thesis is
wrong and Phase 2 would be built on a false premise. The sample size today is
one.

---

## Known gaps

Kept honest rather than tidy.

- The fleet strip is glanceable but not an action surface: no peek panel to read
  a question and answer it inline.
- Asks have no lifecycle: they do not collapse into a one-line resolution, and
  several at once would stack rather than queue.
- No suggested replies on an ask (the design calls for two to four tappable).
- No target chip in the composer, so routing is inferred rather than visible.
- No presence gating: Friday does not know whether you are at the page, at the
  machine, or away, so everything is routed the same way.
- The phone uses the same page with no phone-specific layout.
- Calendar access is not granted on this machine. Friday says so rather than
  implying your day is empty.

---

## Contributing

Good first places, roughly by size:

- **`friday/feeds.py`** — add a source. Implement `poll()` returning items, and
  optionally `state()` for `brief me`. The dispatcher handles everything else.
  Linear, Sentry and PagerDuty are all obvious candidates.
- **`friday/when.py`** — more time phrases. Self-contained and fully testable.
- **`friday/nearest.py`** — matching. Small, pure, and the thresholds are load
  bearing, so any change wants a test.
- **`static/index.html`** — the known gaps above are nearly all here.
- **`friday/conversation.py`** — the big one. New commands are a regex, a
  dispatch line, and a handler. Read `_abilities()` first: the model is told what
  Friday can do, and a stale list is how it starts claiming it cannot read Slack
  while Slack is connected.

Two conventions worth keeping. Comments explain **why**, usually by naming the
failure that produced the code, because that is the thing a reader cannot
reconstruct. And every commit message says what broke and what it now does
instead, in prose, for the same reason.

License: not yet decided. Treat it as all rights reserved for now.
