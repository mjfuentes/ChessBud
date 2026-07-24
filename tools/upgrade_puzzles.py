"""Deep re-analysis of data/puzzles.json.

Re-evaluates every puzzle position with more engine time, refreshing the
stored best move and reference eval that the shallow mining pass produced.
Writes incrementally so an interrupted run keeps its progress.

Usage: .venv/bin/python tools/upgrade_puzzles.py [--movetime 1.0]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parent.parent
PUZZLE_FILE = ROOT / "data" / "puzzles.json"
CAP = 1000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movetime", type=float, default=1.0)
    args = parser.parse_args()

    data = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
    puzzles = data["puzzles"]
    engine = chess.engine.SimpleEngine.popen_uci("stockfish")
    changed_best = 0
    try:
        for i, p in enumerate(puzzles):
            if p.get("deep"):
                continue
            board = chess.Board(p["fen"])
            info = engine.analyse(board, chess.engine.Limit(time=args.movetime))
            best = info["pv"][0]
            ref = info["score"].pov(board.turn).score(mate_score=CAP)
            if best.uci() != p["best_uci"]:
                changed_best += 1
            puzzles[i] = {
                **p,
                "best_uci": best.uci(),
                "best_san": board.san(best),
                "ref_cp": max(-CAP, min(CAP, ref)),
                "deep": True,
            }
            if (i + 1) % 25 == 0:
                PUZZLE_FILE.write_text(json.dumps({"puzzles": puzzles}, indent=1), encoding="utf-8")
                print(f"{i + 1}/{len(puzzles)} upgraded, {changed_best} best moves changed", flush=True)
    finally:
        engine.quit()
        PUZZLE_FILE.write_text(json.dumps({"puzzles": puzzles}, indent=1), encoding="utf-8")
    print(f"Done: {len(puzzles)} puzzles, {changed_best} best moves changed by deeper search")


if __name__ == "__main__":
    main()
