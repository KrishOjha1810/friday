# Start here

Five minutes, and the honest version of what works.

```
cd ~/friday
python3 run.py --check      # anything missing, and what it costs you
python3 run.py
```

Open **http://127.0.0.1:8765**.

Type, or hold the mic. Transcription is the local whisper, so nothing leaves the
machine. Tap the speaker to have Friday read replies aloud, or the call button
for a continuous hands-free conversation; anything said in a call also lands in
the chat, so the thread is always the whole record.

### In the menu bar

```
python3 run.py --app
```

An **F** in the menu bar, with the number of agents waiting on you. Click for
the whole thing in a panel. This is the one to leave running.

### From your phone

```
python3 run.py --phone
```

It prints a URL with a key in it. Open it on your phone, same wifi or over
Tailscale. **Keep it private:** Friday can open windows and type into your
running agents, so anyone with that link can drive your machine.

---

## Ten minutes of things to try

### What is going on

```
help                           the short list, works before anything is set up
what should I work on?         one thing to start with, and why
what are my tickets?           across Jira, Linear, GitHub and GitLab at once
use linear for tickets         say once where new ones go
work out a plan for <goal>     the agent drafts it, you approve it
what's on fire?                unresolved errors in production
what's running?
who needs me?
brief me                       everything: agents, Slack, GitHub, repos, calendar
what did I miss                everything since you last spoke
is anyone stuck                who cannot continue without you, and for how long
```

### Talking to one agent

```
open jobhunt                       raises that window
tell jobhunt to run the tests      routes into it
ask jobhunt for a summary of changes    sends it, waits, reports the answer here
ask it to also run the linter      same session, no need to name it again
say more                           its exact words instead of the summary
who am I talking to                where a bare reply would land
tell me when it is done            watches it, tells you when it stops
stop it
```

`tell` means do this. `ask` means bring me the answer.

### Tickets and time

```
file a ticket: the PDF parser dies on page 3
move PROJ-12 to done
put it in for Thursday at 4
```

These go to Jira or Linear if you have connected one, and to GitHub issues in
the repo you are standing in if you have not, which needs no setup because `gh`
is already signed in. Each one reads it back and waits for a yes. Filing and moving need `let
yourself post`; the calendar does not, since your own diary is not the same risk
as writing under your name where colleagues read it.

### Talking to all of them

```
ask everyone what they are working on
```

One question to every running session. Answers arrive as they land, not after
the slowest one.

### Reopening old work

```
ask promptguard to look at my resume
```

If a project is closed, Friday offers to reopen it and send that, then waits for
the window to actually exist before typing. A running session always wins over
an old copy of the same work.

```
find the session where I set up redis
what was I working on recently?
open that one
```

### Noise control

```
quiet                    stops everything unprompted
resume
ignore jobhunt for now   silences one session, keeps the rest
unmute jobhunt
what did I miss          everything held back while you were quiet or busy
```

A muted session is still watched, so unmuting does not recite a backlog. And
quiet genuinely holds rather than deletes: anything that arrives while Friday is
silent is kept, and `what did I miss` hands it over once.

Friday also stays quiet about the session whose window you are actually looking
at, since it already said it on your screen.

---

## What it watches on its own

You do not ask for these. Friday brings them, whoever is waiting on you first,
capped so it never floods, and each thing said once.

- **Your agents.** Every reply, summarised, with the question if it is asking
  one. Claude Code and Codex both, in one fleet.
- **Slack.** A new message with who sent it, what they want, and what you can do.
- **GitHub.** Review requests, mentions, broken builds. Already connected, it
  uses your `gh` login.
- **Your repos.** Uncommitted changes and unpushed commits. Nothing else watches
  these, and they are the quietest way to lose a day's work.
- **Calendar.** A meeting starting in the next fifteen minutes.

---

## Connecting things

```
what's connected?
```

**GitHub** needs nothing. It uses the `gh` CLI you are already signed in to, so
no new token, and exactly your own permissions.

**Slack** is two moves:

```
connect slack
```

1. Open api.slack.com/apps and press **Generate Token** at the top, under App
   Configuration Tokens. Copy it.
2. Paste it to Friday on its own line, then press **Allow** in the tab that
   opens.

Friday builds the Slack app itself with the ten read scopes it needs, so you
never touch a scope list. Type the token rather than dictating it: a token cannot
survive being spoken. It is stored owner-only in `~/.friday/`, and Friday refuses
to use it if the file is readable by anyone else.

Every scope Friday asks for is a read. `chat:write` is not among them unless you
say `let yourself post`, so by default Friday physically cannot post as you.

**Calendar**: say `connect calendar` and allow it when macOS asks. It reads
through EventKit, so it covers whichever accounts are already in Calendar, and
if the package is missing it tells you the one line to run.

**Linear**: paste a personal API key from linear.app/settings/api on its own
line. It starts with `lin_api_`, and Friday works out what it is.

**Gmail**: say `connect gmail`. Google needs an OAuth client created once, which
Friday cannot do for you, so it gives you the four exact steps; paste the client
ID and secret back and it handles the rest forever, refreshing the token itself.

**Jira**: `connect jira https://yoursite.atlassian.net|you@email|TOKEN`, with the
token from id.atlassian.com/manage/api-tokens.

---

## The chain this is really for

```
you:    go to my moonshot channel and read the chat
friday: In #moonshot (last 15 messages, 4 days ago to 1 hour ago):
        Sam asked for a concise overview of your technical work, to help
        plan a roadmap and align future responsibilities.

you:    did we ever talk about this?
friday: Yes. 9 days ago, matching moonshot, overview: "the technical
        overview thing"...  Say "open that one" and I'll bring it up.

you:    open that one
friday: [resumes that session in a new window]
```

If there is no past session, `start a new session on this` opens one already
carrying the Slack context, so you never retype it.

---

## What it will not do

Friday says all of this itself rather than discovering it mid-task:

- Post or send anything in Slack until you allow it. `draft a reply` writes one;
  `let yourself post` lets Friday send it, and even then only after showing you
  the words and waiting for `send it`. `turn off posting` undoes it.
- Put anything in your calendar, or schedule a meeting.
- Comment, merge or change anything anywhere. Everything is read-only.
- Write code, or start work by itself.
- See inside another person's sessions on this Mac. It can see that they exist.

Ask `are you using claude for this?` and it will tell you the truth: a local
model on this Mac, local speech in and out, and nothing sent to anyone.

---

## When something looks wrong

- **It says a session is not running when it is.** Reading the fleet is cached
  for a few seconds. Ask again.
- **It asks "did you mean X?" a lot.** That is deliberate. A clear match is acted
  on, a plausible one is asked about. Acting on a weak match once meant reporting
  on the wrong session as though you had named it.
- **A summary looks thin.** Say `say more` for the agent's exact words. Any
  summary containing a file, code or number that was not in the original is
  thrown away, so occasionally you get the raw text instead of a tidy sentence.
  That is the trade on purpose.
- **Nothing is being announced.** Check `quiet` is off, and that you have not
  muted that session.

Deeper detail, architecture and the roadmap are in `README.md`.
