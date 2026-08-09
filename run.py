#!/usr/bin/env python3
"""Start Friday.

    python3 run.py              on this machine only, no key needed
    python3 run.py --phone      also reachable from your phone (prints a keyed URL)
"""
import sys

from friday.server import run

if __name__ == "__main__":
    args = sys.argv[1:]
    expose = "--phone" in args
    ports = [a for a in args if a.isdigit()]
    run(int(ports[0]) if ports else 8765, expose=expose)
