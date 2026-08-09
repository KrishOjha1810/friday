#!/usr/bin/env python3
"""Start Friday.  python3 run.py  then open http://127.0.0.1:8765"""
import sys
from friday.server import run
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    run(port)
