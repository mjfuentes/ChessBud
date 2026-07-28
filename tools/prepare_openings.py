"""Build opening-practice drills from published theory.

Your archive decides WHICH openings to prepare — the ones you play and the
ones played at you. It no longer decides what the lines are: a family sampled
from nineteen games is not a repertoire.

The lines come from data/eco_*.tsv, the lichess-org/chess-openings list of
3,807 named variations. At your turn one continuation is prepared, the one the
most named variations run through; at the opponent's turn every named reply is
kept, widest first. Branches are labelled with their variation names.

No engine: the book is curated theory, and the trainer measures every book move
as you play it, so a prepared move that leaks is caught at the board rather
than silently swapped out here.

Writes data/drills/<user>/*.json (trainer drill format) and a manifest at
data/users/<user>/openings.json.

Usage: .venv/bin/python tools/prepare_openings.py [--user prosekkopapi]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import chess
import chess.pgn

from blunder_scan import user_color
from opening_report import load_eco_book, classify
from eco_tree import build_tree, family_of, load_eco_lines

ROOT = Path(__file__).resolve().parent.parent
# The ladder grades up to 15 of the user's moves (30 plies), and a rung is only
# a real test if several lines still exist at it. Lines that stop at 14 plies
# made every depth past 7 collapse to a handful of engine tails.
MAX_PLIES = 26
MIN_FAMILY_GAMES = 2
MAX_BRANCHES = 8
MAX_LINES_PER_DRILL = 150
RESULT_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


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


def reaches_family(sans: list[str], family: str, book: dict) -> bool:
    """Does this line actually become the opening the drill is named after?

    Games are bucketed by their deepest ECO name, so a handful transpose in
    from elsewhere (a Petrov that later reaches an Italian position). Branching
    on those produces lines that never touch the family, which the trainer then
    has no way to steer back to."""
    board = chess.Board()
    for san in sans:
        try:
            board.push_san(san)
        except ValueError:
            return False
        entry = book.get(board.epd())
        if entry and entry[1].split(":")[0].strip() == family:
            return True
    return False


def expand_theory(history, board, node, is_user_turn_fn, notes, lines):
    """Grow one opening from the ECO book. No engine.

    The book is curated theory: every line in it is something people play and
    have written about. Evaluating it at generation time asked Stockfish to
    re-decide what theory already settled, and the trainer measures every book
    move as you play it anyway — so a prepared move that leaks is caught at the
    board, where it means something, rather than silently swapped out here.

    At your turn one continuation is prepared: the one the most named
    variations run through, which is the opening's main road. At the
    opponent's turn every named continuation is kept, widest first — that is
    the theory of what gets played at you. A line ends where the book ends.
    """
    if len(lines) >= MAX_LINES_PER_DRILL or len(history) >= MAX_PLIES:
        lines.append((list(history), dict(notes)))
        return

    kids = sorted(node.kids.items(), key=lambda kv: -kv[1].weight) if node else []
    if not kids:
        lines.append((list(history), dict(notes)))
        return

    if is_user_turn_fn(board):
        san, kid = kids[0]
        try:
            board.push_san(san)
        except ValueError:
            lines.append((list(history), dict(notes)))
            return
        expand_theory(history + [san], board, kid, is_user_turn_fn, notes, lines)
        board.pop()
        return

    for san, kid in kids[:MAX_BRANCHES]:
        branch_notes = dict(notes)
        if kid.name:
            branch_notes[str(board.fullmove_number)] = kid.name
        try:
            board.push_san(san)
        except ValueError:
            continue
        expand_theory(history + [san], board, kid, is_user_turn_fn, branch_notes, lines)
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
    # Your games decide WHICH openings to prepare — you play 1.e4, you meet the
    # French. They no longer decide what the lines are: a family sampled from
    # nineteen games is not a repertoire, and the ECO book holds hundreds of
    # named variations for the same opening.
    entries = load_eco_lines(ROOT / "data")
    by_family: dict[str, list] = defaultdict(list)
    for e in entries:
        by_family[family_of(e[1])].append(e)

    out_dir = ROOT / "data" / "drills" / args.user
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for (color_name, family), games in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        if len(games) < MIN_FAMILY_GAMES:
            continue
        theory = by_family.get(family)
        if not theory:
            print(f"skip {family} — not in the ECO book", flush=True)
            continue
        user_is_white = color_name == "white"
        is_user_turn = lambda b: (b.turn == chess.WHITE) == user_is_white  # noqa: E731
        lines: list[tuple[list[str], dict]] = []
        expand_theory([], chess.Board(), build_tree(theory), is_user_turn,
                      {}, lines)
        spec_lines = [
            {"line": " ".join(sans), "notes": notes}
            for sans, notes in lines
            if len(sans) >= 6 and reaches_family(sans, family, book)
        ]
        if not spec_lines:
            continue
        score = 100.0 * sum(s for _, s in games) / len(games)
        name = f"{family} — {color_name.capitalize()}"
        spec = {
            "name": name,
            "user_color": color_name,
            "intro": (
                f"{len(theory)} named variations of the {family}, "
                f"from the ECO book. You have played it {len(games)} times, "
                f"scoring {score:.0f}%."
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

    mpath = ROOT / "data" / "users" / args.user / "openings.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps({"drills": manifest}, indent=1), encoding="utf-8")
    print(f"\n{len(manifest)} drills -> {out_dir}")


if __name__ == "__main__":
    main()
