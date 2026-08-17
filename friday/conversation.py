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
               learn, push, replies, trackers, watchtower, when)

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
LEARNED = "learned"        # "what have you learned?" / "forget that"
PLAN_ASK = "plan_ask"      # "work out a plan for adding OAuth"
TRACKER_PREF = "tracker_pref"   # "use linear for tickets"
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

# The optional group needed to be optional. "schedule it for thurs at 4" only
# matched when the word after the verb was one of the listed pronouns AND
# something followed it, so the commonest phrasing of all fell through to the
# model.
# Three shapes, because people say it three ways and the first two were the
# only ones here. "Let's meet Thursday at 4", "set up a call with Sam tomorrow
# at 4" and "can we get a meeting in the calendar for tomorrow at 4" all fell
# through to the model: the verb list had no "meet", no "set up" and no "get",
# and the optional object arm swallowed "a call" and then demanded a
# preposition where "with" stood.
_SCHEDULE_RE = re.compile(
    # put/add/book/schedule/pencil ... for|at|on <when>
    r"\b(?:put|add|book|schedule|pencil|set\s+up|sort\s+out)\b.*?"
    r"\b(?:for|at|on)\s+(.+?)\s*[.!]?$"
    # book|schedule a meeting <when>
    r"|\b(?:schedule|book|set\s+up)\s+(?:a\s+|an\s+)?"
    r"(?:meeting|call|sync|standup|chat|catch\s?up)\b\s*(.*)$"
    # let's meet <when> / can we meet <when>. The lookahead is load-bearing:
    # without it "the meeting was long" and "nice to meet you" were calendar
    # requests, because the arm matched any sentence containing the word.
    r"|\b(?:meet|meeting)\b\s*(?:with\s+[\w.\-]+\s*)?"
    r"(?=[^.!?]*(?:\d|today|tonight|tomorrow|mon|tue|wed|thu|fri|sat|sun))"
    r"(.+?)\s*[.!]?$",
    re.I)

# The tracker can be named two ways round, "file a linear ticket" and "file a
# ticket in linear", and both were previously parsed and then thrown away, so
# saying where to put it had no effect on where it went.
_TRACKERS = "jira|linear|github|gitlab"
_TICKET_RE = re.compile(
    r"\b(?:file|create|open|raise|make)\s+(?:a\s+|an\s+)?"
    r"(?:(" + _TRACKERS + r")\s+)?(?:ticket|issue|bug|task)\b"
    r"(?:\s+(?:in|on)\s+(" + _TRACKERS + r"))?"
    r"(?:\s+(?:in|on|for)\s+([A-Z][A-Z0-9_]{1,9}))?"
    # "for the parser bug" and "about the login timeout" are how people say it
    # at least as often as a colon, and neither matched: the sentence fell
    # through to READING your tickets, so asking to file one answered with an
    # unrelated existing one and nothing was created.
    r"(?:\s*[:,]\s*(.+)|\s+(?:for|about|because|saying)\s+(.+))?$", re.I)
# "use linear for tickets", "file tickets in jira from now on"
_TRACKER_PREF_RE = re.compile(
    r"\b(?:use\s+(" + _TRACKERS + r")\s+for\s+(?:tickets?|issues?)"
    r"|(?:file|put)\s+(?:my\s+)?(?:tickets?|issues?)\s+(?:in|on)\s+"
    r"(" + _TRACKERS + r"))\b", re.I)
_MOVE_RE = re.compile(
    # A key is PROJ-12 (Jira, Linear) or owner/repo#12 (GitHub, GitLab). It was
    # only the first, so no GitHub issue could be moved by name.
    r"\b(?:move|set|mark|transition|put|close)\s+"
    r"((?:[A-Za-z][A-Za-z0-9]*-\d+)|(?:[\w.\-]+/[\w.\-]+#\d+)|(?:#\d+))\s+"
    r"(?:to|as|into)\s+(.+?)\s*[.!]?$", re.I)

_PLAN_RE = re.compile(
    r"^\s*(?:make|write|draft)?\s*a?\s*plan(?:\s+for\s+(\S+))?\s*[:,]\s*(.+)$",
    re.I | re.S)
# "run the plan" always means the plan. A bare "approved" or "go ahead" only
# means the plan if there IS one waiting: said with an agent blocked on a
# question, it is an answer to the agent, and starting a plan instead is a
# different action entirely from the one intended.
_PLAN_GO_RE = re.compile(
    r"\b(?:run|start|go ahead with|approve|do)\s+(?:the\s+)?plan\b", re.I)
_PLAN_GO_BARE_RE = re.compile(r"^\s*(?:approved|go ahead)\s*[.!]?\s*$", re.I)
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
# The destination is CAPTURED now. It was matched and then thrown away, so
# "post it to #general" posted the draft to whichever channel it was drafted
# for, which is a message going to colleagues the user had just redirected it
# away from.
_SEND_RE = re.compile(
    r"^\s*(?:go ahead and\s+)?(?:send|post)\s*(?:it|that|this)?\s*"
    r"(?:to\s+(?:slack|(#?[\w.\-]+)))?\s*[.!]?\s*$", re.I)
# The one switch in front of every Slack post, DM, ticket and GitHub comment.
# It was unanchored, so "tell api you can comment out that block" turned
# posting on for Slack AND GitHub, and the sentence never reached api at all.
# An ordinary coding sentence must not be able to grant this.
#
# So: the whole utterance has to be about the permission and nothing else. Any
# sentence that is really an instruction to a session is excluded at the call
# site, and the bare forms below are anchored at both ends.
_ALLOW_RE = re.compile(
    r"^\s*(?:friday[,\s]+)?(?:please\s+)?"
    r"(?:(?:let|allow)\s+(?:yourself|you)\s+(?:to\s+)?"
    r"(?:post|write|reply|comment|send)(?:\s+things?)?"
    r"|(?:enable|turn on)\s+(?:slack\s+|github\s+)?"
    r"(?:posting|writing|replies|comments)"
    r"|(?:stop|disable|turn off)\s+(?:yourself\s+)?"
    r"(?:posting|writing|commenting)"
    r"|you\s+can\s+(?:post|reply|comment|send\s+things)(?:\s+now)?)"
    r"\s*[.!]?\s*$", re.I)

_DRAFT_RE = re.compile(
    r"\b(?:draft|write|compose)\s+(?:me\s+)?(?:a\s+|the\s+)?"
    r"(?:reply|response|answer|message)\b(?:\s+(?:saying|that says)\s+(.*))?$"
    r"|\breply\s+(?:saying|with)\s+(.*)$", re.I)

_MISSED_RE = re.compile(
    r"\bwhat\s+(?:did\s+i|have\s+i)\s+miss(?:ed)?\b|\bcatch\s+me\s+up\b"
    r"(?!\s+on\b)|\bwhat\s+happened\s+while\s+i\s+was\s+(?:away|out|gone)\b|"
    r"\banything\s+(?:new|since)\b", re.I)
# One noisy agent should not cost you the whole feature.
# Muting names ONE session. The captured span used to run to the end of the
# sentence, so "silence the notifications for a bit" asked to mute a session
# called "notifications for a bit". A trailing "for a bit" or "for now" is how
# long, not who, and "the notifications" is not a session at all.
_MUTE_TAIL = r"(?:\s+session)?(?:\s+for\s+(?:now|a\s+(?:bit|while|moment)))?"
_MUTE_RE = re.compile(
    r"\b(?:ignore|mute|silence|stop\s+telling\s+me\s+about)\s+"
    r"(?:the\s+)?([\w.\-]+(?:\s+[\w.\-]+)?)" + _MUTE_TAIL + r"\s*[.!?]?$"
    r"|\b(?:unmute|listen\s+to|un-?ignore)\s+(?:the\s+)?"
    r"([\w.\-]+(?:\s+[\w.\-]+)?)" + _MUTE_TAIL + r"\s*[.!?]?$", re.I)
# Words that mean "everything", not a session. These reach the whole-quiet
# switch rather than being hunted for as a name.
_MUTE_ALL = {"notifications", "notification", "alerts", "everything",
             "all of it", "them", "all", "updates", "it all"}
_STUCK_RE = re.compile(
    r"\b(?:is\s+)?any(?:one|thing|body)\s+(?:stuck|blocked|waiting)\b|"
    r"\bwho(?:'?s|\s+is)?\s+(?:stuck|blocked|waiting)\b|"
    r"\bwhat(?:'?s|\s+is)?\s+blocked\b", re.I)

_MORE_RE = re.compile(
    r"\b(?:say|tell me)\s+more\b|\bthe\s+(?:full|whole|exact)\s+"
    r"(?:thing|version|message|reply)\b|\bwhat\s+exactly\s+did\s+"
    r"(?:it|he|she|they|\S+)\s+say\b|\bin\s+full\b|\bverbatim\b", re.I)
# Clearing the target. The page had a "clear" button that repainted itself and
# asked a question, so the chip vanished while a bare message still went to that
# session: the display and the truth disagreed, in the direction that sends
# words somewhere you thought you had deselected.
_NOBODY_RE = re.compile(
    r"^\s*(?:talk to nobody|clear the target|forget who|"
    r"stop talking to \w+|nobody)\s*[.!]?\s*$", re.I)
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
# Anchored at the START, because interrupting an agent destroys in-flight work
# and cannot be undone. Unanchored, "cancel the meeting with api" and "stop
# worrying about the api" both hit Escape on a running session: the pattern
# fired on any short sentence containing the word, and the session name was
# then found anywhere in it. A stop command begins with the verb.
_STOP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:stop|interrupt|halt|cancel|escape)\s+(?:the\s+)?"
    r"([\w.\-]+(?:\s+[\w.\-]+)?)(?:\s+session)?\s*[.!?]?$", re.I)
# "it", "him", "that one": whoever we were just talking to.
_ITS_RE = re.compile(r"^(?:it|him|her|them|that\s+(?:one|session)|"
                     r"the\s+same\s+one)$", re.I)

# "who needs me" and "which sessions need..." are NOT this. They were folded in
# here, so the one question about urgency was answered with the wall of text it
# exists to replace: with fifty sessions running, "who needs me?" listed all
# fifty. They are handled by the stuck path instead.
_FLEET_RE = re.compile(
    r"\b(what('?s| is)? (running|going on|happening)|status|"
    r"what are you (running|watching)|"
    r"how are (things|we)|(show|list) (me )?(my )?(agents?|sessions?))\b", re.I)
_WHO_NEEDS_RE = re.compile(
    r"\bwho needs me\b|\bwho'?s? (?:waiting|stuck|blocked)\b"
    r"|\bwhich (?:agents?|sessions?) (?:need|are waiting|are stuck)\b"
    r"|\bwhat needs (?:me|my attention)\b", re.I)
# The article is skipped BEFORE the name is captured. It was optional-and-
# captured, so "open a new tab" gave the name "a", which prefix-matched the
# first session starting with that letter and focused it. _FILLER exists for
# exactly this and was never consulted on this path.
# Anchored at the END as well. "open source is good" was read as the command
# "open" with the name "source" and pulled a session to the front, because the
# pattern stopped at the name and ignored the rest of the sentence. A real open
# command finishes there.
# Anchored at the START as well as the end. "let me open the docs" is a person
# thinking out loud, and it opened a session called docs-site.
_OPEN_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?"
    r"(open|switch to|go to|jump to|resume|show me)\s+"
    r"(?:(?:the|a|an|my|that)\s+)?"
    r"(?:session\s+)?([\w.\-]+)(?:\s+session)?\s*[.!?]?$", re.I)
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
    # Quotes around the name are how people write one that reads as an
    # ordinary word ("tell 'test' to stop"), and they stopped the name matching
    # at all, so the whole sentence fell through to the model.
    r"(?:the\s+)?(?:session\s+)?['\"`‘’“”]?([\w.\-]+)"
    r"['\"`‘’“”]?\s+"
    r"(?:session\s+)?(?:that\s+|to\s+|about\s+)?(.+)$", re.I)
_FIND_RE = re.compile(
    r"\b(?:find|search(?:\s+for)?|look for|which session|what session|"
    r"where did i|where was i|the (?:session|chat|conversation) (?:where|about|"
    r"in which))\b\s*(.*)$", re.I)
_RECENT_RE = re.compile(
    r"\b(?:what (?:was|were) i (?:working on|doing)|recent sessions?|"
    r"my recent work|what have i been (?:working on|doing))\b", re.I)
# Deliberately narrow, and deliberately nameless. This carried a hard-coded
# person's name, which is both a privacy problem in a public repository and a
# feature that only worked for one machine. It also matched a bare "anyone
# else", so "is anyone else seeing this error" was answered with a list of the
# other accounts on the Mac: a confident, off-topic answer that volunteered
# somebody's name. It now needs an actual reference to users or accounts.
_OTHERS_RE = re.compile(
    r"\b(?:other|another|someone else'?s?|anyone else'?s?)\s+"
    r"(?:users?|accounts?|logins?|people|sessions on this (?:mac|machine))\b"
    r"|\bwho else is (?:logged|signed) in\b"
    r"|\b(?:other|anyone else)\s+logged in\b", re.I)
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
# Asking the AGENT for the plan, rather than dictating one. "plan: a, then b"
# is you writing the steps; this is you naming the goal. The distinction is the
# colon and the word "then", so this pattern deliberately does not match those.
_PLAN_ASK_RE = re.compile(
    r"\b(?:work out|figure out|come up with|draw up|propose|think through)\s+"
    r"(?:an? |the )?(?:plan|approach)\s+(?:for|to|on)\s+(.+)$"
    r"|\bask\s+(\S+)\s+(?:for|to (?:make|write|draw up))\s+a plan\s+"
    r"(?:for|to|on)\s+(.+)$"
    r"|\bhow (?:should|would) (?:we|i|you)\s+(.+)$", re.I)
# A learned preference you cannot see or clear is a bug you cannot fix.
_LEARNED_RE = re.compile(
    r"\bwhat have you learn(?:ed|t)\b|\bwhat do you think i (?:care|don'?t care)\b"
    r"|\bwhy (?:are you|have you been) (?:so )?quiet about\s+(.+?)\s*[.?!]?$"
    r"|\b(forget) what you(?:'?ve)? learn(?:ed|t)(?:\s+about\s+(.+?))?\s*[.?!]?$",
    re.I)
_HELP_RE = re.compile(
    # Bare "help" is anchored at both ends: "help me file a ticket" is a request
    # to file a ticket, and answering it with a menu is the assistant equivalent
    # of pointing at the manual.
    r"^\s*(?:help|commands?|\?+)\s*[?.!]?$"
    r"|^\s*(?:what can you do|what do you do|what are you|"
    r"what can i (?:ask|say|do)|how does this work|"
    r"what (?:else )?can you help (?:me )?with)\b", re.I)
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
# A bare yes, and it has to be BARE. These matched the opening word and let the
# rest of the sentence go, so "go ahead and restart the api server" was read as
# "yes" and answered "Nothing was waiting on you". Anything with an instruction
# attached is an instruction, and it should reach the parser that handles it or
# the model, not the confirmation path.
_YES_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|please|"
    r"go for it|sounds good|approved|confirmed)"
    r"(?:\s+(?:please|thanks|then|mate))?\s*[.!]?\s*$", re.I)
_NO_RE = re.compile(
    r"^\s*(?:no|nope|nah|cancel|stop|don'?t|do not|never ?mind|forget it)"
    r"(?:\s+(?:please|thanks|thanks))?\s*[.!]?\s*$", re.I)
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
    # The calendar verbs count as the confirmation themselves. "Schedule it for
    # Thursday at 4" carries no other meeting word, so it needed one and did not
    # have one, and the commonest phrasing there is fell through to the model.
    # "Put" and "add" still need one, because "put the kettle on" and "add it to
    # the ticket" are not calendar requests.
    # The calendar verbs count as their own confirmation only when what follows
    # is a MEETING. "Book a flight for tomorrow at 9am" and "book a table for
    # 8" are not calendar requests, and the bare verb accepted both.
    if m and (re.search(r"\b(meet|meeting|call|sync|calendar|invite|"
                        r"catch\s?up|put it in|pencil|standup|one.on.one|1:1)\b",
                        t, re.I)
              or re.search(r"\b(?:schedule|book)\s+"
                           r"(?:it|that|this|me|us|him|her|them)\b", t, re.I)):
        return SCHEDULE, {"said": t}
    m = _MOVE_RE.search(t)
    if m:
        # PROJ-12 is uppercased because that is how Jira and Linear write it
        # and speech recognition does not capitalise. owner/repo#12 is left
        # alone, because GitHub and GitLab paths are case sensitive and
        # CC-VB/VOICEBRIDGE#20 is a 404.
        key = m.group(1)
        return MOVE, {"key": key if "/" in key or key.startswith("#")
                      else key.upper(), "to": m.group(2).strip()}
    m = _TRACKER_PREF_RE.search(t)
    if m:
        return TRACKER_PREF, {"which": (m.group(1) or m.group(2)).lower()}
    m = _TICKET_RE.search(t)
    if m:
        return TICKET, {"where": (m.group(1) or m.group(2) or "").lower(),
                        "project": (m.group(3) or "").upper(),
                        "summary": (m.group(4) or m.group(5) or "").strip()}
    if _PLAN_STOP_RE.search(t):
        return PLAN_GO, {"stop": True}
    if _PLAN_WHERE_RE.search(t):
        return PLAN_WHERE, {}
    m = _PLAN_RE.match(t)
    if m:
        return PLAN, {"target": (m.group(1) or "").strip(),
                      "body": m.group(2).strip()}
    if _PLAN_GO_RE.search(t):
        return PLAN_GO, {"stop": False, "bare": False}
    if _PLAN_GO_BARE_RE.match(t):
        return PLAN_GO, {"stop": False, "bare": True}
    if _BRIEF_RE.search(t):
        return BRIEF, {}
    m = _ALLOW_RE.search(t) if not _TELL_RE.search(t) else None
    if m:
        off = bool(re.search(r"\b(stop|disable|turn off)\b", t, re.I))
        return ALLOW, {"on": not off}
    m = _SEND_RE.match(t)
    if m:
        return SEND, {"where": (m.group(1) or "").strip()}
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
        who = (m.group(1) or m.group(2) or "").strip()
        if who.lower() in _MUTE_ALL:
            # "silence the notifications" means all of them, and hunting for a
            # session by that name found the nearest match and muted it.
            return QUIET if m.group(1) else RESUME, {}
        return MUTE, {"name": who, "on": bool(m.group(1))}
    if _NOBODY_RE.match(t):
        return WHO, {"clear": True}
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
    m = _LEARNED_RE.search(t)
    if m:
        return LEARNED, {"about": (m.group(1) or m.group(3) or "").strip(),
                         "forget": bool(m.group(2))}
    if _HELP_RE.search(t):
        return HELP, {}
    m = _PLAN_ASK_RE.search(t)
    if m and ":" not in t.split(" ", 1)[-1][:14]:
        goal = (m.group(1) or m.group(3) or m.group(4) or "").strip(" .?")
        return PLAN_ASK, {"goal": goal, "target": (m.group(2) or "").strip()}
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
    # "we should probably tell the team" captured the name "the". An article is
    # never a session, and this path types into a running agent.
    if m and (m.group(1) or "").lower() in _FILLER:
        m = None
    # "let's not tell anyone yet" is a decision not to say something. Reading it
    # as an instruction to a session called "anyone" is the opposite.
    if m and re.search(r"\b(?:not|never|don'?t|do not|rather not|no need to)\s+"
                       r"(?:\w+\s+){0,2}(?:tell|ask|message|send)\b", t, re.I):
        m = None
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
        if name.lower() in _FILLER:
            # An article that survived the pattern is not a name. One letter
            # prefix-matches any session starting with it, so "open a new tab"
            # opened api.
            return OPEN, {"name": ""}
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
    if _WHO_NEEDS_RE.search(t):
        return STUCK, {}
    if _FLEET_RE.search(t):
        return ASK_FLEET, {}
    if _YES_RE.match(t):
        return CONFIRM, {}
    if _NO_RE.match(t):
        return CANCEL, {}
    return CHAT, {}


def _describe_pending(act: dict) -> str:
    """An offer, in the words you would use to ask for it again."""
    kind = (act or {}).get("kind", "")
    if kind == "ticket":
        return f'file "{(act.get("summary") or "")[:60]}"'
    if kind == "move":
        return f"move {act.get('key', 'that ticket')} to {act.get('to', '')}"
    if kind == "tell":
        return (f'send "{(act.get("message") or "")[:50]}" to '
                f'{act.get("label", "a session")}')
    if kind == "two":
        return "send those two instructions"
    if kind == "open":
        return f"open {act.get('label', 'that session')}"
    if kind == "new":
        return "start a new session"
    if kind == "send":
        return "send that message"
    return "do that"


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
        # What Friday has mentioned lately, so that doing something about one of
        # them shortly after can be told apart from ordinary work.
        self._told = {}
        self.quiet = False         # Friday's own switch, independent of vb's
        self._turn = 0
        self._pending = None
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
            tell_person=self._tell_person,
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
        # Counted here so an offer knows how many things you have said since.
        # Elapsed time alone is not enough: a three-turn detour taken quickly
        # and a slow one are the same mistake, because in both you have stopped
        # thinking about what you were asked.
        self._turn += 1
        intent, payload = classify(text)

        # Something Friday offered a moment ago ("did you mean X?"). A yes takes
        # it; a short reply is another go at the name. Letting either fall
        # through to the model produced the same invented refusal three times
        # while the answer sat in a list Friday had already fetched.
        if (self._offered and self._offered.get("pick")
                and intent == CONFIRM):
            # "Which one?" has no yes. Asking again is the only honest move.
            return self._say("Which one, though? A yes doesn't tell me. Say the "
                             "name.")
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

        # A pending offer takes precedence: "yes" means yes to THAT. But only
        # while it is still plausibly what you are answering.
        #
        # Nothing ever expired a pending action, so this sequence did the wrong
        # thing: file a ticket (Friday asks), then "who needs me?" (Friday says
        # api is waiting on you), then "yes". The yes filed the ticket and api's
        # question went unanswered. Friday had itself just said an agent was
        # waiting, and then took the word for something else.
        if self.pending and intent in (CONFIRM, CANCEL):
            act = self.pending
            stale = (time.time() - float(act.get("asked_at") or 0)
                     > self.PENDING_LIVES)
            moved_on = int(act.get("turn") or 0) < self._turn - self.PENDING_TURNS
            if stale or moved_on:
                self.pending = None
                what = _describe_pending(act)
                if intent == CANCEL:
                    return self._say(f"Dropped it, then. I won't {what}.")
                waiting = self._waiting()
                if waiting:
                    who = waiting[0].get("label", "a session")
                    return self._say(
                        f"I'm not sure what that's yes to. A while back I "
                        f"asked whether to {what}, and {who} is waiting on you "
                        f"as well. Say \"tell {who} yes\", or ask me again to "
                        f"{what}.")
                # Nothing else is competing for it, but the offer is old enough
                # that acting on it silently would be acting on something you
                # may not still have in mind. Show it again, and RE-STAMP it, or
                # the next yes finds the same stale timestamps and shows it
                # again, forever: the action could never be accepted and the
                # only escape was "no".
                act = dict(act)
                act.pop("asked_at", None)
                act.pop("turn", None)
                self.pending = act
                return self._say(f"Just to check, this is still from earlier: "
                                 f"{what}?", needs_confirm=True)
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
            return self._tickets()
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
        if intent == PLAN_ASK:
            return self._ask_for_plan(payload["goal"], payload.get("target", ""))
        if intent == LEARNED:
            return self._learned(payload.get("about", ""),
                                 payload.get("forget", False))
        if intent == HELP:
            return self._help()
        if intent == FIRE:
            return self._fire()
        if intent == SCHEDULE:
            return self._schedule(payload["said"])
        if intent == TRACKER_PREF:
            return self._set_tracker(payload["which"])
        if intent == TICKET:
            return self._file_ticket(payload["project"], payload["summary"],
                                     payload.get("where", ""))
        if intent == MOVE:
            return self._move_ticket(payload["key"], payload["to"])
        if intent == PLAN:
            return self._make_plan(payload["target"], payload["body"])
        if intent == PLAN_GO:
            # A bare "approved" with no plan waiting is a plain yes, and the
            # thing it is most likely answering is an agent. Running a plan
            # instead would be a different action from the one intended, taken
            # silently.
            if payload.get("bare") and not (plans.active() or plans.latest()):
                routed = self._bare_answer("yes")
                if routed is not None:
                    return routed
            return self._run_plan(payload.get("stop", False))
        if intent == PLAN_WHERE:
            return self._plan_status()
        if intent == ALLOW:
            return self._allow_write(payload["on"])
        if intent == SEND:
            return self._send_draft(payload.get("where", ""))
        if intent == DRAFT:
            return self._draft(payload.get("gist", ""))
        if intent == MISSED:
            return self._missed()
        if intent == STUCK:
            return self._stuck()
        if intent == MUTE:
            return self._mute(payload["name"], payload["on"])
        if intent == WHO:
            if payload.get("clear"):
                self.target = ""
                return self._say("Cleared. Nothing you say goes to a session "
                                 "until you name one.")
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
            # Nothing of Friday's own is pending, but an AGENT may be waiting,
            # and "yes" is the most natural possible answer to "Should I
            # proceed?". This said "Nothing was waiting on you" while two
            # sessions sat blocked, which is both wrong and the exact moment
            # the product is supposed to earn its keep.
            routed = self._bare_answer("yes" if intent == CONFIRM else "no")
            if routed is not None:
                return routed
            return self._say("Nothing was waiting on you." if intent == CONFIRM
                             else "Okay.")

        # An agent asked you something a moment ago and this reads like the
        # answer: send it there rather than treating it as small talk. This is
        # the whole point of a supervisor, you answer once, in the thread, and
        # it reaches the right session.
        routed = self._maybe_route_answer(text)
        if routed is not None:
            return routed
        # A reference with nothing to refer to must not reach the model, which
        # will answer it as a general question about a machine it cannot see.
        nowhere = self._pointing_at_nothing(text)
        if nowhere is not None:
            return nowhere
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
        if not ok:
            return self._say(f"I couldn't reach {label}.",
                             action={"kind": "tell", "sid": sid})
        self._acted_on(label)
        nxt = self._next_waiting(besides=sid)
        return self._say(f"Told {label}." + (f"\n{nxt}" if nxt else ""),
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
            if self._answers(sl):
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

    # How long an unanswered offer stays answerable by a bare "yes", and how
    # many of your turns it survives. Both, because a three-turn detour taken
    # quickly and a slow one are the same mistake: you have stopped thinking
    # about the thing you were asked.
    PENDING_LIVES = 300
    PENDING_TURNS = 1

    @property
    def pending(self):
        return self._pending

    @pending.setter
    def pending(self, act):
        """Stamped as it is set, so no call site can forget to.

        There are a dozen places that offer something for confirmation, and a
        rule about staleness that each of them has to remember is a rule that
        will be missed by the thirteenth."""
        if isinstance(act, dict):
            act = dict(act)
            act.setdefault("asked_at", time.time())
            act.setdefault("turn", self._turn)
        self._pending = act

    ACTED_WITHIN = 900        # a quarter of an hour counts as "about that"

    def _acted_on(self, label: str, kind: str = "") -> None:
        """You did something about a thing Friday mentioned.

        Only counted when it was actually mentioned recently. Replying to a
        session you have been working in all morning is not evidence that
        Friday's notifications are useful, and counting it would teach Friday
        that everything it says lands."""
        if not label:
            return
        now = time.time()
        for k, when in list(self._told.items()):
            if now - when > self.ACTED_WITHIN:
                self._told.pop(k, None)
                continue
            if k.split(":", 1)[-1] == label.strip().lower():
                learn.acted(k)
                self._told.pop(k, None)

    def _newest_messages(self):
        """Slack messages Friday has mentioned, newest first.

        By timestamp, not by position. Everything that answers "the thing you
        were just looking at" read the last entry of a dict, and a dict does not
        reorder on re-assignment, so the newest message was whichever channel
        had spoken FIRST. A reply written under your name went to the wrong
        colleague in the wrong channel, and was reported as sent."""
        try:
            rows = list(self.inbox.last.items())
        except Exception:
            return []
        rows.sort(key=lambda kv: (kv[1] or {}).get("when") or 0, reverse=True)
        return rows

    def _tell_person(self, who: str, text: str) -> bool:
        """Send a plan step to a colleague, if Friday is allowed to.

        Deliberately routed through the same posting switch as everything else.
        A plan that quietly messages your colleagues because a step said so is
        exactly the thing that makes people turn all of it off, and a plan step
        is further from your hands than a message you dictated: you approved a
        list, once, possibly hours earlier."""
        if not connectors.can_write():
            return False, "posting is switched off"
        sl = connectors.get("slack")
        try:
            if not (self._answers(sl) and hasattr(sl, "dm")):
                return False, "Slack isn't connected"
            r = sl.dm(who, text)
        except Exception as e:
            return False, str(e)[:60]
        if r.get("ok"):
            return True, r.get("who", who)
        err = r.get("error", "")
        if err == "not_sure":
            # Never quietly. A message under your name to a colleague you did
            # not name is the one thing here you cannot take back.
            return False, (f"the closest name in Slack is "
                           f"{r.get('guess', 'somebody else')}, which is not "
                           f"close enough to message")
        if err == "which_person":
            return False, ("there are several: "
                           + ", ".join(r.get("people", [])[:4]))
        return False, err or "Slack refused it"

    def _steps_from(self, body: str) -> list:
        """Turn what you said into ordered steps.

        Split on the words people actually use for sequence. A plan is written
        down before any of it runs precisely so a bad split is something you
        SEE rather than something an agent discovers halfway through."""
        text = " ".join((body or "").split())
        # "api: then deploy it" is one step for api. Splitting on "then" first
        # cut it into a bare label and an orphan, and the orphan inherited the
        # PREVIOUS agent: a third clause addressed to one agent was typed into
        # another, and a prompt consisting of nothing but "api:" was sent as
        # well. Both then counted toward "all steps done".
        text = re.sub(r"(:\s*)(?:then|and then)\s+", r"\1", text, flags=re.I)
        parts = re.split(r"\s*(?:\d+[.)]\s+|;|,\s*then\s+|\s+then\s+|,\s+and\s+"
                         r"|\.\s+(?=[A-Z])|,\s+)", text)
        return [p.strip(" .;,") for p in parts if p and len(p.strip()) > 2]

    # "api: run the migration" - who a step is for, said the way people write
    # it in a numbered list. Bounded to a short name so an ordinary sentence
    # with a colon in it ("note: this is fragile") is not read as a target.
    _FOR_RE = re.compile(r"^\s*(?:ask\s+)?([A-Za-z][\w.\-]{1,28})\s*:\s*(.+)$")
    # Words that precede a colon in ordinary writing. "note: do not touch main"
    # became a colleague called "note", and every later unlabelled step was
    # reassigned to them, so a commit meant for api ended up owed by a person
    # who does not exist and the plan reported itself as waiting on "note".
    _NOT_A_PERSON = {
        "note", "notes", "warning", "caution", "caveat", "caveats", "tip",
        "important", "todo", "fixme", "nb", "example", "eg", "ie", "reminder",
        "goal", "aim", "context", "background", "why", "how", "what", "when",
        "step", "steps", "first", "then", "finally", "also", "and", "but",
        "result", "output", "input", "requirement", "requirements", "detail",
        "details", "summary", "conclusion", "rule", "rules", "constraint"}

    def _assign(self, steps: list, default: str = "") -> list:
        """Work out who each step is for.

        The mechanic the product was pitched on is "do this now, meanwhile ask
        that", and it needs a way to say who. A step beginning with a name and a
        colon belongs to that name; everything else inherits the step before it,
        because a list under one heading is one person's list and repeating the
        name on every line is not how anybody writes.

        A name that is not a running session is treated as a PERSON. That is the
        honest reading: you would not name something Friday cannot see unless
        you meant a colleague, and it is better to ask you than to guess it was
        a typo for a session."""
        out, current = [], default
        for text in steps:
            m = self._FOR_RE.match(text)
            kind = "agent"
            if m:
                name, text = m.group(1), m.group(2).strip()
                hit, _how = self._find_how(name)
                if hit:
                    current = hit.get("label", name)
                    out.append({"text": text, "target": current,
                                "sid": hit.get("sid", ""), "kind": "agent"})
                    continue
                if name.lower() in self._NOT_A_PERSON:
                    # A label, not a target. Keep the whole line as a step for
                    # whoever the current one is, colon and all, because the
                    # words after "note:" are still the instruction.
                    out.append({"text": f"{name}: {text}", "target": current,
                                "sid": "", "kind": "agent"})
                    continue
                out.append({"text": text, "target": name, "sid": "",
                            "kind": "person"})
                # Deliberately NOT setting `current`. A name Friday does not
                # recognise should never capture the steps after it: if it is
                # unsure this is even a person, reassigning somebody else's
                # remaining work to them is the worst of both readings.
                continue
            hit, _how = self._find_how(current) if current else (None, "")
            out.append({"text": text, "target": current,
                        "sid": hit.get("sid", "") if hit else "",
                        "kind": "agent" if hit else
                                ("person" if current and not hit else "agent")})
        return out

    def _ask_for_plan(self, goal: str, target: str = "") -> dict:
        """Have the agent write the plan, then hold it for you to approve.

        The missing middle of the conductor idea. Friday could run a plan and
        Friday could have an opinion about what to start on, but the steps in
        between had to be typed by you, which meant the one part needing actual
        knowledge of the codebase was the part left to the person who had
        delegated it.

        Nothing runs off the back of this. The agent is asked to plan and told
        explicitly not to start, and the steps come back for approval before a
        single one is sent. That ordering is the whole safety story: an agent
        that plans and executes in one go is an agent you cannot say no to."""
        if not goal:
            return self._say("A plan for what?")
        # One plan at a time, same as one step at a time. Two live plans against
        # the same fleet interleave, and "run the plan" stops meaning one thing.
        if self.plans.running:
            live = plans.active() or {}
            return self._say(
                f"A plan is already running{(' on ' + live.get('target', '')) if live.get('target') else ''}. "
                f"Say \"stop the plan\" first, or \"where is the plan\" to see "
                f"where it got to.")
        name = target or self.target
        if not name:
            names = self._names_of_sessions()
            if not names:
                return self._say("Nothing is running to plan with.")
            return self._offer(
                "Which session should work it out? " + ", ".join(names[:8]),
                yes=lambda n=names[0], g=goal: self._ask_for_plan(g, n),
                again=lambda t, g=goal: self._ask_for_plan(g, t), pick=True)
        hit, _how = self._find_how(name)
        if not hit:
            return self._no_session(name,
                                    lambda n, g=goal: self._ask_for_plan(g, n))
        if not agents.can_conduct(hit):
            return self._say(f"I can read {hit.get('label', name)} but I can't "
                             f"ask it anything: it runs in its own app.")
        path, sid = hit.get("path", ""), hit.get("sid", "")
        label = hit.get("label", name)
        prompt = plans.ASK_FOR_PLAN.format(n=plans.MAX_STEPS, task=goal)
        try:
            marker = replies.mark(path)
        except Exception:
            marker = ""
        if not actions.send_to_session(sid, prompt):
            return self._say(f"I couldn't reach {label}.")

        def _collect():
            answer = ""
            try:
                answer = replies.wait_for_reply(path, marker, timeout=240) or ""
            except Exception:
                answer = ""
            if not answer:
                self.announce(f"{label} hasn't come back with a plan yet. Say "
                              f"\"say more\" to see where it got to.")
                return
            steps = plans.steps_from_answer(answer)
            if len(steps) < 2:
                # Refusing beats manufacturing steps out of prose. What it
                # actually said is more useful than a plan nobody wrote.
                self.announce(f"{label} answered but not with a numbered plan, "
                              f"so I haven't written anything down. It said: "
                              f"{answer[:300]}")
                return
            pid = plans.create(goal[:60], label, steps, sid=sid)
            if not pid:
                self.announce("I couldn't write that plan down.")
                return
            self._plan_id = pid
            listed = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            self.announce(
                f"{label}'s plan for {goal[:60]}. Nothing has run yet:\n{listed}"
                f"\n\nSay \"run the plan\" to do them in order, one at a time.",
                items=[{"sid": sid, "label": label, "kind": "plan"}])
        import threading
        threading.Thread(target=_collect, daemon=True).start()
        return self._say(f"Asked {label} to work out a plan for {goal[:60]}, "
                         f"and told it not to start. I'll bring you the steps "
                         f"to approve.")

    def _make_plan(self, target: str, body: str) -> dict:
        """Write it down and show it. Nothing runs yet."""
        raw = self._steps_from(body)
        steps = self._assign(raw, target or self.target)
        # A plan naming its own targets does not need one asked for, which is
        # what makes "api: do this; web: do that" a single sentence rather than
        # a conversation.
        named = [st for st in steps if st.get("target")]
        if len(named) == len(steps) and len({st["target"] for st in steps}) > 1:
            return self._show_plan(steps, target or "the fleet", "")
        steps = raw
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
                again=lambda t, b=body: self._make_plan(t, b), pick=True)
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, lambda n: self._make_plan(n, body))
        return self._show_plan(self._assign(steps, hit.get("label", name)),
                               hit.get("label", name), hit.get("sid", ""))

    def _show_plan(self, steps: list, target: str, sid: str) -> dict:
        """Write it down and read it back. Still nothing has run.

        Reading it back BY TRACK rather than as one numbered list, because with
        several agents the order of the list is not the order things happen, and
        showing it as one sequence would be describing something that is not
        going to occur."""
        pid = plans.create(f"{len(steps)} steps", target, steps, sid=sid)
        if not pid:
            return self._say("I couldn't write that plan down.")
        self._plan_id = pid
        plan = plans.get(pid)
        by = {}
        for st in plan["steps"]:
            by.setdefault(st.get("target") or target, []).append(st)
        lines = []
        for who, rows in by.items():
            kind = rows[0].get("kind", "agent")
            head = f"{who}" + (" (a person, so I'll ask and wait)"
                               if kind == "person" else "")
            lines.append(head + ":")
            lines += [f"  {i + 1}. {r['text']}" for i, r in enumerate(rows)]
        how = ("in order, one at a time, stopping if it asks you anything"
               if len(by) == 1 else
               f"all {len(by)} at once, one step each, and if one of them stops "
               f"the others carry on")
        return self._say(
            f"Here's the plan. Nothing has run yet:\n" + "\n".join(lines)
            + f"\n\nSay \"run the plan\" and I'll do them {how}.")

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
        # A step that FAILED is put back before starting, or "run the plan to
        # retry those" does nothing at all and the steps queued behind the
        # failure never run either. A retry that silently does nothing is worse
        # than no retry, because you believe the work is queued.
        again = plans._retry(p["id"])
        p = plans.get(p["id"])
        left = [s for s in p["steps"] if s["state"] == plans.PENDING]
        self.plans.start(p["id"])
        if again:
            which = ", ".join(f"{s.get('target') or 'a session'} "
                              f"(step {s['seq'] + 1})" for s in again)
            return self._say(f"Running it again, {len(left)} steps, including "
                             f"the one that failed: {which}.")
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
            # Read without consuming: this path is a question, not a poll, and
            # marking the news as seen here means the feed never brings it up.
            for it in feeds.SentryFeed().poll(remember=False):
                candidates.append((1, "look at production", it["text"]))
                break
        except Exception:
            pass

        # 3. something of yours that is broken
        try:
            gh = connectors.get("github")
            if self._answers(gh):
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
            # Every tracker, not `_tracker()`. That one returns None when two
            # are connected and you have not said which, because a ticket has
            # to be FILED somewhere; suggesting what to work on has no such
            # constraint, and using it here meant connecting a second tracker
            # silently stopped Friday ever proposing a ticket.
            rows = []
            for c in trackers.available():
                rows += [r for r in trackers.issues(c, 5) if not r.get("error")]
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
        # The reason is a sentence too, and it followed a full stop in
        # lowercase: "Answer web. it has been stuck 10 minutes on: ...".
        why = pick[2][:1].upper() + pick[2][1:] if pick[2] else ""
        out = [f"{head}. {why}" if why else f"{head}."]
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
            # Say WHICH way it failed. "When?" in answer to "yesterday at 4" is
            # baffling: you did say when, and Friday read it and refused. The
            # two cases need different things from you.
            if when.names_a_day(said):
                return self._say(
                    "I read a day in that but not one I can use: it may have "
                    "gone already, or it isn't a date I understand. Give me a "
                    "weekday and a time, like \"Thursday at 4\".")
            return self._say("When? Tell me a day and a time, like \"Thursday "
                             "at 4\", and I'll put it in.")
        # What it is ABOUT: whoever asked, if anybody did.
        title = "Meeting"
        try:
            for _cid, m in self._newest_messages():
                title = f"{m['who']} ({m['where']})"
                break
        except Exception:
            pass
        self.pending = {"kind": "event", "title": title, "at": stamp,
                        "reads": reads}
        return self._say(f"Put \"{title}\" in for {reads}?", needs_confirm=True)

    @staticmethod
    def _answers(c) -> bool:
        """Whether a connector is usable, without trusting it not to throw.

        `ready()` reaches the network for most of these, so it can raise for
        every ordinary reason a network call raises. Called bare, one unhappy
        connector took down the whole reply, including the replies you would
        ask precisely because something was wrong."""
        try:
            return bool(c) and bool(c.ready())
        except Exception:
            return False

    def _tracker(self, want: str = ""):
        """Whichever tracker YOU use, which is not for Friday to decide.

        This used to be a hard-coded order, jira then linear then github, which
        quietly chose for everybody: with a work Jira and a side-project Linear
        connected, the side-project ticket went to work. See `trackers.py`; the
        rule now is that Friday uses the one you named, or the one you saved, or
        the only one there is, and otherwise asks."""
        return trackers.get(want)

    def _set_tracker(self, which: str) -> dict:
        """Say once where tickets go, and stop being asked."""
        which = (which or "").strip().lower()
        live = trackers.names()
        if which not in live:
            c = connectors.get(which)
            if c and trackers.is_tracker(c):
                return self._say(
                    f"{trackers.describe(c)} isn't connected yet, so I can't "
                    f"file there. " + connectors.hint(c))
            return self._say("I don't track tickets in that. I can use: "
                             + (", ".join(live) or "nothing yet") + ".")
        trackers.prefer(which)
        c = trackers.get(which)
        return self._say(f"Tickets go to {trackers.describe(c)} from now on. "
                         f"Say \"use <other> for tickets\" to change it.")

    def _ask_which_tracker(self, then) -> dict:
        """Make the choice once and remember it.

        A ticket filed in the wrong tracker is worse than no ticket, because
        everybody believes it exists. So this is one of the few places Friday
        stops rather than picking the likelier option."""
        live = trackers.names()
        pretty = [trackers.describe(c) for c in trackers.available()]
        return self._offer(
            "You have more than one tracker connected: "
            + ", ".join(pretty)
            + ". Which should I use? I'll remember it.",
            yes=lambda n=live[0]: (trackers.prefer(n), then(n))[1],
            again=lambda t: self._pick_tracker(t, then),
            # A yes here picked alphabetically and PERSISTED it, so every later
            # ticket went to a board nobody chose.
            pick=True)

    def _pick_tracker(self, said: str, then) -> dict:
        name = nearest.pick((said or "").strip().lower(), trackers.names())
        if not name:
            return self._say("I don't have a tracker called that. I have: "
                             + ", ".join(trackers.names()) + ".")
        trackers.prefer(name)
        return then(name)

    def _file_ticket(self, project: str, summary: str,
                     where: str = "") -> dict:
        """File a ticket, from what you said or from what you were just reading.

        This is the moment a Slack thread becomes work, which is the whole point
        of reading Slack in the first place. Nothing is filed without you seeing
        the exact wording, because a ticket is something colleagues read with
        your name on it."""
        ji = self._tracker(where)
        if ji is None and trackers.ambiguous():
            return self._ask_which_tracker(
                lambda n, p=project, sm=summary: self._file_ticket(p, sm, n))
        if ji is None:
            if where:
                return self._say(f"I don't have {where} connected. I have: "
                                 + (", ".join(trackers.names()) or "nothing")
                                 + ".")
            hint = connectors.get("jira")
            return self._say("No ticket tracker is connected. "
                             + (connectors.hint(hint) if hint else ""))
        if not summary:
            # Fall back to what you were just looking at, so "file a ticket"
            # right after reading a thread does the obvious thing.
            last = None
            for _cid, m in self._newest_messages():
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
                        "summary": summary, "where": ji.name}
        # Name the tracker as well as the project. With four of them possible,
        # "File this?" is not enough to approve: you are approving WHERE as much
        # as WHAT, and the wrong tracker is the failure that hides itself.
        place = trackers.describe(ji) + (f", {project}" if project else "")
        return self._say(f"File this in {place}?\n\n{summary[:300]}",
                         needs_confirm=True)

    def _move_ticket(self, key: str, to: str, where: str = "") -> dict:
        ji = self._tracker(where)
        if ji is None and trackers.ambiguous():
            return self._ask_which_tracker(
                lambda n, k=key, t=to: self._move_ticket(k, t, n))
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
        self.pending = {"kind": "move", "key": key, "to": to,
                        "where": ji.name}
        return self._say(f"Move {key} to {to} in {trackers.describe(ji)}?",
                         needs_confirm=True)

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

    def _send_draft(self, where: str = "") -> dict:
        """Send the thing you were just shown. Never anything else.

        There is no path from "reply to Sam saying yes" straight to a
        message in Slack. Friday writes it, you read it, and only then does
        "send it" mean anything."""
        d = getattr(self, "_last_draft", None)
        if not d:
            return self._say("There's nothing drafted. Say \"draft a reply\" "
                             "and I'll write one for you to check first.")
        # A destination you named and Friday ignored is how a message goes to
        # the colleagues you had just redirected it away from. The name was
        # captured and thrown away, and the draft always went to its own
        # channel.
        want = (where or "").strip().lstrip("#").lower()
        here = (d.get("where") or "").strip().lstrip("#").lower()
        if want and here and want != here:
            return self._say(
                f"That draft is addressed to {d.get('where')}, not "
                f"#{want.lstrip('#')}. I won't quietly move it. Say \"draft a "
                f"reply\" again while reading #{want.lstrip('#')}, or \"send "
                f"it\" to post it where it was written for.")
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
        for cid, m in self._newest_messages():
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
            held = [text for _when, text, _src in self.budget.held(since=since,
                                                                   clear=False)]
        except Exception:
            held = []
        if held:
            news = news + [{"text": t, "src": src}
                           for _w, t, src in self.budget.held(since=since,
                                                              clear=False)]
        if not news:
            waiting = self._waiting()
            if waiting:
                return self._say("Nothing new was said, but "
                                 + _join([r["label"] for r in waiting])
                                 + f" {_is(len(waiting))} waiting on you.")
            return self._say("Nothing since you last spoke.")
        try:
            self.budget.held(since=since)        # now you have been told
        except Exception:
            pass
        return self._say(self._digest(news))

    def _digest(self, news: list) -> str:
        """An hour away, summarised by who rather than by line.

        Twenty-two notes from four sessions is four things that happened, not
        twenty-two. Listing them one per line is technically complete and
        useless: you cannot act on a list you will not read, and the whole
        reason for asking is that you were not there.

        Only the latest from each is quoted, because the older ones were
        superseded by it. What you want after an hour away is where everything
        stands NOW, not a replay."""
        by = {}
        for h in news:
            text = h.get("text", "")
            who = (h.get("src") or "").strip() or text.split(" says")[0]
            who = who.split(":")[0].strip()[:28] or "something"
            by.setdefault(who, []).append(text)
        if len(by) == 1 and len(news) <= 8:
            lines = [h["text"] for h in news]
            head = ("One thing happened:" if len(news) == 1
                    else f"{len(news)} things happened:")
            return head + "\n- " + "\n- ".join(lines)
        rows = sorted(by.items(), key=lambda kv: -len(kv[1]))
        lines = []
        for who, texts in rows[:6]:
            latest = texts[-1]
            more = (f" (and {len(texts) - 1} earlier)" if len(texts) > 1 else "")
            lines.append(f"{latest}{more}")
        tail = ""
        if len(rows) > 6:
            tail = f"\nAnd {len(rows) - 6} others."
        return (f"{len(news)} things while you were away, from "
                f"{len(rows)} sources. The latest from each:\n- "
                + "\n- ".join(lines) + tail)

    def _waiting(self) -> list:
        """Everyone who cannot continue without you, oldest first.

        Derived from the live fleet every time rather than kept as a list, and
        that is the whole design of the queue. A stored queue goes stale in ways
        you cannot see: the agent times out, you answer it in its own terminal,
        it gives up and moves on, somebody else replies. Every one of those
        leaves a queue entry that is still there and no longer true, and Friday
        would go on offering you a question nobody is asking. The fleet cannot
        be stale, because it is what is actually happening."""
        try:
            rows = [r for r in fleetcache.snapshot().values()
                    if (r.get("question") or r.get("permission"))]
        except Exception:
            return []
        # Oldest first: whoever has been stuck longest has cost the most.
        rows.sort(key=lambda r: r.get("mtime") or 0)
        # Every display of a blocked session comes through here, so the naming
        # guarantee is applied here too. fleetcache makes the same promise for
        # the live fleet; this covers rows that arrive from anywhere else.
        for r in rows:
            if not (r.get("label") or "").strip():
                sid = (r.get("sid") or "").strip()
                r["label"] = f"session {sid[:8]}" if sid else "an unnamed session"
        return rows

    def _bare_answer(self, word: str):
        """A bare yes or no, when an agent is the thing waiting for it.

        One waiting session gets it, because you answered a question only it
        asked. Several is a question back, always: "yes" carries no clue about
        which one you meant, and a yes typed into the wrong agent is not a
        message, it is permission."""
        rows = self._waiting()
        if not rows:
            return None
        if len(rows) > 1:
            lines = [f"  {r.get('label')}: "
                     f"{(r.get('question') or r.get('permission') or '')[:80]}"
                     for r in rows[:5]]
            return self._say(
                f"{len(rows)} sessions are waiting, so I don't know who that "
                f"\"{word}\" is for:\n" + "\n".join(lines)
                + "\n\nSay which, or \"tell <name> " + word + "\".")
        r = rows[0]
        label = (r.get("label") or "").strip()
        # A bare yes goes straight through with no confirmation, which is right
        # when you recognise the name. A session Friday can only call "session
        # 4f2a" is one you may never have seen announced, and a yes typed into
        # an agent is not a message, it is authority. So the name it fell back
        # to earns one extra beat, and nothing more: the question is shown and
        # the yes is confirmed rather than refused.
        if label.startswith("session ") or label == "an unnamed session":
            q = (r.get("question") or r.get("permission") or "").strip()
            self.pending = {"kind": "tell", "sid": r.get("sid", ""),
                            "label": label, "message": word, "await": False,
                            "path": r.get("path", "")}
            return self._say(
                f"{label} has no name I can show you, so before I answer for "
                f"you: it is asking \"{q[:120]}\". Send \"{word}\"?",
                needs_confirm=True)
        if not actions.send_to_session(r.get("sid", ""), word):
            return self._say(f"I couldn't reach {label}.")
        self.target = label
        self._acted_on(label)
        return self._say(f"Told {label} {word}.",
                         action={"kind": "tell", "sid": r.get("sid", ""),
                                 "undo": True})

    def _next_waiting(self, besides: str = "") -> str:
        """The line offering whoever is still stuck after this one.

        Answering one of five and hearing nothing about the other four is how a
        queue silently becomes a pile. This is said at the moment it is useful,
        which is immediately after you have dealt with one."""
        rest = [r for r in self._waiting() if r.get("sid") != besides]
        if not rest:
            return ""
        first = rest[0]
        label = first.get("label") or "another session"
        q = (first.get("question") or first.get("permission") or "").strip()
        more = (f" ({len(rest) - 1} more after that)" if len(rest) > 1 else "")
        # Deliberately NOT setting self.target. Mentioning who is still waiting
        # must not change who "it" means: this line is printed right after you
        # addressed somebody else, and re-pointing the pronoun sent the next
        # "ask it to also run the tests" into a different agent that was
        # mid-question. Saying something about a session is not addressing it.
        return f"Next{more}: {label} is asking {q[:120]}"

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
        # The only signal you give deliberately, so it counts for more than a
        # run of notifications you happened not to answer.
        for kind in ("blocked", "spoke"):
            (learn.never_again if on else learn.forget)(f"{kind}:{target.lower()}")
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

    def _ask_all(self, tail: str, joiner: str = "",
                 approved: bool = False, only: list = None) -> dict:
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
        if only is not None:
            here = {r["sid"] for r in rows}
            gone = [sid for sid in only if sid not in here]
            rows = [r for r in rows if r["sid"] in set(only)]
            if gone and not rows:
                return self._say("Those sessions have all closed, so I didn't "
                                 "send it anywhere.")
        if not rows:
            return self._say("Nothing is running, so there is nobody to ask.")
        question = tail if approved else self._as_question(tail, joiner)
        # One sentence writing into every agent at once, in words Friday
        # rephrased, is the largest single action available here and it had no
        # confirmation at all. Worse, _as_question turns a statement into a
        # question, so "tell everyone standup is at 10" arrived in every session
        # as "standup is at 10?", which the agents then answer.
        if not approved:
            names = ", ".join((r.get("label") or r.get("sid", ""))[:20]
                              for r in rows[:8])
            # The sids you were shown, not whoever is running when you say
            # yes. A session that started in between joined a broadcast you
            # approved for two, which is the confirmation describing one thing
            # and a different thing happening.
            self.pending = {"kind": "askall", "question": question,
                            "sids": [r["sid"] for r in rows],
                            "names": names}
            return self._say(
                f"That goes to all {len(rows)}: {names}.\nEach of them gets, "
                f"exactly:\n\n{question}\n\nSend it?", needs_confirm=True)
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
                again=lambda t: self._watch(t), pick=True)
        row = next((r for r in fleetcache.snapshot().values()
                    if (r.get("label") or "") == target), None)
        if not row:
            return self._no_session(target, self._watch)
        self.target = target
        # Already stopped is an answer, not something to wait an hour for. Say
        # so now rather than watching a session that is not going to transition.
        if row.get("status") != "working":
            q = (row.get("question") or "").strip()
            if q:
                return self._say(f"{target} isn't working, it's waiting on "
                                 f"you: {q}")
            return self._say(f"{target} is already idle, so there's nothing to "
                             f"wait for. I'll watch it anyway in case it starts "
                             f"again.")
        self._watch_until_idle(row["sid"], target)
        return self._say(f"Watching {target}. I'll tell you when it stops.")

    def _watch_until_idle(self, sid: str, label: str,
                          timeout: float = 3600) -> None:
        import threading

        def _wait():
            end = time.time() + timeout
            # False, not True. Starting at True meant the first poll of a
            # session that was ALREADY idle counted as a transition, so "tell me
            # when api is done" announced "api has finished" three seconds
            # later about work that never ran. Nothing is reported until it has
            # actually been seen working.
            was_working = False
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
        if not self._answers(gh):
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
            if self._answers(sl) and hasattr(sl, "channel_names"):
                found = nearest.best_window(said, sl.channel_names(40))
                if found:
                    return self._read_channel_named(found, said)
        if not (name or "").strip():
            sl = connectors.get("slack")
            if not self._answers(sl):
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
        if not self._answers(sl):
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
        if not self._answers(gh):
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
        if not self._answers(gh):
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
        if not self._answers(gh):
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
        if not self._answers(gm):
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
        if not self._answers(se):
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

    def _tickets(self) -> dict:
        """Everything assigned to you, everywhere it is written down.

        Reading is the one place Friday should NOT make you choose a tracker.
        Writing has to pick one, because a ticket goes somewhere; reading has no
        such constraint, and a person with work Jira and a side-project Linear
        wants both when they ask what is on their plate. Being the single place
        is the entire promise, and it cannot be true if the answer depends on
        which tracker Friday felt like consulting."""
        live = trackers.available()
        if not live:
            ji = connectors.get("jira")
            return self._say("No ticket tracker is connected. "
                             + connectors.hint(ji))
        blocks, broke = [], []
        for c in live:
            rows = trackers.issues(c, 8)
            if rows and rows[0].get("error"):
                # A tracker that errors is not a tracker with no work, and
                # reporting the two the same way is how you miss a day of it.
                broke.append(f"{trackers.describe(c)} ({rows[0]['error'][:60]})")
                continue
            if not rows:
                continue
            lines = [f"{r.get('key', '')} [{r.get('status', '')}]: "
                     f"{(r.get('summary') or '')[:80]}" for r in rows]
            head = trackers.describe(c) if len(live) > 1 else \
                trackers.describe(c)
            blocks.append(f"{head}:\n- " + "\n- ".join(lines))
        if not blocks and not broke:
            where = " or ".join(trackers.describe(c) for c in live)
            return self._say(f"Nothing open assigned to you in {where}.")
        out = "\n\n".join(blocks)
        if broke:
            # Joined, not appended: with every tracker erroring there are no
            # blocks, and appending left the reply starting with two blank
            # lines.
            note = ("I couldn't reach " + ", ".join(broke)
                    + (", so there may be more." if blocks else "."))
            out = (out + "\n\n" + note) if out else note
        return self._say(out)

    def _slack(self, query: str) -> dict:
        sl = connectors.get("slack")
        if not self._answers(sl):
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
        # "Opening", not "Reopened". Nothing here waits for the session to
        # come back: the AppleScript returns as soon as Terminal accepts the
        # command, so the past tense was a claim about something that had not
        # happened yet and might not.
        return self._say(f"Opening {hit['project']} in a new window. Say "
                         f"\"what's running\" in a moment to check it came "
                         f"back."
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
        """Bring a window to the front.

        Reversible in itself, which is why an EXACT name goes straight through.
        A guessed one does not, and the reason is not the window: opening a
        session makes it the target, and the target is where your next bare
        message goes. "Show me the money" opened a session called moneyman, and
        the sentence after it would have been typed into moneyman."""
        if not name:
            names = self._session_names()
            return self._say("Which one? " + (", ".join(names) if names
                                              else "nothing is running."))
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, self._propose_open)
        if how in ("maybe", "fuzzy", "ambiguous"):
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
        # If the sentence already gave a name that IS a session, exactly, keep
        # it. Re-splitting looks for the best-scoring name anywhere in the
        # sentence, which meant a session called "test" matched the word
        # "tests" at the end of "tell friday to run the tests" and quietly
        # took the message away from friday. Common words do end up as session
        # names, because they end up as directory names.
        #
        # Exact only, deliberately. This function exists because "tell voice
        # bridge ..." split at the first space and "voice" resolves close
        # enough to voicebridge to be acted on; that case must still re-split,
        # and it does, because "voice" is not a session name.
        said_name = (name or "").strip().lower()
        exact_name = (said_name and any(said_name == (n or "").strip().lower()
                                        for n in names))
        found, i, j, _sc = ("", 0, 0, 0.0)
        if exact_name:
            # Locate the name where it actually appears, then carry on through
            # the same logic. Returning early here skipped the phrase handling
            # below, so "ask api FOR the test results" stopped becoming "Give me
            # the test results" and sent the bare fragment instead.
            toks = [w.lower().strip(".,?!:;") for w in said.split()]
            parts = said_name.split()
            for at in range(len(toks) - len(parts) + 1):
                if toks[at:at + len(parts)] == parts:
                    found, i, j = name, at, at + len(parts)
                    break
        if not found:
            found, i, j, _sc = nearest.best_span(said, names)
            # Only if it is at or before where the sentence PUT the name. The
            # search looks anywhere, so "ask sam to review the api changes"
            # found "api" five words in, re-anchored onto it, and sent the
            # running api session the word "changes". The name you said is not
            # a session, and the right answer is to say so; a session mentioned
            # later in the sentence is part of what you are talking ABOUT.
            #
            # At or before, not exactly at, because the case this fallback
            # exists for is "tell voice bridge ...", where the parser cut at
            # the first space and the real name starts in the same place.
            if found and i > self._name_at(said, name):
                found = ""
        if not found:
            return name, message, want, False
        # Whether you actually SAID the name, or Friday worked it out. Resolving
        # "ap" to "api" and then treating it as though you had typed "api"
        # skipped the confirmation that stops a wrong instruction being written
        # into a running agent.
        heard = " ".join(said.split()[i:j])
        exact = exact_name or nearest.flat(heard) == nearest.flat(found)
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

    # Words that are not an instruction on their own. "tell api to" left the
    # dangling "to" as the message and sent it, so a sentence you stopped
    # halfway through became a prompt typed into a running agent.
    _NOT_AN_INSTRUCTION = {
        "to", "that", "the", "a", "an", "it", "and", "or", "please", "then",
        "this", "for", "about", "with", "of", "in", "on", "so", "just", "ok"}
    # A second target inside the message. "tell api to deploy and tell web to
    # build" sent api the words "deploy and tell web to build", which api reads
    # as an instruction to go and do something to web.
    _SECOND_TARGET_RE = re.compile(
        r"\b(?:and\s+|then\s+|,\s*)?(?:tell|ask)\s+([\w.\-]{2,30})\s+"
        r"(?:to\s+)?(.+)$", re.I)
    # "ask api and web to run the tests": the second name is not introduced by
    # another verb, so the pattern above cannot see it. api received the words
    # "and web to run the tests", which reads to a coding agent as an
    # instruction about another agent, and web got nothing at all.
    _AND_TARGET_RE = re.compile(
        r"^(?:and|,)\s+([\w.\-]{2,30})(?:\s+sessions?)?\s+(?:to\s+)?(.+)$",
        re.I)

    def _propose_tell(self, name: str, message: str,
                      want_answer: bool = False, said: str = "") -> dict:
        # Quotes around a name are how people write one that sounds ordinary,
        # and they stopped it being recognised at all.
        name = (name or "").strip().strip("'\"`\u2018\u2019\u201c\u201d")
        name, message, want_answer, said_exactly = self._resplit(
            said, name, message, want_answer)
        message = (message or "").strip()
        bare = [w for w in re.findall(r"[\w']+", message.lower())
                if w not in self._NOT_AN_INSTRUCTION]
        if not bare:
            who = name or self.target or "it"
            return self._say(f"What should I say to {who}? I heard the name "
                             f"but not the message.")
        split = self._split_instructions(name, message)
        if split is not None:
            return split
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
                                                       want_answer),
                    pick=True)
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
        if how == "ambiguous":
            # Naming it exactly is normally your confirmation, and here it
            # cannot be, because the name does not identify one session. Say
            # what distinguishes them: what each is doing is the only thing you
            # can tell them apart by out loud.
            others = [r for r in self._same_named(hit.get("label", name))]
            lines = []
            for r in others[:4]:
                doing = (r.get("question") or r.get("topic") or
                         r.get("status") or "").strip()
                when = connectors.when(r.get("mtime") or 0)
                lines.append(f"  {doing[:70] or 'nothing I can see'} ({when})")
            return self._say(
                f"There are {len(others)} sessions called "
                f"{hit.get('label', name)}:\n" + "\n".join(lines)
                + f"\n\nI'd send \"{message}\" to the most recent one. "
                  f"Yes to do that, or say which.", needs_confirm=True)
        return self._say(f'Did you mean {hit.get("label", name)}? '
                         f'I\'ll send "{message}".', needs_confirm=True)

    def _name_at(self, said: str, name: str) -> int:
        """Which word the sentence put the name at, or 1 if it cannot tell.

        1 rather than 0 because these sentences open with a verb ("tell api
        ..."), so the name slot is the second word, and a fallback that allowed
        position 0 would let anything through."""
        want = (name or "").strip().lower().strip("'\"`")
        if not want:
            return 1
        toks = [w.lower().strip(".,?!:;'\"`") for w in said.split()]
        first = want.split()[0]
        for at, w in enumerate(toks):
            if w == first:
                return at
        return 1

    def _split_instructions(self, name: str, message: str):
        """Notice a second instruction hiding inside the first.

        "tell api to deploy and tell web to build" used to send api the words
        "deploy and tell web to build". That is not a truncation, it is worse: a
        coding agent reading "tell web to build" will try to do something about
        web, so the second half does not vanish, it gets carried out by the
        wrong agent.

        Only fires when the second name is a session that actually exists, so
        "tell api to build and tell me when it's done" is left alone: "me" is
        not a session, and that sentence means one thing."""
        m = self._SECOND_TARGET_RE.search(message)
        both = False
        if not m:
            m = self._AND_TARGET_RE.match(message)
            both = bool(m)
        if not m:
            return None
        other, rest = m.group(1).strip(), m.group(2).strip()
        hit, _how = self._find_how(other)
        if not hit or not rest:
            return None
        # strip(" ,.and") strips CHARACTERS, so "deploy" came out as "eploy".
        # The conjunction has to go as a word.
        first = re.sub(r"[\s,]*\b(?:and|then)\s*$", "",
                       message[:m.start()]).strip(" ,.")
        if both:
            # "ask api and web to run the tests" is ONE instruction for two
            # agents, not two different ones, so they both get the same words.
            first = rest
        if not first:
            return None
        self.pending = {"kind": "two", "first": {"name": name, "text": first},
                        "second": {"name": hit.get("label", other),
                                   "sid": hit.get("sid", ""), "text": rest}}
        return self._say(
            f"That's two instructions. Send them separately?\n"
            f"  {name}: {first}\n"
            f"  {hit.get('label', other)}: {rest}", needs_confirm=True)

    def _same_named(self, label: str) -> list:
        """Every session sharing a name, newest first."""
        want = (label or "").strip().lower()
        try:
            rows = [r for r in fleetcache.snapshot().values()
                    if (r.get("label") or "").strip().lower() == want]
        except Exception:
            return []
        rows.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
        return rows

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
                # The tracker you APPROVED, not whichever one resolves now.
                # Re-deciding here could file it somewhere you did not agree to,
                # which is the exact failure the confirmation exists to prevent.
                ji = self._tracker(act.get("where", ""))
                if ji is None:
                    return self._say("That tracker isn't connected any more, so "
                                     "nothing was filed.")
                r = ji.create(act["summary"], project=act.get("project", ""))
                if r.get("ok"):
                    return self._say(f"Filed {r['key']}. {r.get('url', '')}")
                if r.get("error") == "which_project":
                    opts = ", ".join(r.get("projects") or []) or "none I can see"
                    return self._say(f"Which project? {opts}")
                return self._say(
                    f"It didn't file: "
                    f"{r.get('error') or (trackers.describe(ji) + ' said no')}. "
                    f"Nothing was created.")
            if act["kind"] == "move":
                ji = self._tracker(act.get("where", ""))
                if ji is None:
                    return self._say("That tracker isn't connected any more, so "
                                     "nothing moved.")
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
            if act["kind"] == "askall":
                return self._ask_all(act["question"], approved=True,
                                     only=act.get("sids") or [])
            if act["kind"] == "two":
                # Checked against the fleet as it is NOW. A session can close
                # between the offer and the yes, and the stored sid would be
                # sent to regardless and reported as sent: a claim about work
                # that did not happen, which is the one thing that must never
                # be said.
                try:
                    live = set(fleetcache.snapshot())
                except Exception:
                    live = set()
                out = []
                for part in (act["first"], act["second"]):
                    sid = part.get("sid")
                    if not sid:
                        hit, _how = self._find_how(part["name"])
                        sid = hit.get("sid", "") if hit else ""
                    if not sid or (live and sid not in live):
                        out.append(f"{part['name']}: it has closed, so nothing "
                                   f"went there")
                        continue
                    ok = actions.send_to_session(sid, part["text"])
                    out.append(f"{part['name']}: "
                               + ("sent" if ok else "couldn't reach it"))
                return self._say("; ".join(out) + ".")
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
                if ok:
                    self._acted_on(act.get("label", ""))
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
                if not ok:
                    return self._say(f"I couldn't reach {label}.",
                                     action={"kind": "tell",
                                             "sid": act["sid"], "undo": False})
                # Whoever else is still stuck, said at the one moment it is
                # useful: you have just dealt with one. This is the main path
                # for answering an agent, and it was the one place the queue
                # was not offered.
                nxt = self._next_waiting(besides=act["sid"])
                return self._say(f"Sent it to {label}."
                                 + (f"\n{nxt}" if nxt else ""),
                                 action={"kind": "tell", "sid": act["sid"],
                                         "undo": True})
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

    def _learned(self, about: str = "", forget: bool = False) -> dict:
        """What Friday thinks it has worked out about you, and how to undo it.

        The whole reason a learned attention model is acceptable here rather
        than creepy: it is counts of two things, you can read them, and you can
        delete them. The day it decides wrongly that you do not care about
        something, the only acceptable answer is a way to say otherwise."""
        if forget:
            if not about:
                learn.forget()
                return self._say("Forgotten. I'll go back to telling you about "
                                 "everything until I have a reason not to.")
            hits = [r["key"] for r in learn.summary(limit=50)
                    if about.strip().lower() in r["key"]]
            for k in hits:
                learn.forget(k)
            return self._say(
                f"Forgotten what I'd learned about {about}."
                if hits else f"I hadn't learned anything about {about}.")
        if about:
            hits = [r for r in learn.summary(limit=50)
                    if about.strip().lower() in r["key"]]
            if not hits:
                return self._say(f"Nothing I've learned is making me quieter "
                                 f"about {about}. If you're not hearing about "
                                 f"it, it's the rules rather than a habit.")
            r = hits[0]
            return self._say(
                f"{r['why']} So I've been treating it as less urgent. Say "
                f"\"forget what you learned about {about}\" to undo that.")
        rows = learn.summary()
        if not rows:
            return self._say("Nothing yet. I need to mention something a few "
                             "times before what you do about it means anything.")
        lines = []
        for r in rows:
            kind, _, name = r["key"].partition(":")
            verdict = ("you act on this" if r["score"] > 0.3
                       else "you don't act on this" if r["score"] < -0.3
                       else "no strong view")
            lines.append(f"{name}: {verdict}. {r['why']}")
        return self._say(
            "What I've picked up:\n- " + "\n- ".join(lines)
            + "\n\nIt only ever makes me quieter, never louder, and never "
              "about a session that's waiting on you. Say \"forget what you "
              "learned\" to clear it.")

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

    # "the redis one", "that one", "the second one", "do that". A reference to
    # something, and the something has to exist. Bounded to short utterances,
    # because a long sentence carries enough of its own meaning to be answered
    # even when it also contains "that one".
    _POINTS_AT_RE = re.compile(
        r"\b(?:the\s+\w+\s+one|the\s+(?:first|second|third|last|other)\b"
        r"|that\s+one|this\s+one|the\s+one)\b|^\s*(?:do|use|try)\s+"
        r"(?:that|it|this)\s*[.!?]?$", re.I)

    def _pointing_at_nothing(self, text: str):
        """Catch a reference whose referent does not exist.

        "Use the redis one" means nothing on its own. It means something when an
        agent has just offered you a choice, or when Friday has just listed
        things. With nothing in play it used to fall through to the local model,
        which would answer it as a general question and produce confident prose
        about a machine it cannot see. That is the single worst failure mode
        available here: an assistant that invents an answer about YOUR system is
        worse than one that says it does not know, because you cannot tell the
        difference from the reply.

        Deliberately narrow. If anything is waiting, or Friday just spoke about
        something, the reference has a plausible target and the normal routing
        handles it. This only fires when there is genuinely nothing to point
        at."""
        if len(text.split()) > 12 or not self._POINTS_AT_RE.search(text):
            return None
        if self.target or self._waiting():
            return None
        try:
            if fleetcache.snapshot():
                return None      # something is running; routing can try
        except Exception:
            pass
        return self._say(
            "I don't know what that refers to. Nothing is running and nothing "
            "has asked you anything, so there's no \"one\" for me to pick. "
            "Say what you'd like me to do, or \"what's running\" to see "
            "what's there.")

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
                if self._answers(sl) and hasattr(sl, "channel_names"):
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
            "Never guess what an unfamiliar name or id means. "
            # This reply cannot act. Anything that gets DONE is done by code
            # before the model is ever called, so a model that says "I'll use
            # Redis instead" has promised something nobody will carry out, and
            # the user has no way to tell that from a real confirmation. Asked
            # "use the redis one" with nothing running, it answered exactly
            # that.
            "You cannot take any action in this reply: you are not able to run "
            "anything, change anything, or send anything from here. Never say "
            "you will do something, are doing something, or have done "
            "something. If asked to do something, say what you would need in "
            "order to do it, or that you cannot. "
            "If the user refers to a thing (\"the redis one\", \"that\", "
            "\"the second one\") and no fact above says what it is, say you "
            "do not know what it refers to; never pick one. "
            "Answer in one or "
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
        same = [r for r in rows if (r.get("label") or "").lower() == q]
        if len(same) == 1:
            return same[0], "exact"
        if len(same) > 1:
            # Two agents in the same directory get the same name, which is not
            # rare: it is what happens the moment you open a second one on the
            # same project. Picking either is a message typed into the wrong
            # running agent, reported as success. "ambiguous" makes the caller
            # ask, and the tie is broken by what the sessions are DOING, since
            # that is the only thing that distinguishes them out loud.
            asking = [r for r in same if (r.get("question") or
                                          r.get("permission"))]
            if len(asking) == 1:
                # Only one of them is blocked on you, so that is the one you
                # are talking to. This is a guess, but a narrow and defensible
                # one, and it is marked as a guess.
                return asking[0], "fuzzy"
            same.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
            return same[0], "ambiguous"
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
        if how in ("sounds-like", "maybe", "several"):
            for r in rows:
                if (r.get("label") or "") == label:
                    # "several" means two names flatten to the same thing, such
                    # as voicebridge and voice-bridge. That is the same problem
                    # as two sessions sharing a name, and it gets the same
                    # answer: ask.
                    return r, {"sounds-like": "fuzzy", "maybe": "maybe",
                               "several": "ambiguous"}[how]
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

    def _offer(self, question: str, yes, again=None, no: str = "",
               pick: bool = False) -> dict:
        """Ask "did you mean X?" and remember what a yes means.

        Withholding a name Friday already has is the failure this replaces: it
        knows the real list, so the honest move is to put the closest one to you
        rather than report that you said something unrecognisable.

        `pick=True` marks a question with SEVERAL answers ("which one? a, b,
        c"). A yes is not one of them. Every one of these bound a yes to the
        first option, and this is a voice product, so "yes" is the likeliest
        spoken reply to any sentence ending in a question mark: "tell it to drop
        the migrations table" then "yes" typed that into whichever session came
        first, and reported it as sent."""
        self._offered = {"yes": yes, "again": again, "no": no, "pick": pick}
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
        # What was mentioned, and when, so that acting on it shortly afterwards
        # can be recognised as acting on it. Without the timestamp any later
        # mention of the same session would count, and everything would look
        # interesting.
        for it in (items or []):
            k = learn.key_for(it)
            if k:
                learn.saw(k)
                self._told[k] = time.time()
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
