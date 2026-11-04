"""TaxAgentBench agent environment (MVP), Python port of the retired Ruby prototype (backup: TaxAgentBench-Env.zip).

Standard library plus PyMuPDF, so it runs the same on Windows, macOS and Linux.
"""
from pathlib import Path

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parents[2]


class TabError(Exception):
    """Expected, user-facing failures (bad input, missing setup). The CLIs print the message and exit 1."""
