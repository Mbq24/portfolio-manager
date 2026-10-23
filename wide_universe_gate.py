#!/usr/bin/env python3
"""DEPRECATED — thin wrapper. Use multi_instrument_gate.py --universe wide.

This started as a standalone 31-instrument sweep and duplicated the gate's measurement
logic. Two copies of the same decision rules is how they drift: the standalone version
had the buy-and-hold hurdle while the production gate did not, so the script that
decided what joined the live book was the weaker of the two. Everything now lives in
multi_instrument_gate.py; this file exists only so old invocations and notes keep
working.
"""
import sys

from multi_instrument_gate import main

if __name__ == "__main__":
    argv = ["--universe", "wide"] + [a for a in sys.argv[1:]]
    sys.exit(main(argv))
