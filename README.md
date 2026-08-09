# Friday

A voice-and-text assistant that sits above your coding agents.

One conversation. You type or you talk, same thread. It knows every session on
your machine, tells you when one needs you, takes your answer back to the right
one, and can open, resume, or hand context to any of them.

Friday is not a coding agent. It coordinates them, and keeps you the only one
who decides anything.

## Status

Early. The engine (fleet sensing, the attention model that decides when to
interrupt you, the local brain, answer routing) is built and tested. This is
the app around it.

## How it relates to voicebridge

[voicebridge](https://github.com/cc-vb/voicebridge) is a separate product in its
own right: the voice layer for a single Claude Code session. Friday consumes it
as a library for speech and the phone link. You can use voicebridge alone;
Friday needs it.
