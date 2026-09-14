"""Shared `--user` / `--pgn` handling for every script in tools/.

No script hardcodes a chess.com username. The user comes from `--user`, or
from the CHESSCOACH_USER environment variable when the flag is omitted.

The game archive lives at data/users/<user>/games.pgn (written by
fetch_games.py); data/games.pgn is accepted as a legacy fallback so a hand
exported PGN still works without the fetch step.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_ENV = "CHESSCOACH_USER"


def env_user() -> str | None:
    value = os.environ.get(USER_ENV, "").strip()
    return value or None


def add_user_argument(parser: argparse.ArgumentParser) -> None:
    default = env_user()
    parser.add_argument(
        "--user",
        default=default,
        required=default is None,
        help=f"chess.com username (default: ${USER_ENV})",
    )


def add_pgn_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pgn",
        default=None,
        help="PGN file (default: data/users/<user>/games.pgn, else data/games.pgn)",
    )


def games_pgn(user: str, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    per_user = ROOT / "data" / "users" / user / "games.pgn"
    return per_user if per_user.exists() else ROOT / "data" / "games.pgn"
