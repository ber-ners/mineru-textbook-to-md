"""Run the bundled MinerU skill implementation from any working directory."""

from __future__ import annotations

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from mineru_textbook_to_md.cli import run  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run())
