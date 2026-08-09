# Try Friday

    cd ~/friday
    python3 run.py

Then open **http://127.0.0.1:8765** in your browser.

### From your phone

    python3 run.py --phone

It prints a URL with a key in it. Open that on your phone (same wifi, or over
your Tailscale network). The key is required, so keep the URL private: Friday
can open windows and type into your running agents, so anyone with that link
can drive your machine.

## What to try

Type or hold the mic:

- `what's running?` — the fleet in plain English
- `who needs me?`
- `open <session>` — brings that terminal to the front (just does it, no
  permission theatre, because it is instantly reversible)
- `tell <session> to <something>` — routes into that session. If you name it
  exactly, it sends; if Friday had to guess which one you meant, it asks first
- `quiet` / `resume`
- anything else is just conversation

**Your past work** (not just what is running)
- `find the session where I was learning tokenization`
- `what was I working on recently?`

**Other people on this Mac**
- `are there other users running sessions?` — it can see that they exist, and
  deliberately cannot see inside them

**GitHub** (already connected, it uses your `gh` login)
- `anything on github?` — notifications and your open pull requests
- `what are my open issues?`
- `search github for <thing>`

**Email and Jira** (each needs a token; ask and Friday tells you how)
- `any new email?`
- `my jira tickets`

**The chain this is really for**

    you: go to my eng group in slack and read the chat
    friday: [reads it] Rahul is asking whether the webhook retries are
            idempotent, and wants a decision before Friday's deploy.
    you: did we ever talk about this?
    friday: Yes. 9 days ago: "the webhook retry thing"…  say "open that one"
    you: open that one
    friday: [resumes that session in a new window]

If there is no past session, say `start a new session on this` and Friday opens
one already carrying the Slack context, so you never retype it.

**Sessions, past and present**
- `open <name>` — raise a running session's window
- `open that one` — after a search: raises it if running, RESUMES it if closed
- `start a new session on <thing>` — opens a new window already briefed

**What's broken / what you've been doing** (GitHub, already connected)
- `what's broken?` — failing CI, grouped so 49 notifications read as 4 problems
- `what have I been doing lately?`
- `what are my open issues?`

**Connections**
- `what's connected?`
- `connect slack` / `connect gmail` / `connect jira` — Friday gives you the
  shortest real path for each, and opens a browser itself where that works

**Slack** (needs one setup step)
- `go to my <name> group in slack and read the chat` — reads it and tells you
  what is actually being asked
- `search slack for <thing>`
- To connect: make an app at api.slack.com/apps with the user scopes
  `search:read`, `channels:history`, `users:read`, install it to your
  workspace, then tell Friday: `connect slack xoxp-your-token`
- The token is saved owner-only in `~/.friday/`, and Friday refuses to use it
  if the file is readable by anyone else. Everything is read-only: Friday never
  posts, comments or merges.

Tap the speaker icon to have Friday read replies aloud. Hold the mic to talk;
transcription is the local whisper, nothing leaves the machine.

## What it does on its own

While it is open, Friday watches every Claude Code session and brings things up
unprompted when one genuinely needs you (blocked on a question, errored, stuck,
finished). Those messages look different from replies, so you can always tell
who started talking. It will not interrupt you about a session you are already
looking at, will not talk over you, and caps itself at four interruptions an
hour.

## Known gaps (honest list)

- The fleet strip is glanceable but not yet an action surface: no peek panel to
  read a question and answer it inline.
- Asks have no lifecycle yet: they do not collapse into a one-line resolution,
  and there is no "+2 waiting" queueing, so several at once would stack.
- No suggested replies on an ask (the design calls for 2 to 4 tappable ones).
- No target chip in the composer, so routing is inferred rather than visible.
- No presence gating yet: it does not know whether you are at the page, at the
  machine, or away, so notifications are not routed differently.
- The phone uses the same page with no phone-specific layout yet (the design
  calls for a collapsed fleet chip and one visible ask at a time).
