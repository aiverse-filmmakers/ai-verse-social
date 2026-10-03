#!/usr/bin/env python3
"""Portable entrypoint; works from any directory without installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from social_video_ops.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

