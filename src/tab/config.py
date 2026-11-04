"""Locations, overridable through environment variables:
  TAB_HOME      where runs live (default: <project>/runtime)
  TAB_DAY1_DIR  the "Day 1 Environment" folder with the persona drafts (default: ~/Downloads/Day 1 Environment)
"""
import os
from pathlib import Path

from . import ROOT


def home() -> Path:
    return Path(os.path.expanduser(os.environ.get("TAB_HOME") or str(ROOT / "runtime"))).resolve()


def day1_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("TAB_DAY1_DIR") or "~/Downloads/Day 1 Environment")).resolve()


def personas_dir() -> Path:
    return ROOT / "personas"
