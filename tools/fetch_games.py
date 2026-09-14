"""Fetch a chess.com user's complete game archive via the public API.

Writes data/users/<user>/games.pgn. Works for any username — this is the
entry point for preparing a new user's coaching data.

Usage: .venv/bin/python tools/fetch_games.py --user <name> [--time-class blitz]
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from userarg import add_user_argument

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "chesscoach-local-trainer/1.0 (personal coaching tool)"}


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_user_argument(parser)
    parser.add_argument(
        "--time-class", default="blitz",
        help="comma-separated (blitz,rapid,bullet,daily) or 'all'",
    )
    args = parser.parse_args()
    wanted = None if args.time_class == "all" else set(args.time_class.split(","))

    archives = get_json(f"https://api.chess.com/pub/player/{args.user}/games/archives")["archives"]
    print(f"{len(archives)} monthly archives for {args.user}")
    pgns: list[str] = []
    for url in archives:
        month = get_json(url)
        kept = [
            g["pgn"] for g in month.get("games", [])
            if "pgn" in g and (wanted is None or g.get("time_class") in wanted)
        ]
        pgns.extend(kept)
        print(f"  {url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]}: {len(kept)} games")

    out = ROOT / "data" / "users" / args.user / "games.pgn"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n".join(pgns) + "\n", encoding="utf-8")
    print(f"Wrote {len(pgns)} games -> {out}")


if __name__ == "__main__":
    main()
