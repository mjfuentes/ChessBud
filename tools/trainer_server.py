"""Local opening trainer server.

Serves the board UI from web/ and a small JSON API. The opponent plays
scripted drill replies (data/drills/*.json) while you are in book, warns
when you deviate from your prep, and switches to strength-limited
Stockfish once the book runs out.

Usage: .venv/bin/python tools/trainer_server.py [--port 8420] [--elo 1320]
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DRILL_DIR = ROOT / "data" / "drills"
PUZZLE_FILES = {
    "blunders": ROOT / "data" / "puzzles.json",
    "openings": ROOT / "data" / "puzzles_openings.json",
}
LIVE_MISTAKES_FILE = ROOT / "data" / "puzzles_live.json"
OPENING_CHECK_PLIES = 24
PROGRESS_FILE = ROOT / "data" / "puzzle_progress.json"
PUZZLE_TOLERANCE_CP = 60
PRACTICE_TOLERANCE_CP = 80
BOOK_BLUNDER_CP = 150

# Progress is keyed by FEN (stable across re-mining), guarded for the
# threaded server. `pending` holds FENs whose next attempt is the first
# (scoring) attempt of the current appearance.
progress_lock = threading.Lock()
pending_first_attempt: set[str] = set()

ACTIVITY_LOG = ROOT / "data" / "activity_log.jsonl"
log_lock = threading.Lock()


def log_event(kind: str, **fields) -> None:
    entry = {"ts": round(time.time(), 3), "kind": kind, **fields}
    with log_lock:
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {}
    with open(PROGRESS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def save_progress(progress: dict) -> None:
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=1), encoding="utf-8")
    tmp.rename(PROGRESS_FILE)


class Drill:
    """Indexes drill lines by move history for expected-move and reply lookup."""

    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.intro = spec.get("intro", "")
        self.user_color = chess.WHITE if spec.get("user_color", "white") == "white" else chess.BLACK
        self.expected: dict[tuple, set[str]] = {}
        self.replies: dict[tuple, list[tuple[str, str | None]]] = {}
        for variation in spec["lines"]:
            self._index_line(variation)

    def _index_line(self, variation: dict) -> None:
        board = chess.Board()
        history: list[str] = []
        notes = variation.get("notes", {})
        for ply, san in enumerate(variation["line"].split()):
            key = tuple(history)
            if board.turn == self.user_color:
                self.expected.setdefault(key, set()).add(san)
            else:
                note = notes.get(str(ply // 2 + 1))
                entry = (san, note)
                if entry not in self.replies.setdefault(key, []):
                    self.replies[key].append(entry)
            try:
                board.push_san(san)
            except ValueError:
                return
            history.append(san)


def load_drills(user: str) -> dict[str, Drill]:
    drills = {}
    paths = [(p.stem, p) for p in DRILL_DIR.glob("*.json")]
    user_dir = DRILL_DIR / user
    if user_dir.is_dir():
        paths += [(f"{user}/{p.stem}", p) for p in user_dir.glob("*.json")]
    for drill_id, path in paths:
        with open(path, encoding="utf-8") as fh:
            drills[drill_id] = Drill(json.load(fh))
    add_practice_repertoires(drills, user)
    return drills


class MistakeIndex:
    """Maps the user's historical opening-mistake positions to the game paths
    that reach them, so practice games can steer into unfixed mistakes."""

    def __init__(self, user: str):
        self.user = user
        self.signature = None
        self.last_build = 0.0
        self.epd_to_fen: dict[str, str] = {}
        self.paths: dict[str, set[str]] = {}

    def _pgn_path(self) -> Path:
        p = ROOT / "data" / "users" / self.user / "games.pgn"
        return p if p.exists() else ROOT / "data" / "games.pgn"

    def get(self) -> "MistakeIndex":
        pf = PUZZLE_FILES["openings"]
        gp = self._pgn_path()
        signature = (
            pf.stat().st_mtime if pf.exists() else 0,
            gp.stat().st_mtime if gp.exists() else 0,
            LIVE_MISTAKES_FILE.stat().st_mtime if LIVE_MISTAKES_FILE.exists() else 0,
        )
        # throttle rebuilds: the miner touches the file constantly while running
        if signature == self.signature or time.time() - self.last_build < 30:
            return self
        self.signature = signature
        self.last_build = time.time()
        self.epd_to_fen = {}
        self.paths = {}
        for p in load_puzzles("openings"):
            epd = chess.Board(p["fen"]).epd()
            self.epd_to_fen[epd] = p["fen"]
            if p.get("path_epds"):
                self.paths[epd] = set(p["path_epds"])
        if not self.epd_to_fen or not gp.exists():
            return self
        with open(gp, encoding="utf-8", errors="replace") as fh:
            while (game := chess.pgn.read_game(fh)) is not None:
                board = chess.Board()
                trail = [board.epd()]
                for i, mv in enumerate(game.mainline_moves()):
                    if i >= 26:
                        break
                    try:
                        board.push(mv)
                    except (ValueError, AssertionError):
                        break
                    epd = board.epd()
                    trail.append(epd)
                    if epd in self.epd_to_fen and epd not in self.paths:
                        self.paths[epd] = set(trail)
        return self

    def hot_epds(self, progress: dict) -> set[str]:
        """Every position on the road to a not-yet-fixed mistake."""
        hot: set[str] = set()
        for epd, fen in self.epd_to_fen.items():
            if not progress.get(fen, {}).get("solved"):
                hot |= self.paths.get(epd, {epd})
        return hot


def record_mistake_result(fen_key: str, solved_now: bool) -> bool:
    """Update progress for a historical mistake position met during practice.

    A sound move marks it fixed — unless the user just bounced off this same
    position seconds ago (retries after a warning don't count as a fix).
    Returns True when the position was newly marked fixed.
    """
    with progress_lock:
        progress = load_progress()
        entry = progress.get(fen_key, {"solved": False, "misses": 0})
        now = int(time.time())
        fixed = False
        if solved_now:
            if now - entry.get("last_miss", 0) > 60 and not entry.get("solved"):
                entry = {**entry, "solved": True}
                fixed = True
            elif entry.get("solved"):
                fixed = False
        else:
            entry = {
                **entry,
                "solved": False,
                "misses": entry.get("misses", 0) + 1,
                "last_miss": now,
            }
        progress[fen_key] = {**entry, "last_seen": now}
        save_progress(progress)
    return fixed


class DrillCache:
    """Reload drills whenever files under data/drills change, so freshly
    generated preparation is playable without a server restart."""

    def __init__(self, user: str):
        self.user = user
        self.signature = None
        self.drills: dict[str, Drill] = {}

    def get(self) -> dict[str, Drill]:
        paths = sorted(DRILL_DIR.glob("*.json"))
        user_dir = DRILL_DIR / self.user
        if user_dir.is_dir():
            paths += sorted(user_dir.glob("*.json"))
        signature = tuple((str(p), p.stat().st_mtime) for p in paths)
        if signature != self.signature:
            self.signature = signature
            self.drills = load_drills(self.user)
        return self.drills


def add_practice_repertoires(drills: dict[str, Drill], user: str) -> None:
    """Merge all of a user's drills per color into one playable repertoire.

    The opponent then 'decides the opening' the way real opponents do: any
    reply seen in the user's games can appear, and the user must answer
    with their prepared move for whatever line arises.
    """
    for color_name, color in (("white", chess.WHITE), ("black", chess.BLACK)):
        merged = Drill({"name": f"Opening practice — {color_name.capitalize()}",
                        "user_color": color_name, "lines": []})
        merged.intro = (
            f"You are {color_name}. The opponent plays what your real opponents "
            "play — answer with your prep. Off-book moves bounce back."
        )
        count = 0
        for drill_id, d in list(drills.items()):
            if not drill_id.startswith(f"{user}/") or d.user_color != color:
                continue
            count += 1
            for key, sans in d.expected.items():
                merged.expected.setdefault(key, set()).update(sans)
            for key, entries in d.replies.items():
                bucket = merged.replies.setdefault(key, [])
                for entry in entries:
                    if entry not in bucket:
                        bucket.append(entry)
        if count:
            drills[f"practice:{color_name}"] = merged


class EngineWrapper:
    """One Stockfish process, serialized — SimpleEngine is not thread-safe
    and the HTTP server is threaded."""

    def __init__(self, elo: int | None):
        self.elo = elo
        self._engine: chess.engine.SimpleEngine | None = None
        self._lock = threading.Lock()

    @property
    def engine(self) -> chess.engine.SimpleEngine:
        if self._engine is None:
            self._engine = chess.engine.SimpleEngine.popen_uci("stockfish")
            if self.elo is not None:
                self._engine.configure(
                    {"UCI_LimitStrength": True, "UCI_Elo": max(1320, self.elo)}
                )
        return self._engine

    def eval_cp(self, board: chess.Board, color: chess.Color, movetime: float = 0.6) -> int:
        cp, _ = self.analyse(board, color, movetime)
        return cp

    def analyse(
        self, board: chess.Board, color: chess.Color, movetime: float = 0.6
    ) -> tuple[int, list[chess.Move]]:
        with self._lock:
            info = self.engine.analyse(board, chess.engine.Limit(time=movetime))
        cp = max(-1000, min(1000, info["score"].pov(color).score(mate_score=1000)))
        return cp, list(info.get("pv", []))

    def best_move(self, board: chess.Board) -> chess.Move:
        with self._lock:
            result = self.engine.play(board, chess.engine.Limit(time=0.35))
        return result.move

    def quit(self) -> None:
        if self._engine is not None:
            self._engine.quit()


def game_state(board: chess.Board) -> dict:
    outcome = board.outcome()
    return {
        "fen": board.fen(),
        "legal": [m.uci() for m in board.legal_moves],
        "check": board.is_check(),
        "game_over": outcome is not None,
        "result": outcome.result() if outcome else None,
    }


def with_promotion(board: chess.Board, uci: str) -> chess.Move:
    move = chess.Move.from_uci(uci if len(uci) > 4 else uci)
    piece = board.piece_at(move.from_square)
    if (
        len(uci) == 4
        and piece is not None
        and piece.piece_type == chess.PAWN
        and chess.square_rank(move.to_square) in (0, 7)
    ):
        move = chess.Move.from_uci(uci + "q")
    return move


def opponent_reply(
    board: chess.Board,
    history: list[str],
    drill: Drill | None,
    engine: EngineWrapper,
    hot: set[str] | None = None,
    strong: EngineWrapper | None = None,
) -> dict:
    if drill is not None:
        book = drill.replies.get(tuple(history))
        if book:
            preferred, rest = [], []
            for san, note in book:
                probe = board.copy()
                try:
                    probe.push(probe.parse_san(san))
                except ValueError:
                    continue
                (preferred if hot and probe.epd() in hot else rest).append((san, note))
            random.shuffle(preferred)
            random.shuffle(rest)
            best_cp = None
            for san, note in preferred + rest:
                # sanity-check book moves at full strength: real opponents
                # blunder, but serving their blunders teaches nothing
                if strong is not None:
                    if best_cp is None:
                        best_cp = strong.eval_cp(board, board.turn, movetime=0.25)
                    probe = board.copy()
                    mv = probe.parse_san(san)
                    probe.push(mv)
                    after_cp = strong.eval_cp(probe, not probe.turn, movetime=0.25)
                    if best_cp - after_cp > BOOK_BLUNDER_CP:
                        continue
                move = board.parse_san(san)
                board.push(move)
                return {"reply_san": san, "reply_uci": move.uci(), "source": "book", "note": note}
    # out of book in the opening: reply at full strength so the line is real
    replier = strong if strong is not None and len(history) < OPENING_CHECK_PLIES else engine
    move = replier.best_move(board)
    san = board.san(move)
    board.push(move)
    return {"reply_san": san, "reply_uci": move.uci(), "source": "engine", "note": None}


def handle_drills(drills: dict[str, Drill], user: str) -> dict:
    manifest_path = ROOT / "data" / "users" / user / "openings.json"
    stats = {}
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as fh:
            stats = {e["id"]: e for e in json.load(fh).get("drills", [])}
    entries = []
    for drill_id, d in drills.items():
        s = stats.get(drill_id, {})
        entries.append(
            {
                "id": drill_id,
                "name": d.name,
                "user_color": "white" if d.user_color == chess.WHITE else "black",
                "games": s.get("games"),
                "score_pct": s.get("score_pct"),
                "lines": s.get("lines", len(d.expected)),
            }
        )
    entries.sort(key=lambda e: (0 if "/" in e["id"] else 1, e["user_color"], e["name"]))

    with progress_lock:
        progress = load_progress()
    sets = {}
    for set_name in PUZZLE_FILES:
        puzzles = load_puzzles(set_name)
        solved = sum(1 for p in puzzles if progress.get(p["fen"], {}).get("solved"))
        sets[set_name] = {"total": len(puzzles), "solved": solved}
    return {"drills": entries, "user": user, "puzzles": sets}


def practice_hot_epds(mistake_index: MistakeIndex) -> set[str]:
    with progress_lock:
        progress = load_progress()
    return mistake_index.get().hot_epds(progress)


def handle_new(
    payload: dict, drills: dict[str, Drill], engine: EngineWrapper, mistake_index: MistakeIndex
) -> dict:
    drill_id = payload.get("drill", "")
    if payload.get("practice"):
        options = [k for k in drills if k.startswith("practice:")]
        drill_id = random.choice(options) if options else ""
    drill = drills.get(drill_id)
    board = chess.Board(payload["fen"]) if payload.get("fen") else chess.Board()
    intro = drill.intro if drill else "Free play — you move for the side to play."
    start_fen = board.fen()
    pre_moves = []
    if drill is not None and board.turn != drill.user_color:
        reply = opponent_reply(board, [], drill, engine, practice_hot_epds(mistake_index))
        # (root position is always in book for generated repertoires)
        pre_moves.append(
            {
                "san": reply["reply_san"],
                "uci": reply["reply_uci"],
                "fen": board.fen(),
                "note": reply.get("note"),
            }
        )
    game_id = f"g{int(time.time() * 1000):x}{random.randrange(16 ** 4):04x}"
    log_event(
        "game_start",
        game=game_id,
        drill=drill_id or None,
        orientation="black" if drill and drill.user_color == chess.BLACK else "white",
        pre_moves=[m["san"] for m in pre_moves],
    )
    return {
        **game_state(board),
        "message": intro,
        "game_id": game_id,
        "drill_id": drill_id if drill else None,
        "drill_name": drill.name if drill else None,
        "orientation": "black" if drill and drill.user_color == chess.BLACK else "white",
        "start_fen": start_fen,
        "pre_moves": pre_moves,
        "eval_cp": 0,
    }


def handle_move(
    payload: dict,
    drills: dict[str, Drill],
    engine: EngineWrapper,
    analysis: EngineWrapper,
    mistake_index: MistakeIndex,
) -> dict:
    board = chess.Board(payload["fen"])
    history = list(payload.get("history", []))
    drill = drills.get(payload.get("drill", ""))

    move = with_promotion(board, payload["move"])
    if move not in board.legal_moves:
        return {"error": "illegal move", **game_state(board)}
    user_san = board.san(move)

    prep_note = None
    mistake_fen = None
    in_prep = None
    if drill is not None:
        mistake_fen = mistake_index.get().epd_to_fen.get(board.epd())
        expected = drill.expected.get(tuple(history))
        in_prep = bool(expected) and user_san in expected
        in_opening = len(history) < OPENING_CHECK_PLIES
        if not in_prep and (expected or mistake_fen or in_opening):
            prep = " or ".join(sorted(expected)) if expected else None
            mover = board.turn
            best_cp, best_pv = analysis.analyse(board, mover, movetime=0.35)
            board.push(move)
            after_cp = analysis.eval_cp(board, mover, movetime=0.35)
            board.pop()
            if best_cp - after_cp > PRACTICE_TOLERANCE_CP:
                warning = f"{user_san} gives ground here — try again."
                if prep:
                    warning += f" (Your prep: {prep}.)"
                if mistake_fen:
                    record_mistake_result(mistake_fen, False)
                    warning += " You went wrong here in a real game too."
                elif in_opening and best_pv:
                    record_live_mistake(
                        payload["fen"], user_san, board.san(best_pv[0]),
                        best_pv[0].uci(), best_cp, history,
                    )
                    record_mistake_result(payload["fen"], False)
                    warning += " Saved as a new exercise — it will come back."
                log_event(
                    "move", game=payload.get("game"), drill=payload.get("drill"),
                    fen=payload["fen"], san=user_san, uci=move.uci(),
                    ply=len(history), rejected=True, mistake=bool(mistake_fen),
                )
                return {**game_state(board), "rejected": True, "warning": warning}
            if prep:
                prep_note = f"{user_san} is playable. Your prep was {prep}."
    fixed_now = False
    if mistake_fen:
        fixed_now = record_mistake_result(mistake_fen, True)
        if fixed_now:
            prep_note = ((prep_note + " ") if prep_note else "") + \
                "You went wrong in this position in a real game — now fixed."
    board.push(move)
    history.append(user_san)
    fen_after_user = board.fen()

    if board.outcome() is not None:
        log_event(
            "move", game=payload.get("game"), drill=payload.get("drill"),
            fen=payload["fen"], san=user_san, uci=move.uci(), ply=len(history) - 1,
            rejected=False, prep=in_prep, mistake=bool(mistake_fen),
            fixed=fixed_now, game_over=True,
        )
        return {
            **game_state(board),
            "user_san": user_san,
            "fen_after_user": fen_after_user,
            "history": history,
            "prep_note": prep_note,
        }

    hot = practice_hot_epds(mistake_index) if drill is not None else None
    reply = opponent_reply(
        board, history, drill, engine, hot, strong=analysis if drill is not None else None
    )
    history.append(reply["reply_san"])
    eval_cp = analysis.eval_cp(board, chess.WHITE, movetime=0.3)
    log_event(
        "move", game=payload.get("game"), drill=payload.get("drill"),
        fen=payload["fen"], san=user_san, uci=move.uci(), ply=len(history) - 2,
        rejected=False, prep=in_prep, mistake=bool(mistake_fen), fixed=fixed_now,
        reply=reply["reply_san"], source=reply["source"], eval_cp=eval_cp,
    )
    return {
        **game_state(board),
        "user_san": user_san,
        "fen_after_user": fen_after_user,
        "history": history,
        "prep_note": prep_note,
        "eval_cp": eval_cp,
        **reply,
    }


def pretty_date(pgn_date: str) -> str:
    try:
        year, month, day = pgn_date.split(".")
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        return f"{int(day)} {months[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return pgn_date


def load_puzzles(set_name: str = "blunders") -> list[dict]:
    path = PUZZLE_FILES.get(set_name)
    if path is None:
        return []
    puzzles = []
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            puzzles = json.load(fh).get("puzzles", [])
    if set_name == "openings" and LIVE_MISTAKES_FILE.exists():
        with open(LIVE_MISTAKES_FILE, encoding="utf-8") as fh:
            puzzles = puzzles + json.load(fh).get("puzzles", [])
    return [{**p, "id": f"{set_name}:{p['id']}"} for p in puzzles]


def record_live_mistake(
    fen: str, played_san: str, best_san: str, best_uci: str, ref_cp: int, history: list[str]
) -> None:
    """A bad move played out of prep during practice becomes a new exercise."""
    with progress_lock:
        existing = []
        if LIVE_MISTAKES_FILE.exists():
            with open(LIVE_MISTAKES_FILE, encoding="utf-8") as fh:
                existing = json.load(fh).get("puzzles", [])
        if any(p["fen"] == fen for p in existing):
            return
        board = chess.Board()
        fens = [board.fen()]
        epds = [board.epd()]
        for san in history:
            try:
                board.push_san(san)
            except ValueError:
                break
            fens.append(board.fen())
            epds.append(board.epd())
        entry = {
            "id": f"lv{len(existing)}",
            "fen": fen,
            "played_san": played_san,
            "best_san": best_san,
            "best_uci": best_uci,
            "ref_cp": ref_cp,
            "drop": 0,
            "phase": "opening",
            "move_no": len(history) // 2 + 1,
            "date": time.strftime("%Y.%m.%d"),
            "opponent": "practice",
            "opponent_elo": "",
            "prev_fen": fens[-2] if len(fens) >= 2 else None,
            "last_uci": None,
            "last_san": history[-1] if history else None,
            "path_epds": epds,
        }
        tmp = LIVE_MISTAKES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"puzzles": existing + [entry]}, indent=1), encoding="utf-8")
        tmp.rename(LIVE_MISTAKES_FILE)


def puzzle_set_of(puzzle_id: str) -> str:
    return puzzle_id.split(":", 1)[0] if ":" in puzzle_id else "blunders"


def handle_puzzle_next(payload: dict) -> dict:
    set_name = payload.get("set", "blunders")
    puzzles = load_puzzles(set_name)
    seen = set(payload.get("seen", []))
    with progress_lock:
        progress = load_progress()
    unsolved = [p for p in puzzles if not progress.get(p["fen"], {}).get("solved")]
    solved_count = len(puzzles) - len(unsolved)
    if not unsolved:
        return {"done": True, "total": len(puzzles), "solved_count": solved_count}
    # prefer puzzles not shown this session; if exhausted, missed ones repeat
    pool = [p for p in unsolved if p["id"] not in seen] or unsolved
    p = random.choice(pool)
    with progress_lock:
        pending_first_attempt.add(p["fen"])
    log_event("puzzle_served", id=p["id"], set=set_name, fen=p["fen"])
    board = chess.Board(p["fen"])
    color_name = "White" if board.turn == chess.WHITE else "Black"
    return {
        "id": p["id"],
        "fen": p["fen"],
        "legal": [m.uci() for m in board.legal_moves],
        "orientation": color_name.lower(),
        "check": board.is_check(),
        "context": {
            "opponent": p["opponent"],
            "opponent_elo": p.get("opponent_elo", ""),
            "date": pretty_date(p["date"]),
            "move_no": p["move_no"],
            "phase": p["phase"],
            "task": (
                f"Opponent played {p['last_san']}. " if p.get("last_san") else ""
            ) + f"Find the best move for {color_name}.",
        },
        "prev_fen": p.get("prev_fen"),
        "last_uci": p.get("last_uci"),
        "last_san": p.get("last_san"),
        "remaining": len(unsolved) - 1,
        "solved_count": solved_count,
        "total": len(puzzles),
        "set": set_name,
        "eval_cp": p["ref_cp"] if board.turn == chess.WHITE else -p["ref_cp"],
        "game_over": False,
        "result": None,
    }


FORCED_MARGIN_CP = 90


def trim_to_forcing(
    board: chess.Board, pv: list[chess.Move], engine: chess.engine.SimpleEngine, max_plies: int = 6
) -> list[chess.Move]:
    """Keep the line only while moves are essentially forced.

    The first move (the point of the line) is always kept. After that, stop
    at the first position where the side to move has a second option within
    FORCED_MARGIN_CP of the best — once there are many playable moves, the
    rest of the line teaches nothing.
    """
    b = board.copy()
    kept: list[chess.Move] = []
    for i, mv in enumerate(pv[:max_plies]):
        if i >= 1:
            infos = engine.analyse(b, chess.engine.Limit(time=0.1), multipv=2)
            if len(infos) > 1:
                best = infos[0]["score"].pov(b.turn).score(mate_score=1000)
                second = infos[1]["score"].pov(b.turn).score(mate_score=1000)
                if best is not None and second is not None and best - second < FORCED_MARGIN_CP:
                    break
        kept.append(mv)
        b.push(mv)
    return kept


def pv_line(board: chess.Board, pv: list[chess.Move], limit: int = 6) -> list[dict]:
    """Variation as clickable items: numbered SAN plus the FEN each move leads to."""
    b = board.copy()
    items = []
    for i, mv in enumerate(pv[:limit]):
        if b.turn == chess.WHITE:
            label = f"{b.fullmove_number}."
        else:
            label = f"{b.fullmove_number}..." if i == 0 else ""
        san = b.san(mv)
        b.push(mv)
        items.append({"label": label, "san": san, "uci": mv.uci(), "fen": b.fen()})
    return items


def handle_puzzle_attempt(payload: dict, analysis: EngineWrapper) -> dict:
    puzzle_id = payload.get("id", "")
    puzzles = {p["id"]: p for p in load_puzzles(puzzle_set_of(puzzle_id))}
    p = puzzles.get(puzzle_id)
    if p is None:
        return {"error": "unknown puzzle id"}
    board = chess.Board(p["fen"])
    color = board.turn
    move = with_promotion(board, payload["move"])
    if move not in board.legal_moves:
        return {"error": "illegal move"}
    played_san = board.san(move)
    board.push(move)
    eval_white, pv = analysis.analyse(board, chess.WHITE)
    eval_user = eval_white if color == chess.WHITE else -eval_white
    continuation = pv_line(board, trim_to_forcing(board, pv, analysis.engine))
    if move.uci() == p["best_uci"]:
        correct = True
    else:
        correct = p["ref_cp"] - eval_user <= PUZZLE_TOLERANCE_CP
    best_line = []
    if not correct:
        board_before = chess.Board(p["fen"])
        _, best_pv = analysis.analyse(board_before, color)
        best_line = pv_line(board_before, trim_to_forcing(board_before, best_pv, analysis.engine))
    board_orig = chess.Board(p["fen"])
    original_line = pv_line(board_orig, [board_orig.parse_san(p["played_san"])])
    with progress_lock:
        first_attempt = p["fen"] in pending_first_attempt
        if first_attempt:
            pending_first_attempt.discard(p["fen"])
            progress = load_progress()
            entry = progress.get(p["fen"], {"solved": False, "misses": 0})
            if correct:
                entry = {**entry, "solved": True}
            else:
                entry = {**entry, "misses": entry.get("misses", 0) + 1}
            progress[p["fen"]] = {**entry, "last_seen": int(time.time())}
            save_progress(progress)
    log_event(
        "puzzle_attempt", id=puzzle_id, set=puzzle_set_of(puzzle_id), fen=p["fen"],
        san=played_san, correct=correct, first_attempt=first_attempt,
        best=p["best_san"], eval_cp=eval_white,
    )
    return {
        "correct": correct,
        "first_attempt": first_attempt,
        "played_san": played_san,
        "played_uci": move.uci(),
        "fen_after": board.fen(),
        "best_san": p["best_san"],
        "best_uci": p["best_uci"],
        "original_san": p["played_san"],
        "original_line": original_line,
        "continuation_line": continuation,
        "best_line": best_line,
        "eval_cp": eval_white,
    }


def make_handler(
    drill_cache: DrillCache,
    engine: EngineWrapper,
    analysis: EngineWrapper,
    user: str,
    mistake_index: MistakeIndex,
):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_DIR), **kwargs)

        def log_message(self, *args):  # silence request logging
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                drills = drill_cache.get()
                if self.path == "/api/drills":
                    body = handle_drills(drills, user)
                elif self.path == "/api/new":
                    body = handle_new(payload, drills, engine, mistake_index)
                elif self.path == "/api/move":
                    body = handle_move(payload, drills, engine, analysis, mistake_index)
                elif self.path == "/api/puzzle/next":
                    body = handle_puzzle_next(payload)
                elif self.path == "/api/puzzle/attempt":
                    body = handle_puzzle_attempt(payload, analysis)
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:  # noqa: BLE001 — report any API error to the client
                self.send_error(500, str(exc))

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--elo", type=int, default=3000)
    parser.add_argument("--user", default="prosekkopapi")
    args = parser.parse_args()

    drill_cache = DrillCache(args.user)
    engine = EngineWrapper(args.elo)
    analysis = EngineWrapper(None)
    mistake_index = MistakeIndex(args.user)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(drill_cache, engine, analysis, args.user, mistake_index),
    )
    print(
        f"Trainer running at http://localhost:{args.port}"
        f"  (drills: {', '.join(drill_cache.get())})"
    )
    try:
        server.serve_forever()
    finally:
        engine.quit()
        analysis.quit()


if __name__ == "__main__":
    main()
