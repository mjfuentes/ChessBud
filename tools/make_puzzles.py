"""Generate blunder-replay puzzles from the user's games.

Scans every game with Stockfish; whenever the user's move dropped the eval
by >= --threshold centipawns from a position that was not already lost,
records the pre-move position plus the engine's preferred move as a puzzle.

Writes data/puzzles.json incrementally after each game, so the trainer can
serve puzzles while generation is still running.

Usage: .venv/bin/python tools/make_puzzles.py [--threshold 250] [--max-games N]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from blunder_scan import capped_cp, phase_of, user_color
from userarg import add_pgn_argument, add_user_argument, games_pgn

ROOT = Path(__file__).resolve().parent.parent


def scan_game(
    game: chess.pgn.Game,
    color: chess.Color,
    engine: chess.engine.SimpleEngine,
    args: argparse.Namespace,
    next_id: int,
) -> list[dict]:
    puzzles = []
    board = chess.Board()
    prev_cp = 0
    last = {"fen": None, "uci": None, "san": None}
    limit = chess.engine.Limit(time=args.movetime)
    opponent = game.headers.get("Black" if color == chess.WHITE else "White", "?")
    opponent_elo = game.headers.get("BlackElo" if color == chess.WHITE else "WhiteElo", "")
    for move in game.mainline_moves():
        if board.fullmove_number > args.max_move:
            break
        is_user = board.turn == color
        san = board.san(move)
        move_no = board.fullmove_number
        phase = phase_of(board)
        fen_before = board.fen()
        board.push(move)
        cp = capped_cp(engine.analyse(board, limit)["score"], color)
        if is_user and prev_cp - cp >= args.threshold and prev_cp > -args.threshold:
            board.pop()
            best = engine.play(board, chess.engine.Limit(time=args.best_movetime))
            best_san = board.san(best.move)
            best_uci = best.move.uci()
            board.push(move)
            if best_uci != move.uci():
                puzzles.append(
                    {
                        "id": f"pz{next_id + len(puzzles)}",
                        "fen": fen_before,
                        "played_san": san,
                        "best_san": best_san,
                        "best_uci": best_uci,
                        "ref_cp": prev_cp,
                        "drop": prev_cp - cp,
                        "phase": phase,
                        "move_no": move_no,
                        "date": game.headers.get("Date", "?"),
                        "opponent": opponent,
                        "opponent_elo": opponent_elo,
                        "prev_fen": last["fen"],
                        "last_uci": last["uci"],
                        "last_san": last["san"],
                    }
                )
        prev_cp = cp
        last = {"fen": fen_before, "uci": move.uci(), "san": san}
    return puzzles


def write_out(out: Path, puzzles: list[dict]) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"puzzles": puzzles}, indent=1), encoding="utf-8")
    tmp.rename(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_pgn_argument(parser)
    add_user_argument(parser)
    parser.add_argument("--out", default=str(ROOT / "data" / "puzzles.json"))
    parser.add_argument("--threshold", type=int, default=250)
    parser.add_argument("--movetime", type=float, default=0.06)
    parser.add_argument("--best-movetime", type=float, default=1.0)
    parser.add_argument("--max-games", type=int, default=10_000)
    parser.add_argument("--max-move", type=int, default=999,
                        help="only scan the first N full moves of each game")
    args = parser.parse_args()

    engine = chess.engine.SimpleEngine.popen_uci("stockfish")
    puzzles: list[dict] = []
    scanned = 0
    try:
        with open(games_pgn(args.user, args.pgn), encoding="utf-8", errors="replace") as fh:
            while (game := chess.pgn.read_game(fh)) is not None:
                if scanned >= args.max_games:
                    break
                color = user_color(game, args.user)
                if color is None:
                    continue
                puzzles = puzzles + scan_game(game, color, engine, args, len(puzzles))
                scanned += 1
                write_out(Path(args.out), puzzles)
                if scanned % 20 == 0:
                    print(f"{scanned} games scanned, {len(puzzles)} puzzles", flush=True)
    finally:
        engine.quit()
    print(f"Done: {scanned} games, {len(puzzles)} puzzles -> {args.out}")


if __name__ == "__main__":
    main()
