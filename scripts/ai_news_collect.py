#!/usr/bin/env python3
"""Run the collector from an existing source checkout."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from feedroom.collector import main

    raise SystemExit(main())
