"""Clock usage report from %clk annotations in a chess.com PGN export.

Answers: where does the clock go, how does time usage differ in wins vs
losses, and what actually happens in the games lost on time.

Usage: .venv/bin/python tools/time_report.py --user <name> [--pgn <file>]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from blunder_scan import phase_of
from opening_report import load_eco_book, classify
from userarg import ROOT, add_pgn_argument, add_user_argument, games_pgn

LONG_THINK = 20.0
CHECKPOINTS = (10, 20, 30)


@dataclass(frozen=True)
class MoveTime:
    move_no: int
    phase: str
    spent: float
    remaining: float


@dataclass(frozen=True)
class GameTimes:
    score: float
    lost_on_time: bool
    family: str
    date: str
    opponent: str
    moves: tuple[MoveTime, ...]


def parse_time_control(headers: chess.pgn.Headers) -> tuple[float, float]:
    tc = headers.get("TimeControl", "300+5")
    base, _, inc = tc.partition("+")
    return float(base), float(inc or 0)


def extract_game(game: chess.pgn.Game, user: str, book: dict) -> GameTimes | None:
    headers = game.headers
    if headers.get("White", "").lower() == user.lower():
        color = chess.WHITE
    elif headers.get("Black", "").lower() == user.lower():
        color = chess.BLACK
    else:
        return None
    result = headers.get("Result")
    if result not in ("1-0", "0-1", "1/2-1/2"):
        return None
    white_score = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[result]
    score = white_score if color == chess.WHITE else 1.0 - white_score

    base, inc = parse_time_control(headers)
    moves = []
    prev_clock = base
    board = chess.Board()
    for node in game.mainline():
        move_no, phase = board.fullmove_number, phase_of(board)
        mover = board.turn
        board.push(node.move)
        clock = node.clock()
        if mover != color or clock is None:
            continue
        moves.append(
            MoveTime(
                move_no=move_no,
                phase=phase,
                spent=max(0.0, prev_clock + inc - clock),
                remaining=clock,
            )
        )
        prev_clock = clock

    _, name = classify(game, book)
    return GameTimes(
        score=score,
        lost_on_time=score == 0.0 and "on time" in headers.get("Termination", ""),
        family=name.split(":")[0].strip(),
        date=headers.get("Date", "?"),
        opponent=headers.get("Black" if color == chess.WHITE else "White", "?"),
        moves=tuple(moves),
    )


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def phase_table(games: list[GameTimes]) -> list[str]:
    wins = [g for g in games if g.score == 1.0]
    losses = [g for g in games if g.score == 0.0]
    lines = ["\nAVG SECONDS PER MOVE (wins | losses):"]
    for phase in ("opening", "middlegame", "endgame"):
        w = avg([m.spent for g in wins for m in g.moves if m.phase == phase])
        l = avg([m.spent for g in losses for m in g.moves if m.phase == phase])
        lines.append(f"  {phase:<12} {w:5.1f}s | {l:5.1f}s")
    return lines


def checkpoint_table(games: list[GameTimes]) -> list[str]:
    lines = ["\nCLOCK REMAINING AT MOVE N (wins | losses, of 300s):"]
    for cp in CHECKPOINTS:
        cells = []
        for bucket in (1.0, 0.0):
            rem = [
                next((m.remaining for m in g.moves if m.move_no >= cp), None)
                for g in games
                if g.score == bucket
            ]
            cells.append(avg([r for r in rem if r is not None]))
        lines.append(f"  move {cp:<3} {cells[0]:6.0f}s | {cells[1]:6.0f}s")
    return lines


def long_think_table(games: list[GameTimes]) -> list[str]:
    thinks = [
        (m.spent, m.move_no, m.phase, g)
        for g in games
        for m in g.moves
        if m.spent >= LONG_THINK
    ]
    by_phase = defaultdict(int)
    for _, _, phase, _ in thinks:
        by_phase[phase] += 1
    lines = [
        f"\nLONG THINKS (>= {LONG_THINK:.0f}s): {len(thinks)} total "
        f"({len(thinks) / len(games):.1f} per game) — "
        + ", ".join(f"{p}: {n}" for p, n in sorted(by_phase.items(), key=lambda kv: -kv[1]))
    ]
    for spent, move_no, phase, g in sorted(thinks, key=lambda t: -t[0])[:5]:
        lines.append(
            f"  {spent:5.1f}s on move {move_no} ({phase}) — {g.date} vs {g.opponent}"
        )
    return lines


def flag_table(games: list[GameTimes]) -> list[str]:
    flagged = [g for g in games if g.lost_on_time]
    lines = [f"\nLOSSES ON TIME ({len(flagged)} games):"]
    if not flagged:
        return lines
    last_phases = defaultdict(int)
    for g in flagged:
        last_phases[g.moves[-1].phase if g.moves else "?"] += 1
    lines.append(
        "  flagged during: "
        + ", ".join(f"{p} x{n}" for p, n in sorted(last_phases.items(), key=lambda kv: -kv[1]))
    )
    open_spent = avg([sum(m.spent for m in g.moves if m.phase == "opening") for g in flagged])
    open_spent_all = avg(
        [sum(m.spent for m in g.moves if m.phase == "opening") for g in games if not g.lost_on_time]
    )
    lines.append(
        f"  time spent on moves 1-10: {open_spent:.0f}s in flagged games"
        f" vs {open_spent_all:.0f}s in the rest"
    )
    return lines


def opening_family_table(games: list[GameTimes]) -> list[str]:
    groups = defaultdict(list)
    for g in games:
        groups[g.family].append(sum(m.spent for m in g.moves if m.phase == "opening"))
    lines = ["\nTIME SPENT ON MOVES 1-10 BY OPENING (min 5 games):"]
    ranked = sorted(
        ((f, v) for f, v in groups.items() if len(v) >= 5), key=lambda kv: -avg(kv[1])
    )
    lines.extend(f"  {family:<28} {avg(v):5.0f}s  ({len(v)} games)" for family, v in ranked)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_pgn_argument(parser)
    add_user_argument(parser)
    args = parser.parse_args()

    book = load_eco_book(ROOT / "data")
    games = []
    with open(games_pgn(args.user, args.pgn), encoding="utf-8", errors="replace") as fh:
        while (g := chess.pgn.read_game(fh)) is not None:
            extracted = extract_game(g, args.user, book)
            if extracted is not None and extracted.moves:
                games.append(extracted)

    print(f"{args.user}: {len(games)} games with clock data")
    for section in (
        phase_table(games),
        checkpoint_table(games),
        long_think_table(games),
        flag_table(games),
        opening_family_table(games),
    ):
        print("\n".join(section))


if __name__ == "__main__":
    main()
