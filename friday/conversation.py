"""What Friday does when you say something.

This is the assistant. One thread, and everything arrives in it: what you type
or say, what Friday answers, and the things Friday brings up on its own when an
agent needs you.

The design rule that keeps it honest: understanding what you MEANT is a fast
deterministic pass first, and only genuinely open-ended talk reaches the model.
That is not a performance trick, it is a correctness one. "Open jobhunt" must do
the same thing every single time; a model that is right 95% of the time is the
wrong tool for a command that moves your work around.

Actions are TIERED by how reversible they are and by how sure Friday is, not
confirmed uniformly. Asking "Open jobhunt?" every single time is friction you
hit fifty times a day, and a confirmation you always say yes to stops being a
safety mechanism: you learn to tap through it. So:

  tier 0  do it, say so, offer undo      reversible and unambiguous
  tier 1  ask first, inline              Friday had to guess, or it writes
                                         into another agent's session
  tier 2  read it back, explicit yes     irreversible: push, merge, delete

The deciding question for tier 0 vs 1 is not WHICH action it is, it is whether
Friday had to guess. If you named a session and exactly one matches, that was
your confirmation.
"""

import re
import time
from pathlib import Path

from . import (actions, agents, budget as budgets, connectors, engine,
               feeds,
               fleetcache, inbox, memory, nearest, plan as plans,
               push, replies, watchtower, when)

# What kind of thing the user just said.
ASK_FLEET = "fleet"        # "what's running", "who needs me"
OPEN = "open"              # "open jobhunt", "switch to api"
TELL = "tell"              # "tell api to use redis"  (routed to an agent)
CONFIRM = "confirm"        # "yes", "do it"
CANCEL = "cancel"          # "no", "cancel"
QUIET = "quiet"            # "quiet", "stop talking"
RESUME = "resume"          # "resume", "you can talk again"
NEEDS = "needs"            # "what does api need?"
FIND = "find"              # "find the session where I set up redis"
RECENT = "recent"          # "what was I working on yesterday"
OTHERS = "others"          # "are there other users' sessions?"
READ_CHANNEL = "readchan"  # "go to my #eng slack and read the chat"
GITHUB = "github"          # "anything on github", "search github for X"
ISSUES = "issues"          # "what are my open issues"
BROKEN = "broken"          # "what's broken?" / "is anything failing?"
ACTIVITY = "activity"      # "what have I been doing lately"
MAIL = "mail"              # "any new email"
JIRA = "jira"              # "my jira tickets"
DID_WE = "didwe"           # "did we ever talk about this?"
SLACK = "slack"            # "search slack for X"
CONNECT = "connect"        # "connect slack <token>"
OPEN_FOUND = "openfound"   # "open that one" after a search
NEW_SESSION = "newsession" # "start a new session on this"
ENGINE = "engine"          # "are you using claude for this?"
ASK_ALL = "askall"         # "ask everyone what they're working on"
MORE = "more"              # "say more", "what exactly did it say"
MISSED = "missed"          # "what did I miss?"
DRAFT = "draft"            # "draft a reply"
SEND = "send"              # "send it" after a draft
ALLOW = "allow"            # "let yourself post to slack"
NEXT = "next"              # "what should I work on?"
FIRE = "fire"              # "what's on fire?"
HELP = "help"              # "what can you do?"
SCHEDULE = "schedule"      # "put it in for Thursday at 4"
TICKET = "ticket"          # "file a ticket: the parser breaks on PDFs"
MOVE = "move"              # "move PROJ-12 to done"
PLAN = "plan"              # "plan: do a, then b, then c"
PLAN_GO = "plango"         # "run the plan"
PLAN_WHERE = "planwhere"   # "where is the plan?"
BRIEF = "brief"            # "brief me", "where does everything stand"
MUTE = "mute"              # "ignore jobhunt for now"
STUCK = "stuck"            # "is anyone stuck?"
WHO = "who"                # "who am I talking to?"
WATCH = "watch"            # "tell me when voicebridge is done"
STOP = "stop"              # "stop voicebridge"
CHAT = "chat"              # anything else: a real conversation

# Conducting more than one agent at a time. Asking each of five sessions the
# same question by hand, then going to five windows for the answers, is the work
# Friday is supposed to remove.
# A summary you cannot check is a summary you have to trust. These give you the
# agent's own words, and tell you who a bare reply would reach.
# Coming back to the desk. Scrolling to work out what happened while you were
# away is the same manual sweep as walking the windows.
# Google client credentials pasted as a pair. Without this, "gmail 123.apps...
# GOCSPX-..." reads as a question about mail, and the setup silently never
# starts.
_GOOGLE_CREDS_RE = re.compile(
    r"\b(?:gmail|google)\s+([\w.\-]+\.apps\.googleusercontent\.com)\s+"
    r"(\S{8,})", re.I)

# Friday holds no write scope in Slack on purpose, so a reply is something it
# writes and you send. Offering to "reply" and then not sending would be the
# worst of both.
# The whole picture, asked for rather than waited on. The unprompted stream is
# deliberately sparse, so there has to be a way to pull everything at once.
# A plan is written down before any of it runs, and nothing runs until you say
# so. "plan: a, then b, then c" or a numbered list.
# Filing a ticket from a conversation is the moment a Slack thread becomes work,
# and it is the whole front half of the conductor idea.
# The other half of "Sam wants a meeting Thursday". Friday could report it and
# do nothing about it, which is half a sentence.
# The front half of the conductor idea, and the only place Friday is allowed an
# opinion rather than a report.
_NEXT_RE = re.compile(
    r"\bwhat\s+(?:should|shall|do)\s+i\s+(?:work\s+on|do|start\s+with|pick)\b"
    r"|\bwhat'?s?\s+next\b|\bwhere\s+(?:should|do)\s+i\s+start\b"
    r"|\bwhat\s+should\s+i\s+be\s+doing\b", re.I)

_SCHEDULE_RE = re.compile(
    r"\b(?:put|add|book|schedule|pencil)\s+(?:it|that|a\s+\w+|the\s+\w+|"
    r"us|me)?\s*(?:in|down|on)?\b.*?"
    r"(?:for|at|on)\s+(.+?)\s*[.!]?$"
    r"|\b(?:schedule|book)\s+(?:a\s+)?(?:meeting|call|sync)\b\s*(.*)$",
    re.I)

_TICKET_RE = re.compile(
    r"\b(?:file|create|open|raise|make)\s+(?:a\s+|an\s+)?"
    r"(?:jira\s+|linear\s+)?(?:ticket|issue|bug|task)\b"
    r"(?:\s+(?:in|on|for)\s+([A-Z][A-Z0-9_]{1,9}))?"
    r"(?:\s*[:,]\s*(.+))?$", re.I)
_MOVE_RE = re.compile(
    r"\b(?:move|set|mark|transition|put)\s+([A-Za-z][A-Za-z0-9]*-\d+)\s+"
    r"(?:to|as|into)\s+(.+?)\s*[.!]?$", re.I)

_PLAN_RE = re.compile(
    r"^\s*(?:make|write|draft)?\s*a?\s*plan(?:\s+for\s+(\S+))?\s*[:,]\s*(.+)$",
    re.I | re.S)
_PLAN_GO_RE = re.compile(
    r"\b(?:run|start|go ahead with|approve|do)\s+(?:the\s+)?plan\b|"
    r"^\s*(?:approved|go ahead)\s*[.!]?\s*$", re.I)
_PLAN_WHERE_RE = re.compile(
    r"\b(?:where\s+(?:is|are)\s+(?:we|the plan)|how(?:'?s| is)\s+the\s+plan|"
    r"what'?s?\s+left|plan\s+status|show\s+(?:me\s+)?the\s+plan)\b", re.I)
_PLAN_STOP_RE = re.compile(r"\b(?:stop|pause|cancel|abandon)\s+the\s+plan\b",
                           re.I)

_BRIEF_RE = re.compile(
    r"\bbrief\s+me\b|\bwhere\s+(?:does|do)\s+everything\s+stand\b|"
    r"\bwhat(?:'?s| is|\s+are)?\s+(?:the\s+)?"
    r"(?:situation|status|state\s+of\s+play)\b|"
    r"\bfull\s+(?:picture|rundown)\b|\beverything\s+i\s+need\s+to\s+know\b",
    re.I)

# Sending is never inferred from a sentence about sending. It happens only when
# you say yes to a specific piece of text Friday has just shown you.
_SEND_RE = re.compile(
    r"^\s*(?:go ahead and\s+)?(?:send|post)\s*(?:it|that|this)?\s*"
    r"(?:to\s+(?:slack|#?[\w.\-]+))?\s*[.!]?\s*$", re.I)
_ALLOW_RE = re.compile(
    r"\b(?:let|allow)\s+(?:yourself|you)\s+(?:to\s+)?(post|write|reply|comment)"
    r"|\b(?:enable|turn on)\s+(?:slack\s+)?(?:posting|writing|replies)\b"
    r"|\b(?:stop|disable|turn off)\s+(?:yourself\s+)?(?:posting|writing)\b"
    r"|\byou\s+can\s+(?:post|reply|comment)\b", re.I)

_DRAFT_RE = re.compile(
    r"\b(?:draft|write|compose)\s+(?:me\s+)?(?:a\s+|the\s+)?"
    r"(?:reply|response|answer|message)\b(?:\s+(?:saying|that says)\s+(.*))?$"
    r"|\breply\s+(?:saying|with)\s+(.*)$", re.I)

_MISSED_RE = re.compile(
    r"\bwhat\s+(?:did\s+i|have\s+i)\s+miss(?:ed)?\b|\bcatch\s+me\s+up\b"
    r"(?!\s+on\b)|\bwhat\s+happened\s+while\s+i\s+was\s+(?:away|out|gone)\b|"
    r"\banything\s+(?:new|since)\b", re.I)
# One noisy agent should not cost you the whole feature.
_MUTE_RE = re.compile(
    r"\b(?:ignore|mute|silence|stop\s+telling\s+me\s+about)\s+"
    r"(?:the\s+)?([\w.\- ]{2,40}?)(?:\s+session)?(?:\s+for\s+now)?\s*[.!?]?$"
    r"|\b(?:unmute|listen\s+to|un-?ignore)\s+(?:the\s+)?([\w.\- ]{2,40}?)"
    r"(?:\s+session)?\s*[.!?]?$", re.I)
_STUCK_RE = re.compile(
    r"\b(?:is\s+)?any(?:one|thing|body)\s+(?:stuck|blocked|waiting)\b|"
    r"\bwho(?:'?s|\s+is)?\s+(?:stuck|blocked|waiting)\b|"
    r"\bwhat(?:'?s|\s+is)?\s+blocked\b", re.I)

_MORE_RE = re.compile(
    r"\b(?:say|tell me)\s+more\b|\bthe\s+(?:full|whole|exact)\s+"
    r"(?:thing|version|message|reply)\b|\bwhat\s+exactly\s+did\s+"
    r"(?:it|he|she|they|\S+)\s+say\b|\bin\s+full\b|\bverbatim\b", re.I)
_WHO_RE = re.compile(
    r"\bwho\s+am\s+i\s+(?:talking|speaking)\s+to\b|"
    r"\bwhich\s+(?:one|session)\s+(?:am\s+i|are\s+we)\b|"
    r"\bwho'?s?\s+(?:the\s+)?target\b", re.I)

_ASKALL_RE = re.compile(
    r"\b(?:ask|tell)\s+(?:them\s+all|everyone|everybody|all(?:\s+the)?"
    r"(?:\s+sessions?|\s+agents?)?|each\s+(?:session|agent|one))\b\s*"
    r"(to|for|about|that)?\s*(.*)$", re.I)
# "tell me when it's done" is a standing request, not a question about now.
_WATCH_RE = re.compile(
    r"\b(?:tell|let)\s+me\s+know\s+when\b|\bnotify\s+me\s+when\b|"
    r"\bping\s+me\s+when\b|\bwhen\s+(?:it|that|he|she|they|\S+)\s+"
    r"(?:is\s+|has\s+)?(?:done|finished|finishes|ready)\b", re.I)
_STOP_RE = re.compile(
    r"\b(?:stop|interrupt|halt|cancel|escape)\s+(?:the\s+)?"
    r"([\w.\- ]{2,40}?)(?:\s+session)?\s*[.!?]?$", re.I)
# "it", "him", "that one": whoever we were just talking to.
_ITS_RE = re.compile(r"^(?:it|him|her|them|that\s+(?:one|session)|"
                     r"the\s+same\s+one)$", re.I)

_FLEET_RE = re.compile(
    r"\b(what('?s| is)? (running|going on|happening)|status|who needs me|"
    r"which (agents?|sessions?) need|what are you (running|watching)|"
    r"how are (things|we)|(show|list) (me )?(my )?(agents?|sessions?))\b", re.I)
_OPEN_RE = re.compile(
    r"\b(open|switch to|go to|jump to|resume|show me)\s+(?:the\s+)?"
    r"(?:session\s+)?([\w.\-]+)", re.I)
# "open it", "open that" name nothing. Treating a pronoun as a session name is
# how "can you open it through Claude?" became "I couldn't bring Reply with
# exactly ALPHA to the front": it matched a session labelled by its own first
# prompt. A pronoun means ask, never guess.
_PRONOUNS = {"it", "that", "this", "them", "one", "there", "here", "him", "her"}
# A pronoun IS a valid target once you have been talking to a session: "ask it
# to also run the tests" is how anybody would say it. What is never a target is
# yourself, which is why "tell me the status" must not become an instruction to
# a session called "me".
_STANDS_FOR_SESSION = {"it", "that", "this", "them", "one", "him", "her"}
_NEVER_A_SESSION = {"me", "us", "myself", "yourself", "everyone", "everybody"}
# Words that are never a session name, so a greedy pattern cannot mistake an
# article for the thing you meant.
_FILLER = {"the", "a", "an", "my", "your", "of", "to", "and", "session"}
# People do not phrase instructions as clean commands. "Can you go to the
# voicebridge session and tell him that the design looks good" must work, not
# fall through to chat and come back as a rephrasing of itself.
# "ask" means you want the answer, "tell" means you want it done. Both send a
# prompt; only one is worth waiting on.
_ASKED_RE = re.compile(r"\b(?:ask|reply to|answer)\b", re.I)
_TELL_RE = re.compile(
    r"(?:^|\b)(?:go to|open)?\s*(?:the\s+)?(?:session\s+(?:of|called)\s+)?"
    r"(?:tell|ask|reply to|answer|send(?:\s+a\s+message)?\s+to|message)\s+"
    r"(?:the\s+)?(?:session\s+)?([\w.\-]+)\s+"
    r"(?:session\s+)?(?:that\s+|to\s+|about\s+)?(.+)$", re.I)
_FIND_RE = re.compile(
    r"\b(?:find|search(?:\s+for)?|look for|which session|what session|"
    r"where did i|where was i|the (?:session|chat|conversation) (?:where|about|"
    r"in which))\b\s*(.*)$", re.I)
_RECENT_RE = re.compile(
    r"\b(?:what (?:was|were) i (?:working on|doing)|recent sessions?|"
    r"my recent work|what have i been (?:working on|doing))\b", re.I)
_OTHERS_RE = re.compile(
    r"\b(?:other users?|another user|someone else|other accounts?|"
    r"anyone else|nikhil|other people)\b", re.I)
_GITHUB_RE = re.compile(
    r"\b(?:github|gh|pull requests?|prs?\b|my notifications?)\b\s*(.*)$", re.I)
_SLACK_RE = re.compile(r"\bslack\b\s*(.*)$", re.I)
# "go to my neither group in slack and read the chat"
# Channel names arrive as several words, because speech splits a compound name
# at the wrong place ('moonshot' becomes 'moon shot'), and the read verb can
# come before the name as easily as after it. Requiring a single token followed
# by 'slack' or 'chat' meant "read the moon shot channel" was not recognised
# as a Slack request at all, and fell through to the model, which invented a
# refusal about not having access.
_READCHAN_RE = re.compile(
    r"\b(?:go to|open|check|read|look at|catch me up on|"
    r"what(?:'?s| is) (?:in|happening in|going on in))\b"
    r"[^.]*?#?([\w.\-]+(?:\s+[\w.\-]+){0,3}?)\s+(?:group|channel)\b"
    r"|\b(?:group|channel)\s+(?:called\s+|named\s+)?#?([\w.\-]+)\b"
    r"|\bslack\b[^.]*?#?([\w.\-]+(?:\s+[\w.\-]+){0,3}?)\s+(?:group|channel)\b",
    re.I)
# You do not always say the word "channel". "Can you read about my chat from
# moon shot?" names a source with "from", and requiring the literal word
# channel or group meant that sentence never reached Slack at all: it fell
# through to the model, which invented "I don't have access to personal chat
# histories" while Friday was connected and could read it.
# Saying any of these IS asking to be caught up, with no need to also name a
# noun: "what was discussed in X" has no word like chat or messages in it.
_TALKREAD_RE = re.compile(
    r"\b(?:what (?:was|were|got) (?:talked|discussed|said|asked)|"
    r"what (?:did|does)\s+[\w']+\s+(?:say|ask|want)|"
    r"catch me up|what happened|what'?s new)\b", re.I)
# These are about reading, but could be about anything, so they need a subject.
_READVERB_RE = re.compile(r"\b(?:read|summar(?:ise|ize)|go through)\b", re.I)
_SUBJECT_RE = re.compile(
    r"\b(?:chat|chats|messages?|conversation|thread|dms?|talk)\b", re.I)
# The source of it: "from moon shot", "in moonshot".
_SOURCE_RE = re.compile(
    r"\b(?:from|in|on)\s+(?:my\s+|the\s+|our\s+)?"
    r"([\w.\-]+(?:\s+[\w.\-]+){0,2})", re.I)

# Words that ride along with a spoken channel name but are never part of it.
# "read the chat in slack moonshot group" otherwise yields the channel name
# "chat in slack moonshot".
_NOT_NAME = {"to", "in", "at", "on", "my", "the", "our", "a", "that", "this",
             "chat", "slack", "message", "messages", "conversation", "read",
             "last", "few", "recent", "from"}


def _clean_channel(name: str) -> str:
    words = [w for w in (name or "").split()]
    while words and words[0].lower().strip(".,") in _NOT_NAME:
        words.pop(0)
    while words and words[-1].lower().strip(".,") in _NOT_NAME:
        words.pop()
    return " ".join(words).strip()
_ISSUES_RE = re.compile(r"\b(?:open\s+)?issues?\b", re.I)
_BROKEN_RE = re.compile(
    r"\b(?:what(?:'s| is)? (?:broken|failing|red)|anything (?:broken|failing)|"
    r"failing (?:tests?|builds?|ci|workflows?)|ci status|builds? failing)\b", re.I)
_ACTIVITY_RE = re.compile(
    r"\b(?:what have i been (?:doing|up to)|my (?:recent )?activity|"
    r"what did i (?:do|push|ship))\b", re.I)
_MAIL_RE = re.compile(r"\b(?:e-?mail|gmail|inbox|mails?)\b\s*(.*)$", re.I)
_JIRA_RE = re.compile(r"\bjira\b|\btickets?\b", re.I)
# "errors" alone is too broad, it catches "what errors did the build hit". This
# wants the production question specifically.
# The first thing a new user types, and until now it fell through to the model,
# which on a fresh machine is not loaded, so the answer was "my brain isn't
# loaded yet". A dead end on the first word.
_HELP_RE = re.compile(
    r"^\s*(?:help|\?|what can you do|what do you do|what are you|"
    r"what can i (?:ask|say|do)|how does this work|commands?|"
    r"what (?:else )?can you help (?:me )?with)\b|^\s*\?+\s*$", re.I)
_FIRE_RE = re.compile(
    r"\bsentry\b|\bon fire\b|\banything (?:broken|breaking) in prod"
    r"|\bprod(?:uction)?\s+(?:errors?|issues?|exceptions?|ok|okay|healthy)"
    r"|\bare (?:there|we) (?:any )?(?:new )?(?:errors?|exceptions?|crashes)"
    r"|\bwhat(?:'?s| is) (?:on fire|breaking|crashing)\b"
    r"|\bany (?:new )?(?:errors?|exceptions?|crashes)\b", re.I)
# ...unless you plainly meant CI, where "errors" means a failing job.
_NOT_FIRE_RE = re.compile(r"\b(?:build|ci|tests?|workflow|pipeline)\b", re.I)
# "are you using Claude for this?" A fair question with a real answer, and one
# a language model asked to improvise will get wrong in the flattering direction.
_ENGINE_RE = re.compile(
    r"\b(?:are|do)\s+you\s+(?:using|use)\s+(?:claude|chatgpt|gpt|openai|an?\s+api)"
    r"|\b(?:what|which)\s+(?:model|brain|llm|engine)\b"
    r"|\bis\s+(?:this|that)\s+claude\b"
    r"|\bwhere\s+(?:do|does)\s+(?:my|the)\s+(?:data|messages?|audio)\s+go\b", re.I)
# "did we ever talk about this?" right after reading something
_DIDWE_RE = re.compile(
    r"\b(?:did (?:we|i) (?:ever )?(?:talk|discuss|work)|have (?:we|i) "
    r"(?:ever )?(?:talked|discussed|worked)|look into claude|check claude|"
    r"any (?:past )?session about)\b", re.I)
# NOT anchored to the start: spoken input arrives as "Friday, connect slack
# xoxp-…" and requiring "connect" first meant it never matched.
_CONNECT_RE = re.compile(r"\bconnect\s+(?:to\s+)?(\w+)(?:\s+([\w.\-]{8,}))?", re.I)
# A pasted token is unmistakable, so accept it on its own and work out where it
# belongs from its prefix. Asking someone to remember command syntax while
# holding a secret in their clipboard is bad design.
# The xoxe. prefix MUST come first in the alternation, or the pattern matches
# the xoxp- part in the middle of "xoxe.xoxp-…" and saves a truncated token
# that can never work. Slack issues xoxe.xoxp- when token rotation is on.
_TOKEN_RE = re.compile(
    r"(xoxe\.xoxp-[\w.\-]{10,}|xoxe-[\w.\-]{10,}|xoxp-[\w-]{10,}"
    r"|xoxb-[\w-]{10,}|ya29\.[\w.\-]{20,}|lin_api_[\w-]{20,}"
    r"|sntryu_[\w]{20,}|sntrys_[\w]{20,})")
# What to say next, per connector. This was one hard-coded Slack sentence for
# every connector, so connecting Sentry told you to go read a Slack channel.
# The first thing you try after connecting something is the moment it either
# works or gets abandoned, and it should be about the thing you just connected.
_FIRST_TRY = {
    "slack": "go to my <channel> group in slack and read the chat",
    "github": "what's failing?",
    "jira": "what are my tickets?",
    "linear": "what are my tickets?",
    "sentry": "what's on fire?",
    "gmail": "any new email?",
}
_CONNS_RE = re.compile(
    r"\b(?:what(?:'?s| is)? connected|connections?|integrations?|"
    r"what (?:tools|apps) (?:do you have|are connected))\b", re.I)
# Anchored to the END on purpose: "open that one" is this intent, but "go to
# the session of voicebridge and tell him…" is an instruction to that session,
# and an unanchored pattern swallowed it.
_OPENFOUND_RE = re.compile(
    r"\b(?:open|resume|bring up|go to)\s+(?:that|it|the)"
    r"(?:\s+(?:one|session|chat|conversation))?\s*[.!?]?$", re.I)
_NEWSESSION_RE = re.compile(
    r"\b(?:start|open|create|spin up|make)\s+(?:a\s+)?new\s+"
    r"(?:claude\s+)?(?:session|chat)\b\s*(?:on|about|for|to)?\s*(.*)$", re.I)
_NEEDS_RE = re.compile(
    r"\b(?:what (?:does|is)|why (?:does|is))\s+([\w.\-]+)\s+"
    r"(?:need|want|waiting|asking|blocked|stuck)", re.I)
_TELL_BACK_RE = re.compile(
    # the (?!…) stops "the session of voicebridge" from capturing "the": that
    # alternative sits earlier in the sentence and would otherwise win
    r"\b(?:session\s+(?:of|called|named)\s+([\w.\-]+)"
    r"|(?!the\b|a\b|an\b|my\b|your\b|this\b|that\b)([\w.\-]+)\s+session)\b"
    r"[^.]*?\b(?:tell|ask|say to|message)\b\s*(.+)$", re.I)
_NOISE_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]|\W*", re.I)
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|sure|do it|go ahead|please|ok(ay)?)\b", re.I)
_NO_RE = re.compile(r"^\s*(no|nope|cancel|stop|don'?t|never ?mind)\b", re.I)
_QUIET_RE = re.compile(r"^\s*(quiet|shush|be quiet|stop talking|silence)\b", re.I)
_RESUME_RE = re.compile(r"^\s*(resume|unmute|you can talk|start talking)\b", re.I)


def _strip_verbs(s: str) -> str:
    """'for the deploy thread' -> 'deploy thread'. What is left is the search."""
    s = re.sub(r"^\s*(?:for|about|on|in|any|anything|my|the)\b\s*", "", (s or ""),
               flags=re.I).strip(" ?.")
    return s


def _joiner_before(said: str, msg: str) -> str:
    """The word that linked the target to the message ('to', 'about', 'for').

    It carries the intent: "ask X FOR a summary" and "ask X ABOUT the plans" are
    requests, and sending the bare fragment ("a summary", "the plans") is not
    what anyone would type into an agent."""
    if not (said and msg):
        return ""
    low, m = said.lower(), msg.lower()
    i = low.find(m)
    if i <= 0:
        return ""
    before = low[:i].strip().split()
    return before[-1] if before else ""


def _phrase(msg: str, joiner: str, want: bool) -> tuple:
    """The message as an agent should receive it, and whether to wait."""
    # The joiner sometimes survives inside the message instead of in front of
    # it, depending on which pattern matched. Same word, same meaning.
    low = (msg or "").lower()
    if low.startswith("for "):
        msg, joiner = msg[4:], "for"
    elif low.startswith("about "):
        msg, joiner = msg[6:], "about"
    if joiner == "for":
        return "Give me " + msg, True
    if joiner == "about" and want:
        return "Tell me about " + msg, True
    return msg, want


def classify(text: str) -> tuple:
    """(intent, payload). Deterministic and ordered: the most specific command
    wins, and only what is left over counts as conversation."""
    t = (text or "").strip()
    if _NEXT_RE.search(t):
        return NEXT, {}
    m = _SCHEDULE_RE.search(t)
    if m and re.search(r"\b(meet|meeting|call|sync|calendar|invite|catch\s?up|"
                       r"put it in|pencil)\b", t, re.I):
        return SCHEDULE, {"said": t}
    m = _MOVE_RE.search(t)
    if m:
        return MOVE, {"key": m.group(1).upper(), "to": m.group(2).strip()}
    m = _TICKET_RE.search(t)
    if m:
        return TICKET, {"project": (m.group(1) or "").upper(),
                        "summary": (m.group(2) or "").strip()}
    if _PLAN_STOP_RE.search(t):
        return PLAN_GO, {"stop": True}
    if _PLAN_WHERE_RE.search(t):
        return PLAN_WHERE, {}
    m = _PLAN_RE.match(t)
    if m:
        return PLAN, {"target": (m.group(1) or "").strip(),
                      "body": m.group(2).strip()}
    if _PLAN_GO_RE.search(t):
        return PLAN_GO, {"stop": False}
    if _BRIEF_RE.search(t):
        return BRIEF, {}
    m = _ALLOW_RE.search(t)
    if m:
        off = bool(re.search(r"\b(stop|disable|turn off)\b", t, re.I))
        return ALLOW, {"on": not off}
    if _SEND_RE.match(t):
        return SEND, {}
    if not t:
        return CHAT, {}
    # Specific multi-word intents go FIRST. "resume that session" is not the
    # voice command "resume", and "open a new session" is not "open <name>":
    # the generic patterns would otherwise swallow both.
    # a bare token, pasted with no command around it
    m = _TOKEN_RE.search(t)
    if m:
        tok = m.group(1)
        # xoxe.xoxp- and xoxe- are Slack too (rotation-enabled tokens). Missing
        # them here meant a pasted token silently became "what's connected?".
        which = ("slack" if tok.startswith(("xoxp-", "xoxb-", "xoxe.", "xoxe-"))
                 else "gmail" if tok.startswith("ya29.")
                 else "linear" if tok.startswith("lin_api_")
                 else "sentry" if tok.startswith(("sntryu_", "sntrys_")) else "")
        return CONNECT, {"which": which, "token": tok}
    m = _GOOGLE_CREDS_RE.search(t)
    if m:
        return CONNECT, {"which": "gmail",
                         "token": f"{m.group(1)} {m.group(2)}"}
    if _CONNS_RE.search(t):
        return CONNECT, {"which": "", "token": ""}
    m = _CONNECT_RE.search(t)
    if m:
        return CONNECT, {"which": m.group(1), "token": m.group(2) or ""}
    if _ENGINE_RE.search(t):
        return ENGINE, {}
    m = _DRAFT_RE.search(t)
    if m:
        return DRAFT, {"gist": (m.group(1) or m.group(2) or "").strip()}
    if _MISSED_RE.search(t):
        return MISSED, {}
    if _STUCK_RE.search(t):
        return STUCK, {}
    m = _MUTE_RE.search(t)
    if m and len(t.split()) <= 7:
        return MUTE, {"name": (m.group(1) or m.group(2) or "").strip(),
                      "on": bool(m.group(1))}
    if _WHO_RE.search(t):
        return WHO, {}
    if _MORE_RE.search(t):
        return MORE, {}
    m = _ASKALL_RE.search(t)
    if m:
        return ASK_ALL, {"joiner": (m.group(1) or "").lower(),
                         "message": (m.group(2) or "").strip()}
    if _WATCH_RE.search(t):
        return WATCH, {"said": t}
    m = _STOP_RE.search(t)
    if m and len(t.split()) <= 6:
        return STOP, {"name": m.group(1).strip(), "said": t}
    if _DIDWE_RE.search(t):
        return DID_WE, {}
    m = _READCHAN_RE.search(t)
    if m:
        name = _clean_channel(next((g for g in m.groups() if g), ""))
        if name:
            return READ_CHANNEL, {"channel": name}
    # "read my chat from X" / "what was discussed in X": a read verb, something
    # to read, and a named source, with no need to say the word channel.
    if _TALKREAD_RE.search(t) or (_READVERB_RE.search(t)
                                  and _SUBJECT_RE.search(t)):
        # Hand over the sentence as well as the best guess at the name. Pulling
        # the name out by grammar picks the wrong preposition often enough that
        # the sentence itself has to stay available: the real channel list is a
        # far better anchor than the shape of the request.
        srcs = [_clean_channel(m.group(1)) for m in _SOURCE_RE.finditer(t)]
        srcs = [x for x in srcs if x]
        return READ_CHANNEL, {"channel": srcs[-1] if srcs else "",
                              "said": t}
    if _HELP_RE.search(t):
        return HELP, {}
    if _FIRE_RE.search(t) and not _NOT_FIRE_RE.search(t):
        return FIRE, {}
    if _JIRA_RE.search(t):
        return JIRA, {}
    m = _MAIL_RE.search(t)
    if m:
        return MAIL, {"query": _strip_verbs(m.group(1))}
    if _BROKEN_RE.search(t):
        return BROKEN, {}
    if _ACTIVITY_RE.search(t):
        return ACTIVITY, {}
    if _ISSUES_RE.search(t) and "github" not in t.lower():
        return ISSUES, {}
    m = _NEWSESSION_RE.search(t)
    if m:
        return NEW_SESSION, {"about": m.group(1).strip()}
    if _OPENFOUND_RE.search(t):
        return OPEN_FOUND, {}
    if _QUIET_RE.match(t):
        return QUIET, {}
    if _RESUME_RE.match(t):
        return RESUME, {}
    # "go to the voicebridge session and tell him that X": the name lands
    # BEFORE the verb, which is how people actually speak.
    m2 = _TELL_BACK_RE.search(t)
    if m2:
        # the pattern has two shapes ("<name> session …" and "session of
        # <name> …"), so take whichever pair matched
        name = m2.group(1) or m2.group(2) or ""
        msg = (m2.group(3) or "").strip()
        msg = re.sub(r"^(?:him|her|it|them)\s+(?:that\s+)?", "", msg, flags=re.I)
        low = name.lower()
        if low not in _NEVER_A_SESSION and (
                low not in _PRONOUNS | _FILLER
                or low in _STANDS_FOR_SESSION) and msg:
            want = bool(_ASKED_RE.search(t[:m2.start(3)] if m2.group(3)
                                         else t))
            msg, want = _phrase(msg, _joiner_before(t, msg), want)
            return TELL, {"name": name, "message": msg, "await": want,
                          "said": t}
    m = _TELL_RE.search(t)
    if m:
        name, msg = m.group(1), m.group(2).strip()
        # "tell voicebridge him that X" / "tell it that X": drop the pronoun
        msg = re.sub(r"^(?:him|her|it|them)\s+(?:that\s+)?", "", msg, flags=re.I)
        low = name.lower()
        if low not in _NEVER_A_SESSION and (
                low not in _PRONOUNS or low in _STANDS_FOR_SESSION) and msg:
            want = bool(_ASKED_RE.search(t[:m.start(1)]))
            msg, want = _phrase(msg, _joiner_before(t, msg), want)
            return TELL, {"name": name, "message": msg, "await": want,
                          "said": t}
    m = _OPEN_RE.search(t)
    if m and len(t.split()) <= 6:      # a command, not a sentence about opening
        name = m.group(2)
        if name.lower() in _PRONOUNS:
            return OPEN, {"name": ""}   # "open it": we must ask which
        return OPEN, {"name": name}
    if _SLACK_RE.search(t):
        return SLACK, {"query": _strip_verbs(_SLACK_RE.search(t).group(1) or t)}
    m = _GITHUB_RE.search(t)
    if m:
        return GITHUB, {"query": _strip_verbs(m.group(1))}
    if _OTHERS_RE.search(t):
        return OTHERS, {}
    if _RECENT_RE.search(t):
        return RECENT, {}
    m = _FIND_RE.search(t)
    if m and len(m.group(1).strip()) >= 3:
        return FIND, {"query": m.group(1).strip()}
    m = _NEEDS_RE.search(t)
    if m:
        return NEEDS, {"name": m.group(1)}
    if _FLEET_RE.search(t):
        return ASK_FLEET, {}
    if _YES_RE.match(t):
        return CONFIRM, {}
    if _NO_RE.match(t):
        return CANCEL, {}
    return CHAT, {}


class Friday:
    """One conversation, with memory of what it just offered to do."""

    def __init__(self):
        self.pending = None        # an action awaiting your yes
        self._last_found = []      # sessions a search turned up
        self._last_slack = []      # slack messages just read out
        # An offer Friday just made ("did you mean X?"), and what to do about
        # the answer. One mechanism for every kind of name, so a session, a
        # channel and a connector all behave the same way when misheard.
        self._offered = None
        # Who we are talking to. "ask it to also run the tests" and "tell me
        # when it's done" are how people speak once a session is in play, and
        # making you say the name every time is making you do the bookkeeping.
        self.target = ""
        self.quiet = False         # Friday's own switch, independent of vb's
        # Whether you are looking at the page. Pushing to a phone something
        # already on the screen in front of you is how a notification
        # permission gets revoked, and there is no second chance at that.
        self.watching = False
        self.watching_at = 0.0
        self._pushed = {}          # tag -> when, so one thing buzzes once
        self._last_draft = None    # the exact text "send it" refers to
        self._plan_id = 0
        # One plan at a time, on evidence: a step is done when the agent has
        # answered, not when Friday sent the prompt.
        self.plans = plans.Runner(
            announce=self.announce,
            send=actions.send_to_session,
            look=lambda sid: (fleetcache.snapshot() or {}).get(sid, {}),
            log=engine.log)
        # One place watches every session and reports what it said, so nothing
        # is announced twice and nothing is missed.
        # One allowance shared by everything that speaks unprompted, so a busy
        # minute produces one held list rather than three separate ones.
        self.budget = budgets.Budget()
        self.watch = watchtower.Watchtower(
            self.announce,
            log=engine.log,
            hushed=lambda: self.quiet,
            budget=self.budget)
        # The same idea for people rather than agents: a Slack message brought
        # to you with something you can do about it.
        # Everything that is not an agent and not Slack: GitHub, your repos,
        # your calendar. One dispatcher so each new tool inherits the same
        # rules instead of inventing its own idea of what is worth saying.
        self.feeds = feeds.Feeds(
            self.announce,
            log=engine.log,
            hushed=lambda: self.quiet,
            budget=self.budget)
        self.feeds.add("github", feeds.GitHubFeed(), period=180)
        self.feeds.add("git", feeds.GitFeed(), period=900)
        self.feeds.add("calendar", feeds.CalendarFeed(), period=120)
        # Faster than the rest on purpose: production being down is the one
        # thing where three minutes of delay is three minutes of outage.
        self.feeds.add("sentry", feeds.SentryFeed(), period=90)
        self.inbox = inbox.Inbox(
            self.announce,
            log=engine.log,
            hushed=lambda: self.quiet,
            budget=self.budget)
        self.history = []          # [{role, text, ts, kind}]
        self.focus = (engine.routing.new_focus()
                      if engine.AVAILABLE else {"mentioned": [], "ts": 0})

    # ---- the thread -------------------------------------------------------
    def add(self, role: str, text: str, kind: str = "",
            about: list = None) -> dict:
        msg = {"role": role, "text": text, "ts": time.time(), "kind": kind}
        # What the announcement is ABOUT travels with it, so the page can offer
        # the right thing to do. Without this an announcement is a sentence you
        # can read and not act on, which is the dashboard Friday exists not to
        # be.
        if about:
            msg["about"] = about[:4]
        self.history.append(msg)
        self.history = self.history[-400:]
        return msg

    # ---- the main entry point --------------------------------------------
    def handle(self, text: str) -> dict:
        """Take what the user said, return {reply, action, needs_confirm}."""
        # Whisper labels non-speech as [SOUND], [BLANK_AUDIO], (music) and so
        # on. Treating a door closing as a question produced "I don't know what
        # that sound is. Can you clarify?", which is a machine talking to noise.
        if _NOISE_RE.fullmatch((text or "").strip()):
            return {"reply": "", "needs_confirm": False, "action": {}}
        self.add("user", text)
        intent, payload = classify(text)

        # Something Friday offered a moment ago ("did you mean X?"). A yes takes
        # it; a short reply is another go at the name. Letting either fall
        # through to the model produced the same invented refusal three times
        # while the answer sat in a list Friday had already fetched.
        if self._offered and intent in (CONFIRM, CANCEL, CHAT):
            offer, self._offered = self._offered, None
            if intent == CONFIRM:
                return offer["yes"]()
            if intent == CANCEL:
                return self._say(offer.get("no") or "Okay. Which one did you "
                                                   "mean?")
            if len(text.split()) <= 4 and offer.get("again"):
                return offer["again"](text)
            self._offered = offer          # not an answer; the offer stands
        elif self._offered:
            self._offered = None

        # A pending offer takes precedence: "yes" means yes to THAT.
        if self.pending and intent in (CONFIRM, CANCEL):
            act = self.pending
            self.pending = None
            if intent == CANCEL:
                return self._say("Okay, left it alone.")
            return self._perform(act)

        if intent == QUIET:
            # "Quiet" is the command you reach for when Friday is being
            # annoying, so it is the last one that may fail loudly.
            if not self._hush(True):
                return self._say("I couldn't reach the attention engine, but "
                                 "I'll stay quiet myself.")
            return self._say("Quiet. I won't interrupt you.")
        if intent == RESUME:
            if not self._hush(False):
                return self._say("I couldn't reach the attention engine, but "
                                 "I'm listening.")
            return self._say("Listening again.")
        if intent == ASK_FLEET:
            return self._say(self.fleet_summary())
        if intent == NEEDS:
            return self._what_needs(payload["name"])
        if intent == FIND:
            return self._find_past(payload["query"])
        if intent == RECENT:
            return self._recent_work()
        if intent == OTHERS:
            return self._other_users()
        if intent == OPEN_FOUND:
            return self._open_found()
        if intent == NEW_SESSION:
            return self._new_session(payload.get("about", ""))
        if intent == CONNECT:
            return self._connect(payload["which"], payload["token"])
        if intent == READ_CHANNEL:
            return self._read_channel(payload["channel"],
                                      payload.get("said", ""))
        if intent == ENGINE:
            return self._engine()
        if intent == DID_WE:
            return self._did_we_discuss()
        if intent == ISSUES:
            return self._issues()
        if intent == BROKEN:
            return self._broken()
        if intent == ACTIVITY:
            return self._activity()
        if intent == MAIL:
            return self._mail(payload.get("query", ""))
        if intent == JIRA:
            return self._jira()
        if intent == GITHUB:
            return self._github(payload.get("query", ""))
        if intent == SLACK:
            return self._slack(payload.get("query", ""))
        if intent == OPEN:
            return self._propose_open(payload["name"])
        if intent == BRIEF:
            return self._brief()
        if intent == NEXT:
            return self._what_next()
        if intent == HELP:
            return self._help()
        if intent == FIRE:
            return self._fire()
        if intent == SCHEDULE:
            return self._schedule(payload["said"])
        if intent == TICKET:
            return self._file_ticket(payload["project"], payload["summary"])
        if intent == MOVE:
            return self._move_ticket(payload["key"], payload["to"])
        if intent == PLAN:
            return self._make_plan(payload["target"], payload["body"])
        if intent == PLAN_GO:
            return self._run_plan(payload.get("stop", False))
        if intent == PLAN_WHERE:
            return self._plan_status()
        if intent == ALLOW:
            return self._allow_write(payload["on"])
        if intent == SEND:
            return self._send_draft()
        if intent == DRAFT:
            return self._draft(payload.get("gist", ""))
        if intent == MISSED:
            return self._missed()
        if intent == STUCK:
            return self._stuck()
        if intent == MUTE:
            return self._mute(payload["name"], payload["on"])
        if intent == WHO:
            return self._who()
        if intent == MORE:
            return self._more()
        if intent == ASK_ALL:
            return self._ask_all(payload["message"], payload.get("joiner", ""))
        if intent == WATCH:
            return self._watch(payload.get("said", ""))
        if intent == STOP:
            return self._stop(payload["name"], payload.get("said", ""))
        if intent == TELL:
            return self._propose_tell(payload["name"], payload["message"],
                                      payload.get("await", False),
                                      payload.get("said", ""))
        if intent in (CONFIRM, CANCEL):
            return self._say("Nothing was waiting on you." if intent == CONFIRM
                             else "Okay.")

        # An agent asked you something a moment ago and this reads like the
        # answer: send it there rather than treating it as small talk. This is
        # the whole point of a supervisor, you answer once, in the thread, and
        # it reaches the right session.
        routed = self._maybe_route_answer(text)
        if routed is not None:
            return routed
        return self._chat(text)

    def _maybe_route_answer(self, text: str):
        """Return a reply if this belongs to a waiting agent, else None."""
        if not engine.AVAILABLE:
            return None
        try:
            r = engine.routing.route(text, self.focus, active_sid="",
                                     find=self._find)
        except Exception:
            return None
        if r.get("ask"):
            # Two agents are waiting: ask which, never guess. A wrong answer
            # delivered to the wrong agent is a wrong instruction it will act on.
            return self._say(r["ask"])
        sid = r.get("sid")
        if not sid or "answering" not in (r.get("why") or ""):
            return None
        label = r.get("label") or "it"
        ok = actions.send_to_session(sid, text)
        return self._say(f"Told {label}." if ok
                         else f"I couldn't reach {label}.",
                         action={"kind": "tell", "sid": sid})

    # ---- what it knows ----------------------------------------------------
    def fleet_summary(self) -> str:
        """Plain-English answer to 'what is going on', the question this whole
        product exists to answer."""
        if not engine.AVAILABLE:
            return "I can't see your sessions right now."
        # The sensor shells out and parses JSON, so it can fail. Letting that
        # reach the request handler turns "what's running" into a 500 and you
        # are told nothing at all, which is the worst of the possible answers.
        try:
            snap = fleetcache.snapshot()
        except Exception as e:
            try:
                engine.log(f"friday fleet: {e}")
            except Exception:
                pass
            return ("I can't read your sessions at the moment, so I don't know "
                    "what's running.")
        if not snap:
            return "Nothing is running."
        waiting = [r for r in snap.values() if r.get("question") or r.get("permission")]
        working = [r for r in snap.values() if r.get("status") == "working"
                   and r not in waiting]
        idle = [r for r in snap.values() if r.get("status") != "working"
                and r not in waiting]
        bits = []
        if waiting:
            names = _join([r["label"] for r in waiting])
            bits.append(f"{names} {_is(len(waiting))} waiting on you.")
        if working:
            bits.append(f"{_join([r['label'] for r in working])} "
                        f"{_is(len(working))} still working.")
        if idle:
            bits.append(f"{_join([r['label'] for r in idle])} {_is(len(idle))} done.")
        return " ".join(bits) or "Nothing is running."

    # ---- conducting more than one agent ---------------------------------
    # "you" from the agent's point of view: the question is being relayed, so
    # "what are THEY working on" has to arrive as "what are YOU working on".
    _RELAY = ((r"\bthey'?re\b", "you are"), (r"\bthey are\b", "you are"),
              (r"\bthey\b", "you"), (r"\btheir\b", "your"),
              (r"\bthem\b", "you"), (r"\beveryone\b", "you"))

    def _as_question(self, tail: str, joiner: str) -> str:
        q = (tail or "").strip().rstrip("?.")
        for pat, sub in self._RELAY:
            q = re.sub(pat, sub, q, flags=re.I)
        if joiner == "for":
            q = "Give me " + q
        if not q:
            q = "What are you working on right now?"
        return ("Answer in one or two sentences, no tool calls if you can help "
                "it: " + q + "?" if not q.endswith("?") else q)

    # Things that are worth a locked phone lighting up. Everything else can
    # wait until you look, and a phone that buzzes for everything gets muted.
    PUSH_KINDS = ("blocked", "slack")

    def at_the_page(self) -> bool:
        """Whether you were looking at Friday in the last half minute."""
        return bool(self.watching and time.time() - self.watching_at < 30)

    def _maybe_push(self, text: str, items: list) -> None:
        """Send this to the phone, or decide not to, and be strict about it.

        Four gates, all of which have to pass. Quiet means quiet. Something you
        are already reading is not news. Only things that need YOU, not every
        agent finishing a thought. And each thing once, however many times it is
        mentioned."""
        try:
            if self.quiet or self.at_the_page():
                return
            kinds = {(i or {}).get("kind", "") for i in (items or [])}
            if not (kinds & set(self.PUSH_KINDS)):
                return
            label = ""
            for i in (items or []):
                label = (i or {}).get("label") or label
            tag = (label or "friday").lower()
            now = time.time()
            # Same source, same minute: one buzz. An agent that repeats itself
            # must not become a phone that repeats itself.
            if now - self._pushed.get(tag, 0) < 60:
                return
            self._pushed[tag] = now
            title = f"{label} needs you" if label else "Friday"
            body = text.split("\n")[0][:200]
            # An agent blocked on you is high; a message from a person can wait
            # for the phone to wake on its own.
            blocked = "blocked" in kinds
            push.send_async(title, body, tag=tag,
                            urgency=0 if blocked else 1)
        except Exception:
            pass

    def _hush(self, on: bool) -> bool:
        """Go quiet, or stop being quiet. Reports whether it took effect.

        Friday keeps its own flag as well as asking voicebridge, so quiet still
        means quiet when the attention engine is unreachable."""
        self.quiet = on
        if not engine.AVAILABLE:
            return True
        try:
            engine.attention.hush() if on else engine.attention.unhush()
            return True
        except Exception:
            return False

    def _brief(self) -> dict:
        """One answer covering everything Friday can see.

        This is the single point of contact in one command: agents, people,
        GitHub, your repos, your calendar. Anything unreadable says so, because
        a brief with a silent hole in it is worse than a short one."""
        lines = []
        fleet = self.fleet_summary()
        if fleet:
            lines.append(fleet)
        waiting = self._waiting()
        if waiting:
            lines.append("Waiting on you: "
                         + _join([r.get("label", "?") for r in waiting]) + ".")
        try:
            for _name, line in self.feeds.brief():
                lines.append(line)
        except Exception:
            lines.append("I couldn't read GitHub or your repos just now.")
        try:
            sl = connectors.get("slack")
            if sl and sl.ready():
                unread = len(self.inbox.last)
                if unread:
                    who = ", ".join(sorted({m["who"] for m
                                            in self.inbox.last.values()}))
                    lines.append(f"Slack: recent messages from {who}.")
            else:
                lines.append("Slack isn't connected.")
        except Exception:
            pass
        return self._say("\n- ".join(["Here's where everything stands:"] + lines)
                         if lines else "Nothing is running and nothing is "
                                       "waiting.")

    def _steps_from(self, body: str) -> list:
        """Turn what you said into ordered steps.

        Split on the words people actually use for sequence. A plan is written
        down before any of it runs precisely so a bad split is something you
        SEE rather than something an agent discovers halfway through."""
        text = " ".join((body or "").split())
        parts = re.split(r"\s*(?:\d+[.)]\s+|;|,\s*then\s+|\s+then\s+|,\s+and\s+"
                         r"|\.\s+(?=[A-Z])|,\s+)", text)
        return [p.strip(" .;,") for p in parts if p and len(p.strip()) > 2]

    def _make_plan(self, target: str, body: str) -> dict:
        """Write it down and show it. Nothing runs yet."""
        steps = self._steps_from(body)
        if len(steps) < 2:
            return self._say("That's one instruction rather than a plan. Say "
                             "\"tell <session> to ...\" for a single thing, or "
                             "give me two or more steps.")
        name = target or self.target
        if not name:
            names = self._names_of_sessions()
            if not names:
                return self._say("Nothing is running to plan against.")
            return self._offer(
                "Which session is this plan for? " + ", ".join(names[:8]),
                yes=lambda n=names[0], b=body: self._make_plan(n, b),
                again=lambda t, b=body: self._make_plan(t, b))
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, lambda n: self._make_plan(n, body))
        pid = plans.create(f"{len(steps)} steps", hit.get("label", name),
                           steps, sid=hit.get("sid", ""))
        if not pid:
            return self._say("I couldn't write that plan down.")
        self._plan_id = pid
        listed = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        return self._say(
            f"Here's the plan for {hit.get('label', name)}. Nothing has run "
            f"yet:\n{listed}\n\nSay \"run the plan\" and I'll do them in "
            f"order, one at a time, stopping if it asks you anything.")

    def _run_plan(self, stop: bool = False) -> dict:
        if stop:
            self.plans.stop()
            p = plans.active() or plans.latest()
            if p:
                plans.set_plan(p["id"], plans.HELD)
            return self._say("Plan paused. Say \"run the plan\" to pick it up "
                             "where it stopped.")
        p = plans.active() or plans.latest()
        if not p:
            return self._say("There's no plan yet. Say \"plan: first thing, "
                             "then second thing\" and I'll write one down.")
        if p["state"] == plans.DONE:
            return self._say(f"That plan is already finished. "
                             f"{plans.describe(p)}")
        if self.plans.running:
            return self._say("It's already running. Say \"where is the plan\" "
                             "to see how far it has got.")
        left = [s for s in p["steps"] if s["state"] in (plans.PENDING,)]
        self.plans.start(p["id"])
        return self._say(f"Running it: {len(left)} step"
                         f"{'s' if len(left) != 1 else ''} left on "
                         f"{p['target']}. I'll tell you after each one, and "
                         f"stop if it needs you.")

    def _plan_status(self) -> dict:
        p = plans.active() or plans.latest()
        if not p:
            return self._say("There's no plan.")
        return self._say(plans.describe(p))

    def _what_next(self) -> dict:
        """What to pick up, and why.

        The one place Friday offers an opinion rather than a report, and the
        front half of the conductor idea: it is not useful to be handed six
        lists, it is useful to be told which one thing to start with.

        The ranking is deliberately explainable and boring. Something waiting on
        you outranks something waiting on nobody, because your answer is the
        only thing that unblocks it. A broken build outranks a new task, because
        it is blocking everybody including you. Everything else is work, and
        work can wait its turn. No model decides this; a model would be more
        fluent and less predictable, and the whole value is that you can argue
        with the reason."""
        candidates = []

        # 1. an agent that cannot continue without you
        for r in self._waiting():
            q = (r.get("question") or r.get("permission") or "").strip()
            mins = int(max(0, time.time() - (r.get("mtime") or 0)) // 60)
            candidates.append((0, f"answer {r.get('label', 'a session')}",
                               f"it has been stuck {mins} minutes on: "
                               f"{q[:110]}" if mins else f"it is asking: {q[:110]}"))

        # 2. production, which is broken for people who are not you
        try:
            for it in feeds.SentryFeed().poll():
                candidates.append((1, "look at production", it["text"]))
                break
        except Exception:
            pass

        # 3. something of yours that is broken
        try:
            gh = connectors.get("github")
            if gh and gh.ready():
                for it in feeds.GitHubFeed().poll():
                    if "failing" in it.get("text", "") and it.get("urgency", 9) <= 1:
                        candidates.append((2, "fix the build", it["text"]))
                        break
        except Exception:
            pass

        # 4. work you have not lost yet, but could
        try:
            for it in feeds.GitFeed().poll():
                if "not pushed" in it.get("text", ""):
                    candidates.append((3, "push what you have",
                                       it["text"]))
                    break
        except Exception:
            pass

        # 5. an actual ticket
        try:
            tracker = self._tracker()
            rows = tracker.my_issues(5) if tracker else []
            rows = [r for r in rows if not r.get("error")]
            if rows:
                top = rows[0]
                title = (top.get("summary") or top.get("title") or "").strip()
                key = (top.get("key") or "").strip()
                # A ticket with no key would read as "start ." An item that
                # cannot be named is not worth offering.
                label = f"start {key}" if key else (
                    f"start on {title[:40]}" if title else "")
                if label:
                    candidates.append((4, label, title[:110] or "no description"))
        except Exception:
            pass

        if not candidates:
            return self._say("Nothing is waiting on you, nothing is broken, and "
                             "there are no tickets I can see. That is either a "
                             "good day or a disconnected one: say \"what's "
                             "connected\" to tell which.")
        candidates.sort(key=lambda c: c[0])
        pick = candidates[0]
        rest = candidates[1:3]
        # Only the first letter: capitalize() lowercases everything after it,
        # which turned "start PROJ-7" into "start proj-7". That is a key
        # somebody copies.
        head = pick[1][:1].upper() + pick[1][1:]
        out = [f"{head}. {pick[2]}"]
        if rest:
            out.append("After that: "
                       + "; ".join(f"{r[1]}" for r in rest) + ".")
        return self._say("\n".join(out))

    def _schedule(self, said: str) -> dict:
        """Put something in the calendar, from what you just said.

        The half of "Sam wants a meeting Thursday" that Friday could never
        finish. It refuses rather than guesses about the time, because a meeting
        in the wrong slot is worse than one you had to type yourself."""
        cal = None
        try:
            cal = self.feeds.sources.get("calendar", [None])[0]
        except Exception:
            cal = None
        if cal is None:
            return self._say("I don't have your calendar.")
        if not cal.available():
            return self._say("macOS hasn't allowed me to touch the calendar. "
                             "Say \"connect calendar\" and allow it.")
        stamp, reads = when.moment(said)
        if not stamp:
            return self._say("When? Tell me a day and a time, like \"Thursday "
                             "at 4\", and I'll put it in.")
        # What it is ABOUT: whoever asked, if anybody did.
        title = "Meeting"
        try:
            for _cid, m in reversed(list(self.inbox.last.items())):
                title = f"{m['who']} ({m['where']})"
                break
        except Exception:
            pass
        self.pending = {"kind": "event", "title": title, "at": stamp,
                        "reads": reads}
        return self._say(f"Put \"{title}\" in for {reads}?", needs_confirm=True)

    def _tracker(self):
        """Whichever tracker you actually use. Jira and Linear answer the same
        questions, so nothing above here needs to know which one it got."""
        # GitHub last, because it is the fallback that always works rather
        # than the tracker a team runs on. If you have Jira or Linear
        # connected, that is where your colleagues will look.
        for name in ("jira", "linear", "github"):
            c = connectors.get(name)
            try:
                if c and c.ready():
                    return c
            except Exception:
                continue
        return None

    def _file_ticket(self, project: str, summary: str) -> dict:
        """File a ticket, from what you said or from what you were just reading.

        This is the moment a Slack thread becomes work, which is the whole point
        of reading Slack in the first place. Nothing is filed without you seeing
        the exact wording, because a ticket is something colleagues read with
        your name on it."""
        ji = self._tracker()
        if ji is None:
            hint = connectors.get("jira")
            return self._say("No ticket tracker is connected. "
                             + (connectors.hint(hint) if hint else ""))
        if not summary:
            # Fall back to what you were just looking at, so "file a ticket"
            # right after reading a thread does the obvious thing.
            last = None
            for _cid, m in reversed(list(self.inbox.last.items())):
                last = m
                break
            if last:
                summary = f"{last['who']} in {last['where']}: {last['text'][:120]}"
            else:
                return self._say("What should the ticket say?")
        if not connectors.can_write() and not connectors.gh_can_write():
            return self._say(f"I'd file: \"{summary[:120]}\". I can't create "
                             f"things yet though. Say \"let yourself post\" "
                             f"and I'll be able to, still showing you every one "
                             f"first.")
        self.pending = {"kind": "ticket", "project": project,
                        "summary": summary}
        where = f" in {project}" if project else ""
        return self._say(f"File this{where}?\n\n{summary[:300]}",
                         needs_confirm=True)

    def _move_ticket(self, key: str, to: str) -> dict:
        ji = self._tracker()
        if ji is None:
            return self._say("No ticket tracker is connected, so I can't move "
                             "anything.")
        if not hasattr(ji, "move"):
            return self._say(f"I can file into {ji.name} but not move things "
                             f"there yet.")
        if not connectors.can_write() and not connectors.gh_can_write():
            return self._say(f"I can't change tickets yet. Say \"let yourself "
                             f"post\" if you want me to be able to move "
                             f"{key}.")
        self.pending = {"kind": "move", "key": key, "to": to}
        return self._say(f"Move {key} to {to}?", needs_confirm=True)

    def _allow_write(self, on: bool) -> dict:
        """Turn posting on or off. The scope is only requested when you ask.

        Off by default and revocable on its own, without tearing down the read
        connection, because those are genuinely different risks: reading your
        Slack is a privacy question, writing to it is a question about what
        arrives in a channel of your colleagues under your name."""
        connectors.allow_write(on)
        connectors.gh_allow_write(on)
        if not on:
            return self._say("Posting is off again. I'll go back to drafting "
                             "and you send.")
        sl = connectors.get("slack")
        needs_reinstall = True
        try:
            needs_reinstall = not self._slack_scope("chat:write")
        except Exception:
            pass
        extra = ""
        if needs_reinstall and sl and getattr(sl, "ready", lambda: False)():
            extra = (" Slack itself still needs the permission: say \"connect "
                     "slack\" and press Allow once more, and I'll ask for "
                     "chat:write this time.")
        return self._say("Alright. I'll still show you the exact words and "
                         "wait for a yes before anything goes out, every "
                         "time." + extra)

    def _slack_scope(self, scope: str) -> bool:
        """Whether the saved token actually carries a scope. Slack returns the
        granted list in a header, so this is a fact rather than a guess."""
        import urllib.request
        sl = connectors.get("slack")
        tok = sl.token() if hasattr(sl, "token") else ""
        if not tok:
            return False
        req = urllib.request.Request(
            "https://slack.com/api/auth.test",
            headers={"Authorization": "Bearer " + tok}, method="POST")
        with urllib.request.urlopen(req, timeout=6) as r:
            return scope in (r.headers.get("x-oauth-scopes") or "")

    def _send_draft(self) -> dict:
        """Send the thing you were just shown. Never anything else.

        There is no path from "reply to Sam saying yes" straight to a
        message in Slack. Friday writes it, you read it, and only then does
        "send it" mean anything."""
        d = getattr(self, "_last_draft", None)
        if not d:
            return self._say("There's nothing drafted. Say \"draft a reply\" "
                             "and I'll write one for you to check first.")
        if not connectors.can_write():
            return self._say("I can't post to Slack yet. Say \"let yourself "
                             "post\" if you want me to be able to, and I'll "
                             "still show you every message before it goes.")
        sl = connectors.get("slack")
        r = sl.post(d["channel"], d["text"]) if hasattr(sl, "post") else {}
        if r.get("ok"):
            self._last_draft = None
            return self._say(f"Sent to {d['where']}.")
        why = r.get("error", "")
        if why == "missing_scope":
            why = ("the token has no permission to post. Say \"connect slack\" "
                   "and press Allow once more to add it")
        return self._say(f"It didn't go: {why or 'Slack refused it'}. Nothing "
                         f"was sent.")

    def _draft(self, gist: str = "") -> dict:
        """Write the reply; you send it.

        Friday's Slack app holds read scopes only, and that is deliberate: an
        assistant that can post as you is a different risk entirely. So the
        useful version of "reply for me" is a draft in your own register that
        you paste, and saying plainly that you are the one sending it."""
        last = None
        for cid, m in reversed(list(self.inbox.last.items())):
            last = m
            break
        if not last:
            return self._say("Reply to what? Nothing has come in that I know "
                             "of.")
        if not (engine.AVAILABLE and engine.brain.up()):
            return self._say(f"My brain isn't loaded, so I can't write it. "
                             f"{last['who']} said: {last['text'][:200]}")
        want = (f"\nThe reply should say: {gist}" if gist else "")
        out = ""
        try:
            out = engine.brain._chat(
                [{"role": "system", "content":
                  "Write a short Slack reply for Krish to send. Two or three "
                  "sentences at most, plain and direct, no greeting padding, no "
                  "sign-off, no markdown. Only commit to things the instruction "
                  "says; never invent a date, a number or a promise."},
                 {"role": "user", "content":
                  f"{last['who']} wrote in {last['where']}:\n"
                  f"{last['text'][:1200]}{want}"}],
                timeout=engine.brain.TIMEOUT_SLOW, max_tokens=180)
            out = engine.brain._clean(out) if out else ""
        except Exception:
            out = ""
        if not out:
            return self._say("I couldn't get a draft out of my brain. "
                             f"{last['who']} said: {last['text'][:200]}")
        self._last_draft = {"text": out, "where": last["where"],
                            "channel": last.get("channel", ""),
                            "who": last["who"]}
        if connectors.can_write() and last.get("channel"):
            return self._say(f"Here's a draft for {last['who']} in "
                             f"{last['where']}:\n\n{out}\n\nSay \"send it\" "
                             f"and it goes as written. I won't change a word.")
        return self._say(f"Here's a draft for {last['who']} in {last['where']}. "
                         f"I can't post to Slack, so send it yourself:\n\n{out}")

    def _missed(self) -> dict:
        """Everything Friday brought up since you last said something.

        Coming back to the desk and scrolling to work out what happened is the
        same manual sweep as walking the windows, which is the job."""
        # Skip the question itself: handle() records it before dispatching, so
        # measuring from "the last thing you said" measures from now, and the
        # answer is always "nothing".
        since = 0
        for h in reversed(self.history[:-1]):
            if h.get("role") == "user":
                since = h.get("ts", 0)
                break
        news = [h for h in self.history
                if h.get("kind") == "proactive" and h.get("ts", 0) > since]
        # Everything held back because Friday was over budget or you had asked
        # for quiet. Without this the offer above ("say what did I miss") points
        # at a list that was already thrown away, which is a promise the code
        # could not keep.
        held = []
        try:
            held = [text for _when, text, _src in self.budget.held(since=since)]
        except Exception:
            held = []
        if held:
            news = news + [{"text": t} for t in held]
        if not news:
            waiting = self._waiting()
            if waiting:
                return self._say("Nothing new was said, but "
                                 + _join([r["label"] for r in waiting])
                                 + f" {_is(len(waiting))} waiting on you.")
            return self._say("Nothing since you last spoke.")
        # Newest last, so it reads in the order it happened.
        lines = [h["text"] for h in news][-8:]
        head = (f"{len(news)} things happened:" if len(news) > 1
                else "One thing happened:")
        extra = ""
        if len(news) > 8:
            extra = f" ({len(news) - 8} older ones before these)"
        return self._say(head + extra + "\n- " + "\n- ".join(lines))

    def _waiting(self) -> list:
        try:
            return [r for r in fleetcache.snapshot().values()
                    if (r.get("question") or r.get("permission"))]
        except Exception:
            return []

    def _stuck(self) -> dict:
        """Who cannot continue without you, and for how long.

        "Working" and "stuck" look identical from outside a terminal, and the
        difference is whether your day is blocking somebody."""
        rows = self._waiting()
        if not rows:
            try:
                n = len(fleetcache.snapshot())
            except Exception:
                n = 0
            return self._say("Nobody is waiting on you."
                             + (f" All {n} are working." if n else ""))
        bits = []
        for r in rows:
            q = (r.get("question") or r.get("permission") or "").strip()
            waited = ""
            since = r.get("mtime") or 0
            if since:
                mins = int(max(0, time.time() - since) // 60)
                if mins >= 1:
                    waited = f" (for {mins} minute{'s' if mins != 1 else ''})"
            bits.append(f"{r.get('label', '?')}{waited}: {q[:120]}")
        self.target = rows[0].get("label", "") or self.target
        return self._say("Waiting on you:\n- " + "\n- ".join(bits))

    def _mute(self, name: str, on: bool) -> dict:
        """Stop reporting one session without going quiet altogether.

        One agent in a loop should not cost you the whole feature: the choice
        between hearing everything and hearing nothing is how you end up
        hearing nothing."""
        names = self._names_of_sessions()
        target = nearest.pick(name, names) if names else ""
        if not target:
            return self._no_session(name, lambda n: self._mute(n, on))
        sid = next((r["sid"] for r in fleetcache.snapshot().values()
                    if (r.get("label") or "") == target), "")
        if not sid:
            return self._no_session(name, lambda n: self._mute(n, on))
        self.watch.mute(sid, on)
        return self._say(f"I won't mention {target} again until you say "
                         f"\"unmute {target}\"." if on else
                         f"Telling you about {target} again.")

    def _who(self) -> dict:
        """Who a bare reply would reach. Guessing wrong sends your words to the
        wrong agent, so this has to be askable."""
        if not self.target:
            names = self._names_of_sessions()
            return self._say("Nobody in particular yet. " +
                             ("Running: " + ", ".join(names[:8]) if names
                              else "Nothing is running."))
        return self._say(f"{self.target}. Anything you say without naming a "
                         f"session goes there.")

    def _more(self) -> dict:
        """The agent's own words, unsummarised.

        A summary is only useful if you can check it, and the one time you need
        the exact wording is the one time a summary has dropped the detail that
        mattered."""
        if not self.target:
            return self._say("More about what? Nobody has said anything yet.")
        sid = ""
        try:
            sid = next((r["sid"] for r in fleetcache.snapshot().values()
                        if (r.get("label") or "") == self.target), "")
        except Exception:
            sid = ""
        full = self.watch.last.get(sid, "")
        if not full:
            return self._say(f"I don't have {self.target}'s exact words. Say "
                             f"\"open {self.target}\" and I'll bring the window "
                             f"up.")
        return self._say(f"{self.target}, in full:\n{full}")

    def _ask_all(self, tail: str, joiner: str = "") -> dict:
        """Put one question to every running session and collect the answers.

        Doing this by hand means typing the same thing into five windows and
        then going back to five windows to read the replies. This is the part
        that is actually conducting rather than relaying."""
        rows = []
        try:
            rows = [r for r in fleetcache.snapshot().values()
                    if r.get("sid")]
        except Exception:
            rows = []
        if not rows:
            return self._say("Nothing is running, so there is nobody to ask.")
        question = self._as_question(tail, joiner)
        asked, marks = [], {}
        for r in rows:
            path = r.get("path", "")
            marks[r["sid"]] = replies.mark(path) if path else ""
            if actions.send_to_session(r["sid"], question):
                asked.append(r)
        if not asked:
            return self._say("I couldn't reach any of them.")
        if self.watch.running:
            for r in asked:
                self.watch.expect(r["sid"])
        else:
            self._collect(asked, marks)
        names = ", ".join(r.get("label", "?") for r in asked)
        missed = [r.get("label", "?") for r in rows if r not in asked]
        note = (" I couldn't reach " + ", ".join(missed) + ".") if missed else ""
        return self._say(f"Asked {len(asked)}: {names}. I'll report back as "
                         f"they answer.{note}")

    def _collect(self, rows: list, marks: dict) -> None:
        """Report each answer as it lands, rather than after the slowest one.

        Waiting for all of them means one stuck agent hides four good answers,
        and you cannot tell the difference between thinking and broken."""
        import threading

        def _one(r):
            label = r.get("label", "?")
            path = r.get("path", "")
            if not path:
                self.announce(f"{label}: I can't read its transcript, so I "
                              f"can't tell you what it said.")
                return
            said = ""
            try:
                said = replies.wait_for_reply(path, marks.get(r["sid"], ""))
            except Exception:
                said = ""
            self.announce(f"{label} says: " + said[:500] if said else
                          f"{label} hasn't answered.")

        for r in rows:
            threading.Thread(target=_one, args=(r,), daemon=True).start()

    def _watch(self, said: str) -> dict:
        """"Tell me when it's done." A standing request, answered later.

        Without this the only way to know an agent finished is to keep asking,
        which is the polling Friday exists to replace."""
        names = self._names_of_sessions()
        target = nearest.best_window(said, names) if names else ""
        if not target and self.target:
            target = self.target          # "tell me when it's done"
        if not target:
            if not names:
                return self._say("Nothing is running to wait for.")
            return self._offer(
                "Which one should I watch? " + ", ".join(names[:8]),
                yes=lambda: self._watch(names[0]),
                again=lambda t: self._watch(t))
        row = next((r for r in fleetcache.snapshot().values()
                    if (r.get("label") or "") == target), None)
        if not row:
            return self._no_session(target, self._watch)
        self.target = target
        self._watch_until_idle(row["sid"], target)
        return self._say(f"Watching {target}. I'll tell you when it stops.")

    def _watch_until_idle(self, sid: str, label: str,
                          timeout: float = 3600) -> None:
        import threading

        def _wait():
            end = time.time() + timeout
            was_working = True
            while time.time() < end:
                time.sleep(3)
                try:
                    row = fleetcache.snapshot().get(sid)
                except Exception:
                    row = None
                if row is None:
                    self.announce(f"{label} has closed.")
                    return
                status = row.get("status", "")
                if status == "working":
                    was_working = True
                    continue
                if was_working:
                    q = (row.get("question") or "").strip()
                    self.announce(f"{label} is waiting on you: {q}" if q
                                  else f"{label} has finished.")
                    return
            self.announce(f"{label} is still going after an hour, so I've "
                          f"stopped watching it.")
        threading.Thread(target=_wait, daemon=True).start()

    def _stop(self, name: str, said: str = "") -> dict:
        """Stop what an agent is doing. The same Escape you would press."""
        names = self._names_of_sessions()
        # Pronoun first. "stop it" must mean the session we were just talking
        # to; matched by sound it means whichever name shares a letter with
        # "it", which was api.
        low = (name or "").strip().lower()
        if low in _STANDS_FOR_SESSION or _ITS_RE.match(low):
            if not self.target:
                return self._say("Stop which one? " + (", ".join(names[:8])
                                                       if names else
                                                       "nothing is running."))
            target = self.target
        else:
            target = (nearest.best_window(said, names) if said and names
                      else "") or (nearest.pick(name, names) if names else "")
        if not target:
            return self._no_session(name, lambda n: self._stop(n, n))
        row = next((r for r in fleetcache.snapshot().values()
                    if (r.get("label") or "") == target), None)
        if not row:
            return self._no_session(target, lambda n: self._stop(n, n))
        ok = actions.interrupt_session(row["sid"])
        self.target = target
        return self._say(f"Stopped {target}." if ok else
                         f"I couldn't reach {target} to stop it.")

    def _open_found(self) -> dict:
        """'open that one' after a search. If the session is running we raise
        its window; if it is closed we RESUME it, which is the whole point of
        remembering it in the first place."""
        if not self._last_found:
            return self._say("I haven't found a session for you to open yet.")
        hit = self._last_found[0]
        sid = hit["sid"]
        live = {}
        try:
            live = {r["sid"]: r for r in fleetcache.snapshot().values()}
        except Exception:
            pass
        if sid in live:
            ok = actions.focus_session(sid)
            return self._say("Opened it." if ok else
                             "I couldn't bring that window to the front.")
        ok = actions.resume_session(sid, cwd=_cwd_for(hit.get("path", "")))
        return self._say("Resumed it in a new window." if ok else
                         "I couldn't resume that session.")

    def _new_session(self, about: str) -> dict:
        """Start fresh work, handing the new session its purpose so you do not
        have to retype what you just told me."""
        opening = about.strip()
        if not opening and self._last_slack:
            # straight from what was just read out of Slack
            opening = ("Context from a Slack thread:\n"
                       + "\n".join(f"- {m['who']}: {m['text']}"
                                    for m in self._last_slack[:6]))
        self.pending = {"kind": "new", "about": opening}
        preview = (opening[:90] + "…") if len(opening) > 90 else opening
        return self._say(
            (f'Start a new session on "{preview}"?' if preview
             else "Start a new empty session?"), needs_confirm=True)

    # ---- connectors: Friday's own eyes on your tools ---------------------
    def _connect(self, which: str, token: str) -> dict:
        which = (which or "").strip().lower()   # "Friday Connect Slack." -> slack
        # no argument: report what is connected and what is not
        if not which:
            rows = connectors.status()
            live = [n for n, v in rows.items() if v["ready"]]
            dead = [n for n, v in rows.items() if not v["ready"]]
            bits = []
            if live:
                bits.append("Connected: " + ", ".join(sorted(live)) + ".")
            if dead:
                bits.append("Not connected: " + ", ".join(sorted(dead))
                            + '. Say "connect <name>" and I\'ll walk you through it.')
            return self._say(" ".join(bits) or "Nothing is connected yet.")
        # no token: try the browser flow (MCP), which is the one-time approval
        if not token:
            # If Friday already built your Slack app, the only thing left is the
            # click, so "connect slack" should just re-open that page. Asking
            # for a fresh configuration token to retry a click you missed is
            # punishing you for reading slowly.
            if which == "slack" and connectors.can_resume():
                def _again():
                    r = connectors.resume_setup()
                    if r.get("ok"):
                        who = r.get("who") or ""
                        self.announce("Slack is connected"
                                      + (f", I can see you as {who}." if who
                                         else "."))
                    else:
                        self.announce("Slack setup stopped: "
                                      + r.get("error", "") + ".")
                import threading
                threading.Thread(target=_again, daemon=True).start()
                return self._say("Your Slack app is already built, so all "
                                 "that's left is the click. Opening that page "
                                 "now, press Allow. No rush.")
            from . import mcp as _mcp
            cfg = _mcp.servers().get(which)
            # Only open a browser if the flow can actually succeed. Otherwise
            # fall through to the connector's own instructions, which for Slack
            # is a pre-filled app link rather than a scope checklist.
            if cfg and _mcp.can_authorize(cfg["url"]):
                def _flow():
                    r = _mcp.authorize(which, cfg["url"])
                    self.announce(f"{which} connected." if r.get("ok") else
                                  f"{which} didn't connect: {r.get('error')}")
                import threading
                threading.Thread(target=_flow, daemon=True).start()
                return self._say(f"Opening your browser to approve {which}. "
                                 "I'll tell you when it's done.")
            # The MCP wrapper's hint is generic. If we have a hand-written
            # connector for the same service, ITS instructions are the useful
            # ones (Slack's is a pre-filled app link, not a scope checklist).
            if which == "gmail":
                return self._say(connectors.gmail_setup_steps())
            if which == "calendar":
                return self._connect("calendar", "x")
            builtin = connectors.REGISTRY.get(which)
            c0 = builtin or connectors.get(which)
            if c0:
                return self._say(connectors.hint(c0))
            guess = nearest.suggest(which, list(connectors.all_connectors()))
            if guess:
                return self._offer(
                    f"I don't have a connector called {which}. Did you mean "
                    f"{guess}?",
                    yes=lambda g=guess: self._connect(g, ""),
                    again=lambda t: self._connect(t.strip().lower(), ""))
            return self._say(f"I don't know a connector called {which}. I have: "
                             + ", ".join(sorted(connectors.all_connectors())))
        # Gmail: "gmail <client-id> <client-secret>" starts the browser flow.
        if which == "gmail" and token and " " in token.strip():
            cid, _, csec = token.strip().partition(" ")

            def _google():
                r = connectors.gmail_connect(cid.strip(), csec.strip())
                self.announce("Gmail is connected. Try: any new email?"
                              if r.get("ok") else
                              "Gmail setup stopped: " + r.get("error", "") + ".")
            import threading
            threading.Thread(target=_google, daemon=True).start()
            return self._say("Opening Google now. Press Allow and I'll keep it "
                             "connected from then on; the token refreshes "
                             "itself.")
        if which == "calendar":
            r = connectors.calendar_prompt()
            if r.get("ok"):
                return self._say(f"Calendar is readable, {r['calendars']} "
                                 f"calendars. I'll warn you before meetings.")
            return self._say("macOS wouldn't give me the calendar. If no dialog "
                             "appeared, it was refused before: System Settings, "
                             "Privacy & Security, Automation, and switch on "
                             "Calendar for your terminal.")
        c = connectors.get(which)
        if not c:
            guess = nearest.suggest(which, list(connectors.all_connectors()))
            if guess:
                return self._offer(
                    f"I don't have a {which} connector. Did you mean {guess}?",
                    yes=lambda g=guess, tk=token: self._connect(g, tk))
            return self._say(f"I don't have a {which} connector. I have: "
                             + ", ".join(sorted(connectors.all_connectors())))
        # A Slack App Configuration Token is not a credential to store, it is
        # permission to BUILD. Given one, do the six manual screens (create the
        # app, add ten scopes, install, copy the token) automatically, and leave
        # one click for you. Storing it instead was the dead end: it passes
        # auth.test and can read nothing.
        if which == "slack" and connectors.is_config_token(token):
            def _build():
                r = connectors.setup_from_config_token(token)
                if r.get("ok"):
                    who = r.get("who") or ""
                    self.announce("Slack is connected"
                                  + (f", I can see you as {who}." if who else ".")
                                  + " Try: go to my <channel> group in slack "
                                    "and read the chat.")
                else:
                    self.announce("Slack setup stopped: " + r.get("error", "") + ".")
            import threading
            threading.Thread(target=_build, daemon=True).start()
            return self._say("That's a configuration token, which is even "
                             "better: I'll build the Slack app myself with the "
                             "right permissions. A browser tab is opening now. "
                             "Press Allow and you're done.")
        if not connectors.save_secret(f"{which}_token", token):
            return self._say(f"I couldn't save the {which} token.")
        # Verify against the BUILT-IN connector (the token path), not the MCP
        # wrapper, which looks for a token in a different place.
        c = connectors.REGISTRY.get(which) or c
        ok = False
        try:
            ok = c.ready()
        except Exception:
            ok = False
        if not ok:
            # Say WHICH thing is wrong. "isn't answering" sent you round the
            # same loop three times with no way to tell what to change.
            why = ""
            try:
                if hasattr(c, "token_problem"):
                    why = c.token_problem()
            except Exception:
                why = ""
            if why:
                return self._say(f"Saved it, but {why}.")
            return self._say(f"Saved it, but {which} isn't answering. The token "
                             f"may be missing a scope.")
        who = ""
        try:
            who = c.whoami() if hasattr(c, "whoami") else ""
        except Exception:
            pass
        extra = f" I can see you as {who}." if who else ""
        return self._say(f"{which} connected.{extra} Try: {_FIRST_TRY.get(which, 'ask me about it')}.")

    def _github(self, query: str) -> dict:
        gh = connectors.get("github")
        if not gh.ready():
            return self._say("GitHub isn't connected: " + connectors.hint(gh))
        if query:
            rows = gh.search(query, limit=4)
            if not rows:
                return self._say(f"Nothing on GitHub for {query}.")
            lines = [f"{r.get('repository', {}).get('nameWithOwner', '')}: "
                     f"{r.get('title', '')[:80]} ({r.get('state', '')})"
                     for r in rows]
            return self._say("On GitHub:\n- " + "\n- ".join(lines))
        # no query: what actually wants your attention
        notes, prs = gh.notifications(6), gh.my_prs(4)
        if not notes and not prs:
            return self._say("Nothing waiting on you on GitHub.")
        bits = []
        if prs:
            bits.append("Open pull requests:\n- " + "\n- ".join(
                f"{p.get('repository', {}).get('nameWithOwner', '')}: {p.get('title', '')[:70]}"
                for p in prs))
        if notes:
            bits.append("Notifications:\n- " + "\n- ".join(
                f"{n.get('repo', '')}: {(n.get('title') or '')[:70]} [{n.get('reason', '')}]"
                for n in notes))
        return self._say("\n\n".join(bits))

    def _read_channel(self, name: str, said: str = "") -> dict:
        # Look for a real channel name anywhere in what was actually said,
        # before falling back to whatever the grammar suggested.
        if said:
            sl = connectors.get("slack")
            if sl.ready() and hasattr(sl, "channel_names"):
                found = nearest.best_window(said, sl.channel_names(40))
                if found:
                    return self._read_channel_named(found, said)
        if not (name or "").strip():
            sl = connectors.get("slack")
            if not sl.ready():
                return self._say("Slack isn't connected yet. " + connectors.hint(sl))
            names = (sl.channel_names(40) if hasattr(sl, "channel_names") else [])
            if names:
                return self._offer(
                    "Which one? I can see: " + ", ".join("#" + n for n in names[:8]),
                    yes=lambda: self._read_channel(names[0]),
                    again=lambda t: self._read_channel(t))
            return self._say("Which channel?")
        return self._read_channel_named(name, said)

    def _read_channel_named(self, name: str, said: str = "") -> dict:
        """Read an actual channel and tell you what is being asked.

        This is the front of the chain: read the thread, then 'did we ever talk
        about this?' searches your sessions using what was just read, so you
        never have to retype the subject."""
        sl = connectors.get("slack")
        if not sl.ready():
            return self._say("Slack isn't connected yet. " + connectors.hint(sl))
        err = (lambda: sl.last_error() if hasattr(sl, "last_error") else "")
        ch = sl.find_channel(name)
        if not ch:
            # Distinguish "no such channel" from "Slack wouldn't give me the
            # list", which look identical from here but need different fixes.
            why = err()
            if why:
                return self._say("I couldn't look at your channels: " + why + ".")
            # Friday HAS the list, so "I can't find it" on its own is withholding
            # the answer. Saying the real names turns a dead end into a choice,
            # which is what three rounds of 'Munsheer' actually needed.
            names = sl.channel_names(40) if hasattr(sl, "channel_names") else []
            guess = nearest.suggest(name, names)
            if guess:
                return self._offer(
                    f"I don't have a channel called {name}. Did you mean "
                    f"#{guess}?",
                    yes=lambda g=guess, q=said: self._read_channel_named(g, q),
                    again=lambda t: self._read_channel(t))
            if names:
                return self._say(f"I don't have a channel called {name}. What I "
                                 f"can see: " + ", ".join("#" + n for n in names[:8]))
            return self._say(f"I can't find a Slack channel called {name}.")
        # A named day is a real bound, not a hint. Reading the last fifteen
        # messages whatever their date and then answering as though they were
        # yesterday's is the worst kind of wrong: the answer's shape says the
        # question was understood.
        oldest, latest, span = when.parse(said)
        rows = sl.read_channel(ch["id"], limit=(200 if span else 15),
                              oldest=oldest, latest=latest)
        if not rows:
            why = err()
            if why:
                return self._say(f"I couldn't read #{ch['name']}: " + why + ".")
            if span:
                return self._say(f"Nothing was said in #{ch['name']} {span}.")
            return self._say(f"#{ch['name']} is empty.")
        self._last_slack = rows
        convo = "\n".join(f"{r['who']}: {r['text']}" for r in rows[-12:])
        summary = self._summarise_thread(convo, said, span)
        # Say WHICH messages were read. Asked what was discussed yesterday and
        # given a summary with no timeframe, you cannot tell whether Friday
        # honoured "yesterday" or quietly ignored it.
        # Say exactly what was read, so the window and the claim can never
        # disagree.
        stamps = [r.get("when") or 0 for r in rows if r.get("when")]
        if span:
            head = f" ({span}, {len(rows)} message{'s' if len(rows) != 1 else ''})"
        elif stamps:
            head = (f" (last {len(rows)} messages, "
                    f"{memory.ago(min(stamps))} to {memory.ago(max(stamps))})")
        else:
            head = ""
        return self._say(f"In #{ch['name']}{head}:\n{summary}")

    def _summarise_thread(self, convo: str, question: str = "",
                          span: str = "") -> str:
        """What is actually being ASKED, in a sentence or two.

        If you asked something specific ("what did Sam say"), answer THAT
        from the thread. Returning the same general summary whatever was asked
        is a way of not listening."""
        if not (engine.AVAILABLE and engine.brain.up()):
            return convo[:600]
        # Names, never he or she: these are real colleagues and the messages do
        # not say anyone's pronouns, so guessing gets it wrong about a person.
        RULES = (" Refer to people by name, and never use he, she, his or her."
                 " Two or three short sentences, no lists."
                 # This summarises real colleagues by name, so an invented
                 # attribution is somebody being told they said something they
                 # did not. Prevention first, then the same grounding check the
                 # agent summaries get.
                 " Every name, file, number and date you write must appear in"
                 " the messages themselves. Do not add a person, a commitment,"
                 " a date or a number the messages do not state.")
        task = ("Summarise this chat for someone who has not read it. Say who is "
                "asking what, and what they need. Only use what is in the "
                "messages." + RULES)
        q = (question or "").strip()
        if span:
            # These messages ARE the window asked for, so it must not hedge
            # about whether they cover it. It used to answer "there is no
            # information about yesterday" while holding yesterday's messages.
            task = (f"These are the messages from {span}. Summarise what was "
                    f"discussed and who wants what. Do not say you lack "
                    f"information about {span}: these messages are exactly "
                    f"that period." + RULES)
        elif q and len(q.split()) > 2:
            task = ("Answer this question using ONLY these messages: " + q[:160]
                    + "\nAnswer it directly. If the messages genuinely do not "
                      "cover it, say only that and summarise what they do say. "
                      "Never do both: do not open by denying something you then "
                      "answer." + RULES)
        out = engine.brain._chat(
            [{"role": "system", "content": task},
             {"role": "user", "content": convo[:4000]}],
            timeout=engine.brain.TIMEOUT_SLOW, max_tokens=180)
        out = engine.brain._clean(out) if out else ""
        if out:
            # The same grounding as an agent summary. This one had none at all,
            # and it is the one that puts words in a colleague's mouth.
            from . import watchtower
            bad = watchtower._invented(out, convo)
            if bad:
                engine.log(f"friday: unsupported specific in a thread "
                           f"summary ({bad})")
                out = watchtower._drop_invented(out, convo)
        return out or convo[:600]

    def _engine(self) -> dict:
        """What is actually doing the work, stated plainly.

        Asked this, a model with no facts will say whatever sounds reassuring.
        The real answer matters: it decides whether reading your Slack means
        sending it to somebody else's server."""
        bits = []
        if engine.AVAILABLE:
            name = (Path(getattr(engine.brain, "MODEL_PATH", "")).stem
                    or "a local model")
            up = False
            try:
                up = engine.brain.up()
            except Exception:
                pass
            bits.append(f"No, not Claude. I think with {name}, which runs on "
                        f"this Mac" + ("" if up else " (not loaded yet)"))
            bits.append("speech in and out is local too, whisper and Kokoro")
        else:
            bits.append("No, not Claude. My local brain isn't available right "
                         "now, so I'm only doing the parts that need no model")
        bits.append("what I read from Slack or GitHub stays here; nothing is "
                    "sent to Anthropic or anyone else")
        return self._say(". ".join(b[0].upper() + b[1:] if b else b
                                   for b in bits) + ".")

    def _did_we_discuss(self) -> dict:
        """Search your past sessions using whatever we were just talking about,
        so the chain flows without you restating the subject."""
        seed = ""
        if self._last_slack:
            seed = " ".join(r["text"] for r in self._last_slack[-6:])
        if not seed:
            for m in reversed(self.history[:-1]):
                if m["role"] == "friday" and len(m["text"]) > 40:
                    seed = m["text"]
                    break
        if not seed:
            return self._say("About what? Point me at something first.")
        terms = self._key_terms(seed)
        hits = memory.search(terms, limit=3)
        if not hits:
            self._last_found = []
            return self._say(f"I searched your sessions for {terms} and found "
                             "nothing. Want me to start a new one on it?")
        self._last_found = hits
        live = set()
        try:
            live = set(fleetcache.snapshot())
        except Exception:
            pass
        h = hits[0]
        where = " (running now)" if h["sid"] in live else ""
        hit_on = ", ".join(h.get("matched") or [])
        # A match on two common words out of five is a coincidence, not a
        # memory. Saying a flat "Yes" to it is the bluffing failure: the answer
        # sounds certain and the session it names has nothing to do with the
        # subject. Claim it only when most of the distinctive terms are there.
        need = max(2, (h.get("terms") or 1) // 2 + 1)
        strong = bool(h.get("phrase")) or len(h.get("matched") or []) >= need
        if not strong:
            self._last_found = hits
            return self._say(
                f"Probably not. I searched for {terms}, and the closest is "
                f"{memory.ago(h['when'])}{where}, but it only matches on "
                f"{hit_on}: {(h.get('about') or '')[:90]}\n\nSay \"open that "
                f"one\" if it is the one, or I can start a new session on it.")
        return self._say(
            f"Yes. {memory.ago(h['when'])}{where}, matching {hit_on}: "
            f"{(h.get('about') or '')[:110]}"
            "\n\nSay \"open that one\" and I'll bring it up.")

    def _key_terms(self, text: str) -> str:
        """The few words worth searching for, so a whole thread does not become
        a query full of 'the' and 'please'."""
        if engine.AVAILABLE and engine.brain.up():
            out = engine.brain._chat(
                [{"role": "system", "content":
                  "Pick the 2-5 most distinctive search keywords from this "
                  "text: proper nouns, technical terms, product names. "
                  "Lowercase, space separated, nothing else."},
                 {"role": "user", "content": text[:1500]}],
                timeout=6.0, max_tokens=24)
            out = " ".join((out or "").split())[:80]
            if out:
                return out
        return " ".join(text.split()[:8])

    def _issues(self) -> dict:
        gh = connectors.get("github")
        if not gh.ready():
            return self._say("GitHub isn't connected: " + connectors.hint(gh))
        rows = gh.my_issues(8)
        if not rows:
            return self._say("No open issues involving you.")
        lines = [f"{r.get('repository', {}).get('nameWithOwner', '')}: "
                 f"{r.get('title', '')[:80]}" for r in rows]
        return self._say(f"{len(rows)} open issue"
                         f"{'s' if len(rows) != 1 else ''}:\n- "
                         + "\n- ".join(lines))

    def _broken(self) -> dict:
        """What is actually broken right now, deduplicated.

        A notification list says 49 things happened; this says four things are
        wrong. Repeats are counted, not listed, because ten failures of the
        same nightly job is one problem you have not looked at."""
        gh = connectors.get("github")
        if not gh or not gh.ready():
            return self._say("GitHub isn't connected, so I can't see your builds.")
        rows = gh.failing(6)
        if not rows:
            return self._say("Nothing's failing on GitHub.")
        lines = []
        for r in rows:
            times = f" ({r['count']} times)" if r.get("count", 1) > 1 else ""
            lines.append(f"{r['repo']}: {r['workflow']}{times}")
        n = len(lines)
        return self._say(f"{n} thing{'s' if n != 1 else ''} failing:\n- "
                         + "\n- ".join(lines))

    def _activity(self) -> dict:
        gh = connectors.get("github")
        if not gh or not gh.ready():
            return self._say("GitHub isn't connected.")
        rows = gh.activity(6)
        if not rows:
            return self._say("No recent GitHub activity.")
        pretty = {"PushEvent": "pushed to", "IssuesEvent": "worked on issues in",
                  "PullRequestEvent": "opened a PR in", "CreateEvent": "created",
                  "DeleteEvent": "deleted in", "WatchEvent": "starred"}
        lines = [f"{pretty.get(r.get('type'), r.get('type', ''))} {r.get('repo', '')}"
                 for r in rows]
        return self._say("Lately you:\n- " + "\n- ".join(lines))

    def _mail(self, query: str) -> dict:
        gm = connectors.get("gmail")
        if not gm.ready():
            return self._say("Gmail isn't connected yet. " + connectors.hint(gm))
        rows = gm.search(query, limit=5)
        if not rows:
            return self._say("Nothing matching in your mail.")
        lines = [f"{r['from'][:40]}: {r['subject'][:90]}" for r in rows]
        return self._say("Mail:\n- " + "\n- ".join(lines))

    def _fire(self) -> dict:
        """What production is doing, asked directly.

        Wider than the feed on purpose. The feed only ever volunteers things it
        has never seen, because an alert about a month-old error is noise. When
        you ASK, the month-old error is exactly what you want, so this reports
        the state rather than the news."""
        se = connectors.get("sentry")
        if not se or not hasattr(se, "issues"):
            return self._say("I don't have Sentry connected. " + (
                connectors.hint(se) if se else ""))
        if not se.ready():
            return self._say("Sentry isn't connected yet. " + connectors.hint(se))
        rows = se.issues(limit=8)
        if rows and rows[0].get("error"):
            return self._say("Sentry answered with an error: " + rows[0]["error"])
        if not rows:
            return self._say("Nothing unresolved in Sentry. Production is quiet.")
        # Sorted by people affected, because 4000 events hitting one bot matters
        # less than 40 hitting 40 people, and the count alone hides that.
        rows.sort(key=lambda r: (-r.get("users", 0), -r.get("count", 0)))
        lines = []
        for r in rows[:5]:
            who = (f", {r['users']} people" if r.get("users") else "")
            mark = "" if r.get("unhandled") else " (handled)"
            lines.append(f"{r['title'][:70]}{mark}: {r['count']} times{who}")
        head = f"{len(rows)} unresolved in Sentry"
        worst = rows[0]
        if worst.get("users", 0) >= 10:
            head = f"{worst['users']} people are hitting the top one"
        return self._say(head + ":\n- " + "\n- ".join(lines))

    def _jira(self) -> dict:
        ji = connectors.get("jira")
        if not ji.ready():
            return self._say("Jira isn't connected yet. " + connectors.hint(ji))
        rows = ji.my_issues(8)
        if rows and rows[0].get("error"):
            return self._say("Jira answered with an error: " + rows[0]["error"])
        if not rows:
            return self._say("No open Jira tickets assigned to you.")
        lines = [f"{r['key']} [{r['status']}]: {r['summary'][:80]}" for r in rows]
        return self._say("Jira:\n- " + "\n- ".join(lines))

    def _slack(self, query: str) -> dict:
        sl = connectors.get("slack")
        if not sl.ready():
            return self._say("Slack isn't connected yet. To fix that: "
                             + connectors.hint(sl))
        if not query:
            return self._say("What should I look for in Slack?")
        rows = sl.search(query, limit=4)
        if not rows:
            # An empty list can mean "no messages" or "Slack refused". Saying
            # "nothing found" for a refusal is the bug that made this feel
            # broken with no way to tell what to fix.
            why = sl.last_error() if hasattr(sl, "last_error") else ""
            if why:
                return self._say("I couldn't search Slack: " + why + ".")
            return self._say(f"Nothing in Slack about {query}.")
        lines = [f"#{r['channel']} · {r['who']} · {connectors.when(r['when'])}: "
                 f"{r['text'][:120]}" for r in rows]
        self._last_slack = rows
        return self._say("In Slack:\n- " + "\n- ".join(lines))

    def _find_past(self, query: str) -> dict:
        """Search everything you have ever done, not just what is running."""
        hits = memory.search(query, limit=4)
        if not hits:
            return self._say(f"I couldn't find anything about {query}.")
        live = {}
        try:
            live = {r["sid"]: r for r in fleetcache.snapshot().values()}
        except Exception:
            pass
        lines = []
        for h in hits:
            mark = " (running now)" if h["sid"] in live else ""
            about = (h.get("about") or h.get("snippet") or "").strip()
            lines.append(f"{memory.ago(h['when'])}{mark}: {about[:100]}")
        self._last_found = hits
        head = ("Found one:" if len(lines) == 1 else f"Found {len(lines)}:")
        tail = ("\n\nSay \"open that one\" to bring it up."
                if hits[0]["sid"] in live else
                "\n\nThe top one isn't running, so I can't open it, only tell you about it.")
        return self._say(head + "\n- " + "\n- ".join(lines) + tail)

    def _recent_work(self) -> dict:
        rows = memory.recent(limit=5)
        if not rows:
            return self._say("I can't see any recent sessions.")
        lines = [f"{memory.ago(r['when'])}: {(r['about'] or 'no description')[:90]}"
                 for r in rows]
        return self._say("Recently:\n- " + "\n- ".join(lines))

    def _other_users(self) -> dict:
        """Honest about what is on the machine, and about the wall."""
        try:
            others = engine.fleet.other_users()
        except Exception:
            others = {}
        if not others:
            return self._say("No one else has Claude running on this Mac.")
        bits = [f"{n} session{'s' if n != 1 else ''} under {u}"
                for u, n in others.items()]
        return self._say(_join(bits) + ". I can see they're running, but not "
                         "what they're doing: another account's work isn't "
                         "readable from here, and shouldn't be.")

    def _no_session(self, name: str, retry, message: str = "") -> dict:
        """A name that is not running. That is not the same as unknown.

        A project you worked in last week is still reachable: its transcripts are
        on disk and `claude --resume` brings the conversation back. Answering "I
        don't have a session called promptguard" while holding every
        conversation you ever had with it is withholding the answer."""
        past = []
        try:
            past = memory.by_project(name, limit=1)
        except Exception:
            past = []
        if past:
            hit = past[0]
            if message:
                return self._offer(
                    f"{hit['project']} isn't running, but I have it from "
                    f"{memory.ago(hit['when'])}. Want me to reopen it and send "
                    f"that?",
                    yes=lambda h=hit, m=message: self._resume_and_tell(h, m))
            return self._offer(
                f"{hit['project']} isn't running. I have it from "
                f"{memory.ago(hit['when'])}. Reopen it?",
                yes=lambda h=hit: self._resume_only(h))
        # A project with a directory and no conversations is a specific fact,
        # and a different one from "no such project".
        try:
            empty = nearest.pick(name, memory.project_names())
            alive = memory.project_names(with_sessions=True)
        except Exception:
            empty, alive = "", []
        if empty and empty not in alive:
            near = nearest.suggest(name, alive)
            if near:
                return self._offer(
                    f"There's a {empty} folder but no conversations in it. Did "
                    f"you mean {near}?",
                    yes=lambda g=near: retry(g))
            # Nothing sounds like it, so naming what DOES have history beats
            # guessing at something that does not.
            tail = (" I have history for: " + ", ".join(alive[:6]) + "."
                    if alive else "")
            return self._say(f"There's a {empty} folder but no conversations in "
                             f"it, so there's nothing to reopen.{tail}")
        names = self._names_of_sessions()
        guess = nearest.suggest(name, names + alive)
        if guess:
            return self._offer(
                f"I don't have a session called {name}. Did you mean {guess}?",
                yes=lambda g=guess: retry(g), again=retry)
        if names:
            return self._say(f"I don't have a session called {name}. Running "
                             f"now: " + ", ".join(names[:8])
                             + (". Past work: " + ", ".join(alive[:6])
                                if alive else ""))
        return self._say(f"I can't find a session called {name}, and nothing "
                         f"is running.")

    def _resume_only(self, hit: dict) -> dict:
        ok = actions.resume_session(hit["sid"], cwd=hit.get("cwd", ""))
        fleetcache.bust()
        return self._say(f"Reopened {hit['project']} in a new window."
                         if ok else
                         f"I couldn't reopen {hit['project']}.")

    def _resume_and_tell(self, hit: dict, message: str) -> dict:
        """Bring a closed conversation back, then say the thing to it.

        The window takes a few seconds to exist, so this waits for the session
        to appear before typing: sending immediately types into nothing and
        reports success."""
        import threading
        label = hit.get("project") or hit["sid"][:8]
        if not actions.resume_session(hit["sid"], cwd=hit.get("cwd", "")):
            return self._say(f"I couldn't reopen {label}, so I haven't sent "
                             f"anything.")
        fleetcache.bust()

        def _deliver():
            sid = ""
            for _ in range(40):                      # up to ~40 seconds
                time.sleep(1.0)
                try:
                    rows = fleetcache.snapshot(max_age=0)
                except Exception:
                    rows = {}
                row = rows.get(hit["sid"])
                if row:
                    sid = row.get("sid", "")
                    break
            if not sid:
                self.announce(f"{label} opened, but I couldn't see it come up, "
                              f"so I haven't sent anything. It's on screen if "
                              f"you want to paste it yourself.")
                return
            if self.watch.running:
                self.watch.expect(sid)
            if actions.send_to_session(sid, message):
                self.target = label
                self.announce(f"Sent it to {label}. I'll tell you what it says.")
            else:
                self.announce(f"{label} is open but I couldn't type into it.")
        threading.Thread(target=_deliver, daemon=True).start()
        return self._say(f"Reopening {label} and sending that. It takes a few "
                         f"seconds to come up.")

    def _what_needs(self, name: str) -> dict:
        """Report exactly what one agent is waiting on, and remember that it is
        waiting, so your very next message can just be the answer."""
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, self._what_needs)
        self.target = hit.get("label", name)
        if how == "maybe":
            # A weak match must be ASKED about, never answered as though it were
            # the thing you named: "what is fridey waiting on" reported on
            # voicebridge, which reads as Friday mishearing you and hiding it.
            return self._offer(f"Did you mean {hit.get('label', name)}?",
                               yes=lambda h=hit: self._what_needs(
                                   h.get("label", name)),
                               again=self._what_needs)
        label = hit.get("label", name)
        q = (hit.get("question") or hit.get("permission") or "").strip()
        if not q:
            state = "still working" if hit.get("status") == "working" else "done"
            return self._say(f"{label} doesn't need anything, it's {state}.")
        # Mark it as waiting so a bare reply routes straight there.
        if engine.AVAILABLE:
            try:
                self.focus = engine.routing.note_spoken(
                    self.focus, [{"sid": hit.get("sid", ""), "label": label,
                                  "kind": "blocked"}])
            except Exception:
                pass
        return self._say(f"{label} is asking: {q}")

    # ---- actions, always proposed first -----------------------------------
    def _propose_open(self, name: str) -> dict:
        """TIER 0. Bringing a window to the front is instantly reversible (you
        just look away), so asking permission is pure friction."""
        if not name:
            names = self._session_names()
            return self._say("Which one? " + (", ".join(names) if names
                                              else "nothing is running."))
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, self._propose_open)
        if how == "maybe":
            # Close enough to mention, not close enough to act on unasked.
            return self._offer(
                f"Did you mean {hit.get('label', name)}?",
                yes=lambda h=hit: self._perform(
                    {"kind": "open", "sid": h.get("sid", ""),
                     "label": h.get("label", name)}),
                again=self._propose_open)
        self.target = hit.get("label", name)
        return self._perform({"kind": "open", "sid": hit.get("sid", ""),
                              "label": hit.get("label", name)})

    # Between a session's name and your message sit two kinds of word: filler
    # that is never part of either, and one boundary word that says which kind
    # of message this is. They must be treated differently, or "for a summary"
    # loses both the "for" (which means you want an answer) and the "a".
    _FILLER_AFTER_NAME = {"session", "in", "claude", "please"}
    _BOUNDARY = {"that", "to", "about", "for"}

    def _resplit(self, said: str, name: str, message: str,
                 want: bool) -> tuple:
        """Find the session name in the sentence and take the rest as the
        message.

        Splitting on the first space put "voice" as the session and sent
        voicebridge the words "bridge session in claude for a summary of
        changes". Worse, "voice" is a close enough match to voicebridge that
        Friday acted on it without asking. The live list of session names is the
        only reliable place to cut."""
        names = self._target_names()
        if not (said and names):
            return name, message, want, False
        found, i, j, _sc = nearest.best_span(said, names)
        if not found:
            return name, message, want, False
        # Whether you actually SAID the name, or Friday worked it out. Resolving
        # "ap" to "api" and then treating it as though you had typed "api"
        # skipped the confirmation that stops a wrong instruction being written
        # into a running agent.
        heard = " ".join(said.split()[i:j])
        exact = nearest.flat(heard) == nearest.flat(found)
        toks = [w for w in said.split() if w.strip(".,?!:;")]
        rest = toks[j:]
        bare = lambda w: w.lower().strip(".,?!:;")
        while rest and bare(rest[0]) in self._FILLER_AFTER_NAME:
            rest.pop(0)
        joiner = ""
        if rest and bare(rest[0]) in self._BOUNDARY:
            joiner = bare(rest.pop(0))
        msg = " ".join(rest).strip(" .,")
        if not msg:
            return found, message, want, exact
        # "ask X FOR a summary" is a request for something, not an instruction:
        # sending the fragment "a summary of changes" is not what you would type.
        msg, want = _phrase(msg, joiner, want)
        return found, msg, want, exact

    def _propose_tell(self, name: str, message: str,
                      want_answer: bool = False, said: str = "") -> dict:
        name, message, want_answer, said_exactly = self._resplit(
            said, name, message, want_answer)
        # "ask it to also run the tests": you already told me who, once.
        if _ITS_RE.match((name or "").strip()) or (name or "").lower() in \
                _STANDS_FOR_SESSION:
            if not self.target:
                names = self._names_of_sessions()
                if not names:
                    return self._say("Nothing is running for me to send that "
                                     "to.")
                return self._offer(
                    "Which one? " + ", ".join(names[:8]),
                    yes=lambda: self._propose_tell(names[0], message,
                                                   want_answer),
                    again=lambda t: self._propose_tell(t, message,
                                                       want_answer))
            name, said_exactly = self.target, True
        """TIER 0 when you named the session exactly (that was your
        confirmation), TIER 1 when Friday had to guess which one you meant."""
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(
                name, lambda n: self._propose_tell(n, message, want_answer),
                message=message)
        # Some agents can be read and not typed into. Antigravity is an IDE,
        # not a terminal. Saying so here rather than after you confirm avoids
        # the worst failure Friday has available to it: a confident "sent" for
        # something that went nowhere, discovered hours later.
        if not agents.can_conduct(hit):
            who = hit.get("label", name)
            vendor = hit.get("vendor", "that agent")
            return self._say(
                f"I can read {who} but I can't type into it: {vendor} runs in "
                f"its own app rather than a terminal I can reach. Say "
                f"\"open {who}\" and I'll bring it up for you.")
        self.target = hit.get("label", name)
        act = {"kind": "tell", "sid": hit.get("sid", ""),
               "await": want_answer, "path": hit.get("path", ""),
               "label": hit.get("label", name), "message": message}
        if how == "exact" and (said_exactly or not said):
            return self._perform(act)
        self.pending = act
        self._offered = None
        return self._say(f'Did you mean {hit.get("label", name)}? '
                         f'I\'ll send "{message}".', needs_confirm=True)

    def _perform(self, act: dict) -> dict:
        """Do the thing that was just confirmed. Failures are reported plainly,
        never swallowed: a silent no-op here is the worst possible outcome."""
        if not engine.AVAILABLE:
            return self._say("I can't reach your sessions right now.")
        label = act.get("label", "it")
        try:
            if act["kind"] == "open":
                ok = actions.focus_session(act["sid"])
                return self._say(f"Opened {label}." if ok else
                                 f"I couldn't bring {label} to the front. It may "
                                 f"not be running in a terminal I can reach.",
                                 action={"kind": "open", "sid": act["sid"]})
            if act["kind"] == "event":
                cal = self.feeds.sources.get("calendar", [None])[0]
                r = cal.add(act["title"], act["at"]) if cal else {}
                if r.get("ok"):
                    return self._say(f"In your {r.get('calendar', '')} calendar "
                                     f"for {act['reads']}.")
                return self._say(f"It didn't go in: "
                                 f"{r.get('error', 'the calendar refused it')}. "
                                 f"Nothing was added.")
            if act["kind"] == "ticket":
                ji = self._tracker() or connectors.get("jira")
                r = ji.create(act["summary"], project=act.get("project", ""))
                if r.get("ok"):
                    return self._say(f"Filed {r['key']}. {r.get('url', '')}")
                if r.get("error") == "which_project":
                    opts = ", ".join(r.get("projects") or []) or "none I can see"
                    return self._say(f"Which project? {opts}")
                return self._say(f"It didn't file: {r.get('error', 'Jira said no')}"
                                 f". Nothing was created.")
            if act["kind"] == "move":
                ji = self._tracker() or connectors.get("jira")
                r = ji.move(act["key"], act["to"])
                if r.get("ok"):
                    return self._say(f"{act['key']} is now {r['state']}.")
                if r.get("error") == "which_state":
                    opts = ", ".join(r.get("states") or [])
                    return self._say(f"{act['key']} can go to: {opts}.")
                return self._say(f"It didn't move: "
                                 f"{r.get('error', 'Jira said no')}. Nothing "
                                 f"changed.")
            if act["kind"] == "new":
                ok = actions.new_session(act.get("about", ""))
                return self._say("Started it in a new window." if ok else
                                 "I couldn't open a new window.")
            if act["kind"] == "tell":
                # Note where the transcript ends BEFORE sending, or the agent's
                # answer cannot be told apart from what it said a minute ago.
                mark = ""
                if act.get("await") and act.get("path"):
                    try:
                        mark = replies.mark(act["path"])
                    except Exception:
                        mark = ""
                ok = actions.send_to_session(act["sid"], act["message"])
                if ok and act.get("await") and act.get("path"):
                    # The watchtower is already reading this session, so let it
                    # do the reporting. Two watchers means saying it twice.
                    if self.watch.running:
                        self.watch.expect(act["sid"])
                    else:
                        self._bring_back(act["path"], mark, label)
                    return self._say(f"Asked {label}. I'll tell you what it "
                                     f"says.",
                                     action={"kind": "tell",
                                             "sid": act["sid"], "undo": True})
                return self._say(f"Sent it to {label}." if ok else
                                 f"I couldn't reach {label}.",
                                 action={"kind": "tell", "sid": act["sid"],
                                         "undo": bool(ok)})
        except Exception as e:
            return self._say(f"That failed: {e}")
        return self._say("I'm not sure what to do with that.")

    # ---- open-ended conversation -----------------------------------------
    # Exactly what Friday can do today. The model is told this verbatim, because
    # the alternative is what actually happened in testing: asked to open a past
    # session it replied "I don't have access to your past sessions", which is
    # false, and asked about a session id it invented a confident paragraph.
    # A small model with no tools will fill any gap with plausible nonsense, so
    # the gap has to be closed explicitly.
    CAN_DO = [
        "tell you which coding sessions are running and what each is doing",
        "tell you what a specific session is waiting on",
        "bring a session's window to the front (say: open <name>)",
        "send an instruction to a session (say: tell <name> to <something>)",
        "ask a session a question and bring its answer back here "
        "(say: ask <name> for a summary of changes)",
        "put one question to every running session at once "
        "(say: ask everyone what they are working on)",
        "keep talking to the same session without naming it again "
        "(say: ask it to also run the tests)",
        "reopen a session that is CLOSED and send it something (say: ask "
        "<project> to ...); it waits for the window before typing",
        "watch a session and tell you when it finishes "
        "(say: tell me when it is done)",
        "stop what a session is doing (say: stop <name>)",
        "hold a multi-step plan and run it one step at a time, stopping if the "
        "agent asks you something (say: plan: first thing, then second thing)",
        "tell you where a plan has got to, and pick it up where it stopped "
        "(say: where is the plan)",
        "tell you, unprompted, whenever any session replies, summarised, with "
        "the ones waiting on you first",
        "send an alert to your phone when something needs you, even with the "
        "page closed and the phone locked (tap the bell to turn it on)",
        "give you a session's exact words instead of the summary (say: say more)",
        "tell you which session a bare reply would reach (say: who am I "
        "talking to)",
        "watch Slack and bring you a new message with who sent it and what "
        "they want, unprompted",
        "write a Slack reply for you to send (say: draft a reply). It cannot "
        "post to Slack itself, on purpose",
        "read a channel for a named day (say: what was said in <channel> "
        "yesterday, or on Friday)",
        "conduct Codex sessions as well as Claude ones, in the same fleet",
        "file a Jira ticket from what you said or what you just read "
        "(say: file a ticket: ...)",
        "move a ticket to another state (say: move PROJ-12 to done)",
        "put a meeting in your calendar (say: put it in for Thursday at 4)",
        "watch GitHub and tell you about review requests, mentions and broken "
        "builds, unprompted",
        "notice work on this machine that exists nowhere else: uncommitted "
        "changes and unpushed commits",
        "warn you before a meeting starts, if macOS has allowed calendar access",
        "give you everything in one answer across agents, Slack, GitHub, your "
        "repos and your calendar (say: brief me)",
        "tell you what to work on next, and why (say: what should I work on)",
        "take your answer to a session that asked you a question",
        "go quiet, or start speaking again",
    ]
    CAN_DO_MORE = [
        "search everything you have worked on before, not just what is running",
        "tell you what you were working on recently",
        "say whether other people on this Mac have sessions running",
        "read your GitHub: notifications, open pull requests, search issues",
        "search your Slack, once you connect it",
    ]
    CANNOT_YET = [
        "run two plans at once, on purpose: one at a time is the point",
        "merge a pull request, or approve a review",
        "search Jira or email",
        "start a brand new session, or write code itself",
        "see INSIDE another person's sessions on this Mac",
    ]

    def _help(self) -> dict:
        """What Friday can do, answered without the model.

        It used to go to the model, which on a fresh machine is not loaded, so
        the first word a new user is most likely to type got "my brain isn't
        loaded yet". That is the worst possible first answer: it reads as broken
        rather than as unconfigured.

        Six things, not thirty. The full list is real but nobody reads thirty
        bullet points, and the ones chosen here are the ones that work with
        nothing connected, so every suggestion made below actually does
        something on a machine that has just been set up."""
        can, cannot = self._abilities()
        lines = [
            "what should I work on?  one thing to start with, and why",
            "who needs me?           agents waiting on an answer from you",
            "brief me                everything, in one answer",
            "what's running?         your fleet, and what each is doing",
            "what's connected?       what I can see, and how to fix what I can't",
            "tell <session> <what>   say something to an agent, in its terminal",
        ]
        # Naming the gap is more useful than hiding it: the answer to "what can
        # you do" is incomplete without "and here is what would need connecting".
        # Trimmed to the first clause, because these entries carry their own
        # follow-up sentence and three of those joined by semicolons is a wall.
        short = []
        for c in cannot[:3]:
            first = re.split(r"[.(]", c)[0].strip()
            if first:
                short.append(first)
        tail = ('\n\nNot yet: ' + "; ".join(short)
                + '. Say "what\'s connected" to see why.') if short else ""
        return self._say("Ask me things like:\n  " + "\n  ".join(lines)
                         + tail + "\n\nThere is more, and README.md lists all "
                         "of it.")

    def _abilities(self) -> tuple:
        """What Friday can do RIGHT NOW, given what is actually connected.

        A fixed list goes stale the moment you connect something: with Slack
        live, the model was still told it could only "search your Slack once you
        connect it", and duly told you it had no access to your channels while
        Friday was reading them."""
        can, cannot = list(self.CAN_DO), []
        # Posting is off until you allow it, so which list it belongs in is a
        # live fact rather than a fixed one.
        try:
            writing = connectors.can_write()
        except Exception:
            writing = False
        (can if writing else cannot).append(
            "send a Slack message as you, after showing you the exact words "
            "and waiting for a yes" if writing else
            "post or send anything in Slack (it drafts, you send). Say "
            "\"let yourself post\" to change that")
        (can if writing else cannot).append(
            "file and move Jira tickets, after reading them back to you"
            if writing else
            "create or move a ticket. Say \"let yourself post\" to change that")
        # The calendar is not behind the writing switch: adding an event to
        # your own diary is not the same risk as writing under your name where
        # colleagues read it, and it is still confirmed every time.
        try:
            cal = self.feeds.sources.get("calendar", [None])[0]
            can.append("put a meeting in your calendar, after reading the day "
                       "and time back to you"
                       if cal and cal.available() else
                       "warn you before a meeting, once macOS allows it")
        except Exception:
            pass
        try:
            live = {n: v["ready"] for n, v in connectors.status().items()}
        except Exception:
            live = {}
        can += [a for a in self.CAN_DO_MORE if "slack" not in a.lower()]
        if live.get("slack"):
            can.append("read any channel in the Slack workspace you connected, "
                       "and summarise what is being asked")
            can.append("search your Slack messages")
        else:
            cannot.append("read Slack (not connected yet)")
        for name, label in (("gmail", "read your email"),
                            ("jira", "look at your Jira tickets")):
            (can if live.get(name) else cannot).append(
                label + ("" if live.get(name) else " (not connected yet)"))
        cannot += [c for c in self.CANNOT_YET
                   if "jira or email" not in c.lower()]
        return can, cannot

    def _chat(self, text: str) -> dict:
        """Real conversation, bounded by what Friday can actually do.

        The model never answers about the machine from its own head: the live
        facts are handed to it, and anything outside its abilities must be an
        honest 'I can't do that yet' rather than an invention."""
        # Last line of defence against the bluff. If you mention messages AND
        # name a channel that really exists, this is a Slack request however it
        # was phrased, and it must not reach a model that will answer "I don't
        # have access to personal chat histories" about a channel Friday can
        # read. Both conditions are required, so "moonshot is annoying" stays
        # ordinary conversation.
        if _SUBJECT_RE.search(text):
            try:
                sl = connectors.get("slack")
                if sl.ready() and hasattr(sl, "channel_names"):
                    found = nearest.best_window(text, sl.channel_names(40))
                    if found:
                        return self._read_channel_named(found, text)
            except Exception:
                pass
        if not engine.AVAILABLE or not engine.brain.model_ready():
            return self._say("I'm here, but my brain isn't loaded yet.")
        sessions = self._session_facts() or "none"
        can, cannot = self._abilities()
        sys_prompt = (
            "You are Friday, a calm assistant that coordinates a developer's "
            "coding agents. You do not write code.\n\n"
            "ONLY these facts are true; never invent others.\n"
            f"Sessions running right now:\n{sessions}\n"
            "That list is the complete truth about what each session is and what "
            "it is about. Never invent a description for a session; if its "
            "subject is not listed, say you do not know what it is working on.\n\n"
            "You CAN: " + "; ".join(can) + ".\n"
            "You CANNOT yet: " + "; ".join(cannot) + ".\n\n"
            "Rules: if asked for something in the CANNOT list, say plainly that "
            "you cannot do it yet, in one sentence, and do not speculate. If "
            "asked about something you have no fact for, say you do not know. "
            "Never guess what an unfamiliar name or id means. Answer in one or "
            "two short sentences, no lists, no markdown.")
        recent = [{"role": "user" if m["role"] == "user" else "assistant",
                   "content": m["text"]} for m in self.history[-6:]]
        # If the model is not actually up, say THAT. Answering "I didn't catch
        # that" when the truth is "my brain is still loading" sends the user
        # rephrasing a question that was fine, and it took 35 seconds to say it.
        if not engine.brain.up():
            engine.brain.start()
            return self._say("My brain is still loading, give it a few seconds "
                             "and ask again.")
        try:
            out = engine.brain._chat(
                [{"role": "system", "content": sys_prompt}] + recent,
                timeout=engine.brain.TIMEOUT_SLOW, max_tokens=120)
        except Exception:
            out = ""
        if out:
            return self._say(engine.brain._clean(out))
        return self._say("That one took too long to think about. Ask me again?")

    # ---- helpers ----------------------------------------------------------
    _TOPIC_CACHE = {}          # sid -> short subject, computed once per session

    def _subject(self, sid: str, raw: str) -> str:
        """A rambling first prompt turned into a few words a person would use.

        "Hey. I need to learn about RWA tokenization and ERC 7943 and 3643
        Because I would be doing…" becomes "learning RWA tokenisation". Quoting
        the raw prompt is technically honest but reads like a log; this is what
        you would actually call that session."""
        raw = (raw or "").strip()
        if not raw:
            return ""
        hit = self._TOPIC_CACHE.get(sid)
        if hit and hit[0] == raw:
            return hit[1]
        short = ""
        try:
            if engine.AVAILABLE and engine.brain.up():
                short = engine.brain._chat(
                    [{"role": "system", "content":
                      "Turn this first request into a 3-6 word description of "
                      "what the session is about, as a person would say it "
                      "(e.g. 'learning RWA tokenisation', 'building the voice "
                      "app'). Lowercase, no quotes, no punctuation, no preamble."},
                     {"role": "user", "content": raw[:400]}],
                    timeout=6.0, max_tokens=20)
                short = " ".join((short or "").split())[:60].strip(' ."\'')
        except Exception:
            short = ""
        if not short:                       # fall back to a trimmed prompt
            short = " ".join(raw.split()[:8])
        self._TOPIC_CACHE[sid] = (raw, short)
        return short

    def _session_facts(self) -> str:
        """Every session as a line of FACT: its name, its state, and what it is
        actually about (taken from the first thing the human asked it). Without
        this the model had only a generated id like 'krishojha-7f' to reason
        with, and duly invented 'a backend data processing pipeline'."""
        try:
            rows = list(fleetcache.snapshot().values())
        except Exception:
            return ""
        out = []
        for r in rows:
            state = ("waiting on you" if (r.get("question") or r.get("permission"))
                     else ("working" if r.get("status") == "working" else "idle"))
            topic = self._subject(r.get("sid", ""), r.get("topic"))
            line = f"- {r.get('label')}: {state}"
            if topic:
                line += f'; it is about: "{topic}"'
            else:
                line += "; subject unknown"
            need = r.get("question") or r.get("permission")
            if need:
                line += f'; it is asking: "{need}"'
            out.append(line)
        return "\n".join(out)

    def _session_names(self) -> list:
        try:
            return [r.get("label", "") for r in fleetcache.snapshot().values()
                    if r.get("label")]
        except Exception:
            return []

    def _find(self, name: str):
        """Match a spoken/typed name to a session, EXCLUDING weak matches.

        This drops `how`, so a caller cannot tell a solid match from a guess.
        That makes it the wrong tool for anything that acts, and it must
        therefore never return a 'maybe'.

        Searches the FLEET first, deliberately: those are the names Friday
        shows you, and an assistant that displays "krishojha-7f" then claims it
        cannot find "krishojha-7f" is broken in the most infuriating way. The
        older roster lookup stays as a fallback for sessions the fleet sensor
        cannot see."""
        if not (name and engine.AVAILABLE):
            return None
        hit, how = self._find_how(name)
        return hit if how in ("exact", "fuzzy") else None

    def _find_how(self, name: str):
        """(session, how) where how is 'exact' | 'fuzzy' | ''. The caller uses
        `how` to decide whether it may act without asking: an exact name is the
        user's own confirmation, a fuzzy one is Friday guessing."""
        if not (name and engine.AVAILABLE):
            return None, ""
        q = name.strip().lower()
        try:
            rows = list(fleetcache.snapshot().values())
        except Exception:
            rows = []
        for r in rows:                                  # exact name
            if (r.get("label") or "").lower() == q:
                return r, "exact"
        starts = [r for r in rows if (r.get("label") or "").lower().startswith(q)]
        if len(starts) == 1:                            # unambiguous prefix
            return starts[0], "fuzzy"
        contains = [r for r in rows if q in (r.get("label") or "").lower()]
        if len(contains) == 1:                          # unambiguous substring
            return contains[0], "fuzzy"
        # Deliberately NO fallback to the older roster lookup: it labels
        # sessions by their first prompt, so it happily "finds" a session
        # called "Reply with exactly ALPHA". A miss is better than nonsense.
        #
        # But a miss is not the end. Session names get misheard exactly like
        # channel names do, so if one sounds close, say which and let the user
        # decide. 'sounds-like' may be acted on; 'maybe' may only be offered.
        labels = [(r.get("label") or "") for r in rows]
        how, label = nearest.resolve(name, labels)
        if how in ("sounds-like", "maybe"):
            for r in rows:
                if (r.get("label") or "") == label:
                    return r, ("fuzzy" if how == "sounds-like" else "maybe")
        return None, ""

    def _bring_back(self, path: str, mark: str, label: str) -> None:
        """Watch for the agent's answer and say it here, in this thread.

        Delivering a question and then leaving the answer in a terminal you are
        not looking at is half a conversation, and it is the half that saves you
        nothing: you still have to go to the window to find out."""
        import threading

        def _watch():
            try:
                said = replies.wait_for_reply(path, mark)
            except Exception:
                said = ""
            if said:
                self.announce(f"{label} says: " + said[:700])
            else:
                self.announce(f"{label} hasn't answered yet. It may be waiting "
                              f"on something, or still working.")
        threading.Thread(target=_watch, daemon=True).start()

    def _offer(self, question: str, yes, again=None, no: str = "") -> dict:
        """Ask "did you mean X?" and remember what a yes means.

        Withholding a name Friday already has is the failure this replaces: it
        knows the real list, so the honest move is to put the closest one to you
        rather than report that you said something unrecognisable."""
        self._offered = {"yes": yes, "again": again, "no": no}
        self.pending = None      # a "yes" must have exactly one meaning
        return self._say(question)

    def _names_of_sessions(self) -> list:
        try:
            return [r.get("label") or "" for r in
                    fleetcache.snapshot().values() if r.get("label")]
        except Exception:
            return []

    def _target_names(self) -> list:
        """Everything you could plausibly be naming: what is running, and every
        project you have ever worked in.

        Only running sessions counted before, so "ask promptguard to look at my
        resume" could not even be parsed: the name was not in the list, so it
        was cut at the space and "prompt" was looked up instead."""
        names = list(self._names_of_sessions())
        try:
            for n in memory.project_names():
                if n not in names:
                    names.append(n)
        except Exception:
            pass
        return names

    def _say(self, text: str, needs_confirm: bool = False,
             action: dict = None) -> dict:
        self.add("friday", text)
        return {"reply": text, "needs_confirm": needs_confirm,
                "action": action or {}}

    def announce(self, text: str, items: list = None) -> dict:
        """Something Friday brings up on its own (the attention engine decided
        it was worth it). Marked distinctly so the UI can show it as Friday
        starting the conversation, not answering.

        `items` are the underlying events. Remembering which of them are
        WAITING on an answer is what lets you reply "use the redis one" and
        have it reach the agent that asked, instead of being chat."""
        if items and engine.AVAILABLE:
            try:
                self.focus = engine.routing.note_spoken(self.focus, items)
            except Exception:
                pass
        # Whoever Friday just reported on is who you are talking to. Otherwise
        # "ask it to do X" after an announcement means nothing, and you are back
        # to naming a session you were just told about.
        if items and len(items) == 1:
            self.target = items[0].get("label") or self.target
        self._maybe_push(text, items)
        return self.add("friday", text, kind="proactive", about=items)


def _cwd_for(transcript_path: str) -> str:
    """Best guess at where a session was working, so a resume lands in the
    right project rather than the home directory."""
    try:
        import json as _j
        with open(transcript_path, "r", errors="ignore") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = _j.loads(line)
                except Exception:
                    continue
                cwd = rec.get("cwd")
                if cwd:
                    return cwd
    except Exception:
        pass
    return ""


def _join(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _is(n: int) -> str:
    return "is" if n == 1 else "are"
