#!/usr/bin/env python3
"""Start Friday.

    python3 run.py              on this machine only, no key needed
    python3 run.py --app        in the menu bar, as a Mac app
    python3 run.py --phone      also reachable from your phone (prints a keyed URL)
"""
import sys

if __name__ == "__main__":
    args = sys.argv[1:]
    ports = [a for a in args if a.isdigit()]
    if "--app" in args:
        # The app starts the server itself, and adopts one that is already
        # running rather than adding a second.
        import os
        if ports:
            os.environ["FRIDAY_PORT"] = ports[0]
        from friday.app import main
        main()
    else:
        from friday.server import run
        run(int(ports[0]) if ports else 8765, expose="--phone" in args)
