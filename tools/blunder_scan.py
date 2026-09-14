"""Scan games with Stockfish and report the user's blunders by game phase.

A blunder is a move that drops the eval by >= --threshold centipawns
(from the user's perspective, capped at +/-1000 to ignore already-lost noise).

Usage:
  .venv/bin/python tools/blunder_scan.py --max-games 10
  .venv/bin/python tools/blunder_scan.py --opening "French Defense" --color white
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from userarg import ROOT, add_pgn_argument, add_user_argument, games_pgn

CAP = 1000


def phase_of(board: chess.Board) -> str:
    if board.fullmove_number <= 10:
        return "opening"
    minor_major = sum(
        len(board.pieces(pt, c))
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
        for c in (chess.WHITE, chess.BLACK)
    )
    return "endgame" if minor_major <= 6 else "middlegame"


def capped_cp(score: chess.engine.PovScore, color: chess.Color) -> int:
    return max(-CAP, min(CAP, score.pov(color).score(mate_score=CAP)))


def user_color(game: chess.pgn.Game, user: str) -> chess.Color | None:
    if game.headers.get("White", "").lower() == user.lower():
        return chess.WHITE
    if game.headers.get("Black", "").lower() == user.lower():
        return chess.BLACK
    return None


def matches_filters(game: chess.pgn.Game, color: chess.Color, args: argparse.Namespace) -> bool:
    if args.color and {"white": chess.WHITE, "black": chess.BLACK}[args.color] != color:
        return False
    if args.opening:
        moves_san = []
        board = chess.Board()
        for move in list(game.mainline_moves())[:12]:
            moves_san.append(board.san(move))
            board.push(move)
        if not opening_matches(args.opening, " ".join(moves_san)):
            return False
    return True


_ECO_CACHE: list[tuple[str, str]] = []


def opening_matches(family: str, movetext: str) -> bool:
    if not _ECO_CACHE:
        import csv
        for tsv in sorted((ROOT / "data").glob("eco_*.tsv")):
            with open(tsv, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    san = " ".join(
                        tok for tok in row["pgn"].split() if not tok.rstrip(".").isdigit()
                    )
                    _ECO_CACHE.append((row["name"], san))
    candidates = [san for name, san in _ECO_CACHE if name.split(":")[0].strip() == family]
    game_san = " ".join(tok for tok in movetext.split() if not tok.rstrip(".").isdigit())
    return any(game_san.startswith(san) or san.startswith(game_san) for san in candidates)


def scan_game(
    game: chess.pgn.Game,
    color: chess.Color,
    engine: chess.engine.SimpleEngine,
    args: argparse.Namespace,
) -> list[dict]:
    blunders = []
    board = chess.Board()
    limit = chess.engine.Limit(time=args.movetime)
    prev_cp = 0
    for move in game.mainline_moves():
        is_users_move = board.turn == color
        san = board.san(move)
        move_no = board.fullmove_number
        phase = phase_of(board)
        board.push(move)
        info = engine.analyse(board, limit)
        cp = capped_cp(info["score"], color)
        if is_users_move and prev_cp - cp >= args.threshold and prev_cp > -args.threshold:
            blunders.append(
                {
                    "move_no": move_no,
                    "san": san,
                    "drop": prev_cp - cp,
                    "phase": phase,
                    "fen_before": board.fen(),  # position after the blunder
                    "date": game.headers.get("Date", "?"),
                    "opponent": game.headers.get(
                        "Black" if color == chess.WHITE else "White", "?"
                    ),
                }
            )
        prev_cp = cp
    return blunders


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_pgn_argument(parser)
    add_user_argument(parser)
    parser.add_argument("--color", choices=["white", "black"])
    parser.add_argument("--opening", help="ECO family name, e.g. 'French Defense'")
    parser.add_argument("--max-games", type=int, default=20)
    parser.add_argument("--movetime", type=float, default=0.05)
    parser.add_argument("--threshold", type=int, default=200)
    args = parser.parse_args()

    engine = chess.engine.SimpleEngine.popen_uci("stockfish")
    all_blunders = []
    scanned = 0
    try:
        with open(games_pgn(args.user, args.pgn), encoding="utf-8", errors="replace") as fh:
            while (game := chess.pgn.read_game(fh)) is not None:
                if scanned >= args.max_games:
                    break
                color = user_color(game, args.user)
                if color is None or not matches_filters(game, color, args):
                    continue
                scanned += 1
                all_blunders.extend(scan_game(game, color, engine, args))
    finally:
        engine.quit()

    print(f"Scanned {scanned} games, found {len(all_blunders)} blunders (>= {args.threshold}cp)")
    phases = Counter(b["phase"] for b in all_blunders)
    for phase in ("opening", "middlegame", "endgame"):
        print(f"  {phase:<12} {phases.get(phase, 0)}")
    print("\nWorst blunders:")
    for b in sorted(all_blunders, key=lambda b: -b["drop"])[:10]:
        print(
            f"  {b['date']} vs {b['opponent']:<18} move {b['move_no']}. {b['san']:<7}"
            f" dropped {b['drop']}cp ({b['phase']})"
        )
        print(f"    FEN after: {b['fen_before']}")


if __name__ == "__main__":
    main()
