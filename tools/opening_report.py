"""Opening performance report for a chess.com PGN export.

Classifies each game against the lichess ECO database by position matching
(so transpositions are handled), then aggregates results by color and opening.

Usage: .venv/bin/python tools/opening_report.py --user <name> [--pgn <file>]
"""

from __future__ import annotations

import argparse
import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from userarg import ROOT, add_pgn_argument, add_user_argument, games_pgn

MAX_BOOK_PLIES = 24
MIN_GAMES_FOR_LEADERBOARD = 3


@dataclass(frozen=True)
class GameRecord:
    color: str  # "white" | "black"
    score: float  # 1.0 win, 0.5 draw, 0.0 loss
    eco: str
    opening: str
    family: str
    termination: str


def load_eco_book(data_dir: Path) -> dict[str, tuple[str, str]]:
    """Map board EPD -> (eco, opening name) for every book line."""
    book: dict[str, tuple[str, str]] = {}
    for tsv in sorted(data_dir.glob("eco_*.tsv")):
        with open(tsv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                board = chess.Board()
                game = chess.pgn.read_game(io.StringIO(row["pgn"]))
                if game is None:
                    continue
                for move in game.mainline_moves():
                    board.push(move)
                book[board.epd()] = (row["eco"], row["name"])
    return book


def classify(game: chess.pgn.Game, book: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Return (eco, name) of the deepest book position reached in the game."""
    board = chess.Board()
    hit = ("?", "Unknown opening")
    for ply, move in enumerate(game.mainline_moves()):
        if ply >= MAX_BOOK_PLIES:
            break
        board.push(move)
        hit = book.get(board.epd(), hit)
    return hit


def score_for(game: chess.pgn.Game, color: str) -> float | None:
    result = game.headers.get("Result", "*")
    table = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
    if result not in table:
        return None
    white_score = table[result]
    return white_score if color == "white" else 1.0 - white_score


def read_games(pgn_path: Path, user: str, book: dict[str, tuple[str, str]]) -> list[GameRecord]:
    records = []
    with open(pgn_path, encoding="utf-8", errors="replace") as fh:
        while (game := chess.pgn.read_game(fh)) is not None:
            headers = game.headers
            if headers.get("White", "").lower() == user.lower():
                color = "white"
            elif headers.get("Black", "").lower() == user.lower():
                color = "black"
            else:
                continue
            score = score_for(game, color)
            if score is None:
                continue
            eco, name = classify(game, book)
            family = name.split(":")[0].strip()
            records.append(
                GameRecord(
                    color=color,
                    score=score,
                    eco=eco,
                    opening=name,
                    family=family,
                    termination=headers.get("Termination", ""),
                )
            )
    return records


def format_line(label: str, games: list[GameRecord]) -> str:
    wins = sum(1 for g in games if g.score == 1.0)
    draws = sum(1 for g in games if g.score == 0.5)
    losses = sum(1 for g in games if g.score == 0.0)
    pct = 100.0 * sum(g.score for g in games) / len(games)
    return f"  {label:<52} {len(games):>3} games  +{wins}={draws}-{losses}  {pct:5.1f}%"


def report_color(records: list[GameRecord], color: str, by: str) -> list[str]:
    subset = [g for g in records if g.color == color]
    groups: dict[str, list[GameRecord]] = defaultdict(list)
    for g in subset:
        groups[getattr(g, by)].append(g)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    lines = [f"\nAs {color.upper()} ({len(subset)} games) — by {by}:"]
    lines.extend(format_line(name, games) for name, games in ranked if len(games) >= 2)
    return lines


def worst_openings(records: list[GameRecord]) -> list[str]:
    groups: dict[tuple[str, str], list[GameRecord]] = defaultdict(list)
    for g in records:
        groups[(g.color, g.family)].append(g)
    eligible = {k: v for k, v in groups.items() if len(v) >= MIN_GAMES_FOR_LEADERBOARD}
    ranked = sorted(eligible.items(), key=lambda kv: sum(g.score for g in kv[1]) / len(kv[1]))
    lines = ["\nWORST SCORES (min 3 games, lower = bigger problem):"]
    lines.extend(
        format_line(f"[{color}] {family}", games) for (color, family), games in ranked[:10]
    )
    return lines


def loss_terminations(records: list[GameRecord]) -> list[str]:
    losses = [g for g in records if g.score == 0.0]
    counts = Counter(
        "on time" if "on time" in g.termination else
        "by checkmate" if "checkmate" in g.termination else
        "by resignation" if "resignation" in g.termination else "other"
        for g in losses
    )
    lines = [f"\nHOW YOU LOSE ({len(losses)} losses):"]
    lines.extend(f"  {how:<20} {n:>3}  ({100.0 * n / len(losses):.0f}%)" for how, n in counts.most_common())
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_pgn_argument(parser)
    add_user_argument(parser)
    args = parser.parse_args()

    book = load_eco_book(ROOT / "data")
    records = read_games(games_pgn(args.user, args.pgn), args.user, book)
    total = sum(g.score for g in records)
    print(f"{args.user}: {len(records)} games, overall score {100.0 * total / len(records):.1f}%")

    output = (
        report_color(records, "white", "family")
        + report_color(records, "black", "family")
        + worst_openings(records)
        + loss_terminations(records)
    )
    print("\n".join(output))


if __name__ == "__main__":
    main()
