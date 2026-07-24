"""Build personalized opening-practice drills from a user's game archive.

For every opening family the user actually plays (both colors), builds a
drill tree where:
- the user's own moves are kept when sound (within EVAL_TOLERANCE of the
  engine's best), and replaced by the engine's move when they leak eval;
- opponent branches are the replies opponents actually play against the
  user, most frequent first;
- lines are extended with engine moves beyond the user's data until
  TARGET_DEPTH plies, so preparation goes deeper than past games.

Writes data/drills/<user>/*.json (trainer drill format) and a manifest at
data/users/<user>/openings.json.

Usage: .venv/bin/python tools/prepare_openings.py [--user prosekkopapi]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from blunder_scan import user_color
from opening_report import load_eco_book, classify

ROOT = Path(__file__).resolve().parent.parent
MAX_PLIES = 20
TARGET_DEPTH = 14
MIN_FAMILY_GAMES = 2
MAX_BRANCHES = 8
MAX_LINES_PER_DRILL = 150
EVAL_TOLERANCE = 50
DECIDED_CP = 350
RESULT_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class EngineMemo:
    def __init__(self) -> None:
        self.engine = chess.engine.SimpleEngine.popen_uci("stockfish")
        self.cache: dict[str, tuple[str, int]] = {}
        self.calls = 0

    def best(self, board: chess.Board) -> tuple[chess.Move, int]:
        """Best move and its eval (side-to-move POV, capped)."""
        key = board.epd()
        if key not in self.cache:
            info = self.engine.analyse(board, chess.engine.Limit(time=0.3))
            cp = info["score"].pov(board.turn).score(mate_score=1000)
            self.cache[key] = (info["pv"][0].uci(), max(-1000, min(1000, cp)))
            self.calls += 1
        uci, cp = self.cache[key]
        return chess.Move.from_uci(uci), cp

    def eval_after(self, board: chess.Board, move: chess.Move) -> int:
        """Eval of `move` from the mover's POV (capped)."""
        child = board.copy()
        child.push(move)
        key = "after:" + child.epd()
        if key not in self.cache:
            info = self.engine.analyse(child, chess.engine.Limit(time=0.25))
            cp = info["score"].pov(not child.turn).score(mate_score=1000)
            self.cache[key] = ("", max(-1000, min(1000, cp)))
            self.calls += 1
        return self.cache[key][1]

    def quit(self) -> None:
        self.engine.quit()


def collect_games(pgn_path: Path, user: str, book: dict):
    """Group each game's opening moves by (color, family)."""
    grouped: dict[tuple[str, str], list[tuple[list[str], float]]] = defaultdict(list)
    with open(pgn_path, encoding="utf-8", errors="replace") as fh:
        while (game := chess.pgn.read_game(fh)) is not None:
            if game.headers.get("Variant"):
                continue
            color = user_color(game, user)
            result = game.headers.get("Result")
            if color is None or result not in RESULT_SCORE:
                continue
            score = RESULT_SCORE[result] if color == chess.WHITE else 1.0 - RESULT_SCORE[result]
            board = chess.Board()
            sans = []
            try:
                for move in list(game.mainline_moves())[:MAX_PLIES]:
                    sans.append(board.san(move))
                    board.push(move)
            except (ValueError, AssertionError):
                continue
            if len(sans) < 6:
                continue
            _, name = classify(game, book)
            family = name.split(":")[0].strip()
            color_name = "white" if color == chess.WHITE else "black"
            grouped[(color_name, family)].append((sans, score))
    return grouped


def build_counters(games: list[tuple[list[str], float]]):
    moves: dict[tuple, Counter] = defaultdict(Counter)
    stats: dict[tuple, list[float]] = defaultdict(lambda: [0, 0.0])
    for sans, score in games:
        for ply in range(len(sans)):
            key = tuple(sans[:ply])
            moves[key][sans[ply]] += 1
            stats[key][0] += 1
            stats[key][1] += score
    return moves, stats


def expand(history, board, is_user_turn_fn, moves, stats, memo, notes, lines):
    """Depth-first expansion; appends (sans, notes) per finished line."""
    if len(lines) >= MAX_LINES_PER_DRILL or len(history) >= MAX_PLIES:
        lines.append((list(history), dict(notes)))
        return
    key = tuple(history)
    cnt = moves.get(key)

    if is_user_turn_fn(board):
        best_move, best_cp = memo.best(board)
        if abs(best_cp) > DECIDED_CP:
            lines.append((list(history), dict(notes)))
            return
        chosen = None
        if cnt:
            own_san = cnt.most_common(1)[0][0]
            try:
                own_move = board.parse_san(own_san)
            except ValueError:
                own_move = None
            if own_move == best_move:
                chosen = own_san
            elif own_move is not None and best_cp - memo.eval_after(board, own_move) <= EVAL_TOLERANCE:
                chosen = own_san
        if chosen is None:
            chosen = board.san(best_move)
        board.push_san(chosen)
        expand(history + [chosen], board, is_user_turn_fn, moves, stats, memo, notes, lines)
        board.pop()
        return

    # opponent's turn: branch over real replies, else extend with the engine
    branches = cnt.most_common(MAX_BRANCHES) if cnt else []
    if not branches:
        if len(history) >= TARGET_DEPTH:
            lines.append((list(history), dict(notes)))
            return
        best_move, best_cp = memo.best(board)
        if abs(best_cp) > DECIDED_CP:
            lines.append((list(history), dict(notes)))
            return
        san = board.san(best_move)
        move_no = board.fullmove_number
        branch_notes = dict(notes)
        if "engine" not in "".join(branch_notes.values()):
            branch_notes[str(move_no)] = "Beyond your games — engine line from here."
        board.push_san(san)
        expand(history + [san], board, is_user_turn_fn, moves, stats, memo, branch_notes, lines)
        board.pop()
        return

    for san, count in branches:
        branch_notes = dict(notes)
        child_key = tuple(history + [san])
        n, pts = stats.get(child_key, [0, 0.0])
        if count >= 3 and n:
            branch_notes[str(board.fullmove_number)] = (
                f"You've faced this {count} times (scoring {100.0 * pts / n:.0f}%)."
            )
        try:
            board.push_san(san)
        except ValueError:
            continue
        expand(history + [san], board, is_user_turn_fn, moves, stats, memo, branch_notes, lines)
        board.pop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="prosekkopapi")
    args = parser.parse_args()

    pgn_path = ROOT / "data" / "users" / args.user / "games.pgn"
    if not pgn_path.exists():
        pgn_path = ROOT / "data" / "games.pgn"
    book = load_eco_book(ROOT / "data")
    grouped = collect_games(pgn_path, args.user, book)

    out_dir = ROOT / "data" / "drills" / args.user
    out_dir.mkdir(parents=True, exist_ok=True)
    memo = EngineMemo()
    manifest = []
    try:
        for (color_name, family), games in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            if len(games) < MIN_FAMILY_GAMES:
                continue
            moves, stats = build_counters(games)
            user_is_white = color_name == "white"
            is_user_turn = lambda b: (b.turn == chess.WHITE) == user_is_white  # noqa: E731
            lines: list[tuple[list[str], dict]] = []
            expand([], chess.Board(), is_user_turn, moves, stats, memo, {}, lines)
            spec_lines = [
                {"line": " ".join(sans), "notes": notes}
                for sans, notes in lines if len(sans) >= 6
            ]
            if not spec_lines:
                continue
            score = 100.0 * sum(s for _, s in games) / len(games)
            name = f"{family} — {color_name.capitalize()}"
            spec = {
                "name": name,
                "user_color": color_name,
                "intro": (
                    f"Built from {len(games)} of your games as {color_name} "
                    f"(you score {score:.0f}%). Play your prepared moves."
                ),
                "lines": spec_lines,
            }
            fname = slug(f"{color_name}-{family}") + ".json"
            (out_dir / fname).write_text(json.dumps(spec, indent=1), encoding="utf-8")
            manifest.append(
                {
                    "id": f"{args.user}/{slug(f'{color_name}-{family}')}",
                    "family": family,
                    "color": color_name,
                    "games": len(games),
                    "score_pct": round(score, 1),
                    "lines": len(spec_lines),
                }
            )
            print(f"{name}: {len(games)} games, {len(spec_lines)} lines", flush=True)
    finally:
        memo.quit()

    mpath = ROOT / "data" / "users" / args.user / "openings.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps({"drills": manifest}, indent=1), encoding="utf-8")
    print(f"\n{len(manifest)} drills -> {out_dir}  ({memo.calls} engine evals)")


if __name__ == "__main__":
    main()
