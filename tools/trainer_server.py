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
import math
import random
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chess
import chess.engine
import chess.pgn

import ladder as ladder_mod

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DRILL_DIR = ROOT / "data" / "drills"
PUZZLE_FILES = {
    "blunders": ROOT / "data" / "puzzles.json",
    "openings": ROOT / "data" / "puzzles_openings.json",
}
LIVE_MISTAKES_FILE = ROOT / "data" / "puzzles_live.json"
OPENING_CHECK_PLIES = 24
# Every run starts from move 1, so the baseline a run's drift is measured
# against is fixed: the initial position, ~+0.3 for White. Black's job in the
# opening is to equalise from -0.3, not to be equal — hence the sign flip.
START_CP = 30
# a run fails if it gives up this much from the starting eval, or if it ends
# below the floor in absolute terms (mirrored in web/app.js)
DRIFT_FAIL_CP = -75
FLOOR_CP = -100
# how much win probability a scripted reply may give up once you have left the
# line it was written for, before the opponent abandons the script and plays
SCRIPT_MAX_LOSS = 0.06
PROGRESS_FILE = ROOT / "data" / "puzzle_progress.json"
LADDER_FILE = ROOT / "data" / "ladder.json"
ladder = ladder_mod.Ladder(LADDER_FILE)
USER = "prosekkopapi"  # replaced by --user in main()
PUZZLE_TOLERANCE_CP = 60
PRACTICE_TOLERANCE_CP = 80
BOOK_BLUNDER_CP = 150

# Progress is keyed by FEN (stable across re-mining), guarded for the
# threaded server. `pending` holds FENs whose next attempt is the first
# (scoring) attempt of the current appearance.
progress_lock = threading.Lock()
pending_first_attempt: set[str] = set()
last_served: dict[tuple, str] = {}  # position -> book reply served last time
recent_paths: list[tuple[str, set]] = []  # (game_id, epds served) for last N games
RECENT_GAMES = 6
RECENT_PENALTY = 0.3


def note_recent(game_id: str | None, epd: str) -> None:
    if not game_id:
        return
    for gid, epds in recent_paths:
        if gid == game_id:
            epds.add(epd)
            return
    recent_paths.append((game_id, {epd}))
    del recent_paths[:-RECENT_GAMES]


def recency_factor(epd: str, game_id: str | None) -> float:
    """Compounding penalty for territory visited in recent games."""
    seen = sum(1 for gid, epds in recent_paths if gid != game_id and epd in epds)
    return RECENT_PENALTY ** seen

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


def article(word: str) -> str:
    return "an" if word[:1].upper() in "AEIOU" else "a"


def note_rank(note: str | None) -> int:
    """Lower is better: stats from real games beat generic notes beat
    'beyond your games' claims (which are only true for one line, not
    necessarily for the position)."""
    if note and "faced this" in note:
        return 0
    if note and "Beyond your games" not in note:
        return 1
    if note is None:
        return 2
    return 3


def add_reply(bucket: list, san: str, note: str | None) -> None:
    for i, (existing_san, existing_note) in enumerate(bucket):
        if existing_san == san:
            if note_rank(note) < note_rank(existing_note):
                bucket[i] = (san, note)
            return
    bucket.append((san, note))


class Drill:
    """Indexes drill lines by move history for expected-move and reply lookup."""

    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.intro = spec.get("intro", "")
        self.user_color = chess.WHITE if spec.get("user_color", "white") == "white" else chess.BLACK
        self.expected: dict[tuple, set[str]] = {}
        self.replies: dict[tuple, list[tuple[str, str | None]]] = {}
        self.lines: list[list[str]] = [v["line"].split() for v in spec["lines"]]
        for variation in spec["lines"]:
            self._index_line(variation)
        self.family = self.name.split(" — ")[0].strip()
        # Histories from which a drill line still reaches this drill's opening.
        # Outside these, the opponent has taken the game somewhere the family
        # can never happen (a Petrov in an Italian drill), so there is nothing
        # to steer back to and the book guard must stay quiet.
        self.on_family: set[tuple] = set()
        for variation in spec["lines"]:
            self._index_family(variation)

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
                add_reply(self.replies.setdefault(key, []), san, note)
            try:
                board.push_san(san)
            except ValueError:
                return
            history.append(san)

    def _index_family(self, variation: dict) -> None:
        """Mark every prefix of a line that goes on to reach this family."""
        book = eco_book()
        board = chess.Board()
        history: list[str] = []
        prefixes: list[tuple] = [()]
        for san in variation["line"].split():
            try:
                board.push_san(san)
            except ValueError:
                return
            history.append(san)
            prefixes.append(tuple(history))
            entry = book.get(board.epd())
            if entry and entry[1].split(":")[0].strip() == self.family:
                # everything up to here was still on the way to the family
                self.on_family.update(prefixes)


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
        self.downstream: dict[str, set[str]] = {}
        self.position_stats: dict[str, list[float]] = {}  # epd -> [games, points]

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
        self.position_stats = {}
        result_score = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
        if gp.exists():
            with open(gp, encoding="utf-8", errors="replace") as fh:
                while (game := chess.pgn.read_game(fh)) is not None:
                    headers = game.headers
                    if headers.get("White", "").lower() == self.user.lower():
                        user_white = True
                    elif headers.get("Black", "").lower() == self.user.lower():
                        user_white = False
                    else:
                        continue
                    white_score = result_score.get(headers.get("Result"))
                    pts = None if white_score is None else (
                        white_score if user_white else 1.0 - white_score
                    )
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
                        if pts is not None:
                            entry = self.position_stats.setdefault(epd, [0, 0.0])
                            entry[0] += 1
                            entry[1] += pts
                        if epd in self.epd_to_fen and epd not in self.paths:
                            self.paths[epd] = set(trail)
        # reverse index: position -> mistakes reachable through it
        self.downstream = {}
        for m_epd, fen in self.epd_to_fen.items():
            for e in self.paths.get(m_epd, {m_epd}):
                self.downstream.setdefault(e, set()).add(fen)
        return self

    def downstream_counts(self, progress: dict) -> dict[str, int]:
        """Position -> how many still-unfixed mistakes lie through it."""
        counts: dict[str, int] = {}
        for epd, fens in self.downstream.items():
            n = sum(1 for f in fens if not progress.get(f, {}).get("solved"))
            if n:
                counts[epd] = n
        return counts

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
                for san, note in entries:
                    add_reply(bucket, san, note)
        if count:
            drills[f"practice:{color_name}"] = merged


def pov_cp(info: dict, color: chess.Color) -> int:
    """An analysis line's eval in centipawns, from `color`'s point of view."""
    return max(-1000, min(1000, info["score"].pov(color).score(mate_score=1000)))


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
        return pov_cp(info, color), list(info.get("pv", []))

    def best_move(self, board: chess.Board) -> chess.Move:
        with self._lock:
            result = self.engine.play(board, chess.engine.Limit(time=0.35))
        return result.move

    def analyse_top2(
        self, board: chess.Board, color: chess.Color, movetime: float = 0.35
    ) -> tuple[int, int | None, list[chess.Move]]:
        """Best eval, second-best eval, and the best line (mover's POV)."""
        with self._lock:
            infos = self.engine.analyse(
                board, chess.engine.Limit(time=movetime), multipv=2
            )
        second = pov_cp(infos[1], color) if len(infos) > 1 else None
        return pov_cp(infos[0], color), second, list(infos[0].get("pv", []))

    def analyse_multipv(
        self, board: chess.Board, color: chess.Color, count: int, movetime: float
    ) -> list[tuple[str, int, list[str]]]:
        """(root uci, eval, line) per candidate, best first, in `color`'s POV.

        The time limit is for the whole search, so it is split across the
        lines — asking for fewer candidates buys depth on each of them."""
        with self._lock:
            infos = self.engine.analyse(
                board, chess.engine.Limit(time=movetime), multipv=max(2, count)
            )
        return [
            (info["pv"][0].uci(), pov_cp(info, color), [m.uci() for m in info["pv"][:6]])
            for info in infos
            if info.get("pv")
        ]


def expected_points(cp: int) -> float:
    """Win-probability equivalent of an eval — the scale chess.com's move
    classification works on."""
    return 1.0 / (1.0 + math.exp(-cp / 400.0))


def classify_loss(loss: float) -> str:
    if loss < 0.02:
        return "excellent"
    if loss < 0.05:
        return "good"
    if loss < 0.10:
        return "inaccuracy"
    if loss < 0.20:
        return "mistake"
    return "blunder"

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
    hot: dict[str, int] | None = None,
    strong: EngineWrapper | None = None,
    game_id: str | None = None,
    fail_next: dict[str, int] | None = None,
) -> dict:
    fail_next = fail_next or {}
    if drill is not None:
        book = list(drill.replies.get(tuple(history)) or [])
        # unredeemed failed lines continue as extra candidates, even past book
        book_sans = {s for s, _ in book}
        book += [(san, None) for san in fail_next if san not in book_sans]
        if book:
            # weighted draw: branches holding more unfixed mistakes or failed
            # lines are likelier, but never certain — and the reply served
            # last time here is penalized so lines don't repeat back-to-back
            key = tuple(history)
            pool = []
            for san, note in book:
                probe = board.copy()
                try:
                    probe.push(probe.parse_san(san))
                except ValueError:
                    continue
                epd = probe.epd()
                # one unit per target: an unfixed real-game mistake and an
                # unredeemed failed practice line weigh exactly the same
                base = (hot.get(epd, 0) if hot else 0) + fail_next.get(san, 0) + 1.0
                weight = base * recency_factor(epd, game_id)
                if last_served.get(key) == san:
                    weight *= 0.2
                pool.append((san, note, weight))
            order = []
            remaining = pool[:]
            while remaining:
                pick = random.choices(
                    range(len(remaining)), weights=[w for _, _, w in remaining]
                )[0]
                order.append(remaining.pop(pick))
            best_cp = None
            for san, note, _ in order:
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
                last_served[key] = san
                note_recent(game_id, board.epd())
                return {"reply_san": san, "reply_uci": move.uci(), "source": "book", "note": note}
    # out of book in the opening: reply at full strength so the line is real
    replier = strong if strong is not None and len(history) < OPENING_CHECK_PLIES else engine
    move = replier.best_move(board)
    san = board.san(move)
    board.push(move)
    note_recent(game_id, board.epd())
    return {"reply_san": san, "reply_uci": move.uci(), "source": "engine", "note": None}


_profile_cache: dict = {"ts": 0.0, "data": None}


def fetch_profile(user: str) -> dict | None:
    """Current chess.com ratings, cached for 30 minutes; fail-soft offline."""
    now = time.time()
    if now - _profile_cache["ts"] < 1800:
        return _profile_cache["data"]
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.chess.com/pub/player/{user}/stats",
            headers={"User-Agent": "chesscoach-local-trainer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as res:
            raw = json.load(res)
        data = {}
        for key, label in (("chess_blitz", "blitz"), ("chess_rapid", "rapid")):
            s = raw.get(key)
            if not s:
                continue
            record = s.get("record", {})
            data[label] = {
                "rating": s.get("last", {}).get("rating"),
                "best": s.get("best", {}).get("rating"),
                "wins": record.get("win"),
                "losses": record.get("loss"),
                "draws": record.get("draw"),
            }
        _profile_cache.update(ts=now, data=data)
    except Exception:
        _profile_cache["ts"] = now  # don't retry on every request while offline
    return _profile_cache["data"]


_log_cache: dict = {"sig": None, "data": None}


def analyze_practice_log() -> dict:
    sig = ACTIVITY_LOG.stat().st_size if ACTIVITY_LOG.exists() else 0
    if _log_cache["sig"] == sig and _log_cache["data"] is not None:
        return _log_cache["data"]
    starts: dict[str, dict] = {}
    moves_by_game: dict[str, list] = {}
    reported: dict[str, bool] = {}  # verdicts the client sent at the end of a run
    fixed = bounces = total_moves = 0
    if ACTIVITY_LOG.exists():
        with open(ACTIVITY_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = e.get("kind")
                if kind == "game_start" and e.get("game"):
                    starts[e["game"]] = e
                elif kind == "run_result" and e.get("game"):
                    reported[e["game"]] = bool(e.get("passed"))
                elif kind == "move" and e.get("game"):
                    moves_by_game.setdefault(e["game"], []).append(e)
                    if e.get("rejected"):
                        bounces += 1
                    else:
                        total_moves += 1
                    if e.get("fixed"):
                        fixed += 1
    results: dict[str, dict] = {}
    order: list[str] = []
    for gid, moves in moves_by_game.items():
        moves.sort(key=lambda x: x.get("ts", 0))
        start = starts.get(gid, {})
        orientation = start.get("orientation", "white")
        sans = list(start.get("pre_moves") or [])
        game_bounces = 0
        flawed = False
        verdict = None
        for e in moves:
            if e.get("rejected"):
                game_bounces += 1
                continue
            sans.append(e["san"])
            if e.get("reply"):
                sans.append(e["reply"])
            if e.get("move_class") in ("inaccuracy", "mistake", "blunder"):
                flawed = True
            cp_e = e.get("eval_cp")
            if verdict is None and cp_e is not None:
                user_cp = cp_e if orientation == "white" else -cp_e
                baseline = START_CP if orientation == "white" else -START_CP
                if user_cp < FLOOR_CP:
                    verdict = False  # early fail: position already lost
                elif e.get("ply", 0) >= 18:
                    verdict = (
                        game_bounces == 0
                        and not flawed
                        and user_cp >= FLOOR_CP
                        and user_cp - baseline > DRIFT_FAIL_CP
                    )
        # the client saw the whole run — including moves reclassified once the
        # reply landed — so its verdict wins. The replay above only covers runs
        # logged before /api/result existed, and assumes the old depth of 10.
        if gid in reported:
            verdict = reported[gid]
        op = opening_name(sans)
        results[gid] = {
            "verdict": verdict,
            "orientation": orientation,
            "family": op["name"].split(":")[0].strip() if op else "Other",
            "sans": sans,
        }
        order.append(gid)

    # a failure is redeemed when the SAME line is later passed clean —
    # whether it reappeared by steering, focus, or chance
    superseded: set[str] = set()
    for i, fid in enumerate(order):
        f = results[fid]
        if f["verdict"] is not False:
            continue
        key = f["sans"][:20]
        for pid in order[i + 1:]:
            p = results[pid]
            if p["verdict"] and p["sans"][:20] == key:
                superseded.add(fid)
                break

    data = {
        "starts": starts,
        "results": results,
        "order": order,
        "superseded": superseded,
        "totals": {"moves": total_moves, "bounces": bounces, "fixed": fixed},
    }
    _log_cache.update(sig=sig, data=data)
    return data


def failed_lines(color_name: str, family: str | None = None) -> list[tuple[str, dict]]:
    """Unredeemed failed opening runs, oldest first."""
    a = analyze_practice_log()
    return [
        (gid, a["results"][gid])
        for gid in a["order"]
        if a["results"][gid]["verdict"] is False
        and gid not in a["superseded"]
        and a["results"][gid]["orientation"] == color_name
        and (family is None or a["results"][gid]["family"] == family)
    ]


def failed_next_moves(drill: Drill, drill_id: str, history: list[str]) -> dict[str, int]:
    """For each unredeemed failed line passing through this position, the
    move it continues with — served as extra-weight steering options."""
    color_name = "white" if drill.user_color == chess.WHITE else "black"
    focused = bool(drill_id) and not drill_id.startswith("practice:")
    out: dict[str, int] = {}
    for _, res in failed_lines(color_name, drill.family if focused else None):
        sans = res["sans"]
        if len(sans) > len(history) and sans[: len(history)] == history:
            nxt = sans[len(history)]
            out[nxt] = out.get(nxt, 0) + 1
    return out


def training_summary() -> dict:
    analysis = analyze_practice_log()
    results = analysis["results"]
    order = analysis["order"]
    superseded = analysis["superseded"]
    total_moves = analysis["totals"]["moves"]
    bounces = analysis["totals"]["bounces"]
    fixed = analysis["totals"]["fixed"]
    if not results:
        return {}

    passes = completed = 0
    per_opening: dict[tuple, dict] = {}
    for gid in order:
        if gid in superseded:
            continue
        res = results[gid]
        entry = per_opening.setdefault(
            (res["orientation"], res["family"]),
            {"color": res["orientation"], "family": res["family"], "games": 0,
             "passes": 0, "completed": 0, "recent": []},
        )
        entry["games"] += 1
        if res["verdict"] is not None:
            completed += 1
            entry["completed"] += 1
            entry["recent"] = (entry["recent"] + [bool(res["verdict"])])[-5:]
            if res["verdict"]:
                passes += 1
                entry["passes"] += 1
    openings = sorted(per_opening.values(), key=lambda e: -e["games"])
    return {
        "games": len(results),
        "moves": total_moves,
        "bounces": bounces,
        "fixed": fixed,
        "passes": passes,
        "completed": completed,
        "openings": openings,
    }


MIXED_DEPTH = 10  # mixed practice has no single line to grade, so it uses this


def ladder_state(drill_id: str, drill: Drill) -> dict:
    """How far this opening's lines have grown — for display only. Nothing here
    gates anything: depth belongs to lines, not to openings."""
    if not drill.lines:
        return {"lines": 0, "started": 0, "grown": 0, "deepest": 0, "average": 0}
    return ladder.summary(drill_id, drill.lines, drill.user_color == chess.WHITE)


def ladder_trie(drill_id: str, drill: Drill) -> list[dict]:
    if not drill.lines:
        return []
    return ladder.trie(drill_id, drill.lines, drill.user_color == chess.WHITE)


def run_depth(drill_id: str, drill: Drill | None, script: list[str]) -> int:
    """How many of your moves this run is graded over: the rung the line you
    are being shown is waiting on. Off the drills, or with no line in play,
    fall back to a fixed length."""
    if drill is None or not drill.lines or not script:
        return MIXED_DEPTH
    is_white = drill.user_color == chess.WHITE
    reach = ladder_mod.line_length(script, is_white)
    if not reach:
        return MIXED_DEPTH
    return min(reach, ladder.depth_of(drill_id, script, is_white))


def seed_ladder_from_history(drills: dict[str, Drill]) -> None:
    """First run only: credit every line your practice log shows you passing,
    to the depth you passed it at."""
    if ladder.has_state():
        return
    analysis = analyze_practice_log()
    grown = 0
    for gid in analysis["order"]:
        res = analysis["results"][gid]
        if not res["verdict"]:
            continue
        drill_id = (analysis["starts"].get(gid) or {}).get("drill")
        drill = drills.get(drill_id or "")
        if drill is None or not drill.lines or drill_id.startswith("practice:"):
            continue
        is_white = res["orientation"] == "white"
        depth = ladder_mod.line_length(res["sans"], is_white)
        if depth:
            ladder.record_pass(drill_id, res["sans"], is_white, depth)
            grown += 1
    print(f"ladder seeded from {grown} passed runs", flush=True)


def hint_ucis(
    payload: dict, board: chess.Board, drill: Drill | None, analysis: EngineWrapper
) -> tuple[list[str], bool, int | None]:
    """(moves to show, whether they are book, best eval) for a hint.

    Inside the drill's own book the answer is the prep, not the engine's pick:
    the course guard is about to bounce anything else, so hinting the engine
    there would be telling you to play a move the trainer refuses."""
    book = expected_ucis(drill, list(payload.get("history", [])), board)
    if book and len(book) == 1:
        return book, True, None
    # read the table the badges come from, so the move you are told to play is
    # the move that earns the star — and so every equally good move is offered
    table = compute_move_classes(payload["fen"], analysis)
    if book:  # strongest prep first
        ranked = sorted(book, key=lambda u: table["moves"].get(u, {}).get("loss", 1.0))
        return ranked[:HINT_MOVES], True, table["best_cp"]
    return table["best_ucis"][:HINT_MOVES], False, table["best_cp"]


def handle_hint(
    payload: dict,
    drills: dict[str, Drill],
    analysis: EngineWrapper,
    mistake_index: MistakeIndex,
) -> dict:
    board = chess.Board(payload["fen"])
    drill = drills.get(payload.get("drill", ""))
    ucis, book, eval_cp = hint_ucis(payload, board, drill, analysis)
    if not ucis:
        return {"error": "no move available"}
    moves = [
        {"uci": uci, "san": board.san(chess.Move.from_uci(uci))} for uci in ucis
    ]
    if payload.get("practice"):
        # asking for help at a known mistake position blocks the 'fixed' credit
        fen_key = mistake_index.get().epd_to_fen.get(board.epd())
        if fen_key:
            record_mistake_result(fen_key, False)
    log_event(
        "hint", fen=payload["fen"], san=moves[0]["san"], book=book,
        alts=len(moves) - 1, mode=payload.get("mode"),
    )
    return {"moves": moves, "book": book, "eval_cp": eval_cp}


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
                "ladder": ladder_state(drill_id, d),
                "book": ladder_trie(drill_id, d),
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
    training = training_summary()
    name_to_id = {d.name: did for did, d in drills.items() if did.startswith(f"{user}/")}
    for e in training.get("openings", []):
        e["drill_id"] = name_to_id.get(f"{e['family']} — {e['color'].capitalize()}")
    return {
        "drills": entries,
        "user": user,
        "puzzles": sets,
        "training": training,
        "profile": fetch_profile(user),
    }


def handle_result(payload: dict, drills: dict[str, Drill]) -> dict:
    """The client reports a finished run. A pass grows the line it played by
    one rung; nothing else in the opening moves."""
    drill_id = str(payload.get("drill", ""))
    drill = drills.get(drill_id)
    passed = bool(payload.get("passed"))
    depth = int(payload.get("depth") or 0)
    # every finished run is logged, drill or not — the home-screen stats read
    # these verdicts rather than trying to recompute them
    if drill is None or not drill.lines:
        log_event("run_result", drill=drill_id or None, game=payload.get("game"),
                  passed=passed, depth=depth or None)
        return {"ladder": None}
    history = list(payload.get("history") or [])
    is_white = drill.user_color == chess.WHITE
    if not passed or not depth:
        log_event("run_result", drill=drill_id, game=payload.get("game"),
                  passed=False, depth=depth or None)
        return {"ladder": ladder_state(drill_id, drill), "grew": False}
    grew = ladder.record_pass(drill_id, history, is_white, depth)
    log_event("run_result", drill=drill_id, game=payload.get("game"), passed=True,
              depth=depth, marked=grew["marked"])
    return {
        "ladder": ladder_state(drill_id, drill),
        "grew": bool(grew["marked"]),
        "depth": depth,
        "next": grew["next"],
    }


def freshen_note(mistake_index: MistakeIndex, board: chess.Board, reply: dict) -> None:
    """Replace line-baked notes with the truth about the reached position."""
    if reply.get("source") != "book":
        return
    stats = mistake_index.get().position_stats.get(board.epd())
    n = int(stats[0]) if stats else 0
    if n >= 2:
        reply["note"] = f"You've faced this {n} times (scoring {100.0 * stats[1] / n:.0f}%)."
    elif reply.get("note") and "faced this" in reply["note"]:
        reply["note"] = None
    elif reply.get("note") and "Beyond your games" in reply["note"] and n >= 1:
        reply["note"] = None


classify_lock = threading.Lock()
classify_cache: dict[str, dict] = {}

# A move within this much win probability of the engine's pick is the same
# move as far as a player is concerned: it earns the star too, and the hint
# offers the whole set rather than pretending there is one answer.
TIE_LOSS = 0.005
# How many moves get the deep look. Only the top of the list is ever visible
# as a star or a hint; everything below it just needs to be sorted into
# inaccuracy / mistake / blunder, which takes no depth at all.
CANDIDATE_PVS = 5
CANDIDATE_TIME = 0.7
SWEEP_TIME = 0.35
# a hint naming more than a few moves stops being a hint
HINT_MOVES = 3


def empty_class_table() -> dict:
    return {"moves": {}, "best_uci": None, "best_ucis": [], "best_cp": 0,
            "second_cp": None, "best_pv": []}


def compute_move_classes(fen: str, analysis: EngineWrapper) -> dict:
    """Classify every legal move, cached per position so the client can badge
    instantly, handle_move can skip its probes, and the hint can answer from
    the same numbers the badges come from.

    Two passes, because a flat multipv sweep over every root move runs ~6 plies
    shallower than a normal search and reorders the top of the list by a couple
    of centipawns — which is exactly what used to make the hint recommend a
    move the badge then refused to call best."""
    with classify_lock:
        cached = classify_cache.get(fen)
    if cached:
        return cached
    board = chess.Board(fen)
    color = board.turn
    n = board.legal_moves.count()
    if n == 0:
        return empty_class_table()
    top = analysis.analyse_multipv(board, color, min(CANDIDATE_PVS, n), CANDIDATE_TIME)
    if not top:
        return empty_class_table()
    scored = {uci: cp for uci, cp, _pv in top}
    if n > len(top):
        # by the deeper search these moves are all worse than the weakest
        # candidate, so clamp them there — shallow noise can't promote a move
        # past one that was actually searched
        floor_cp = top[-1][1]
        for uci, cp, _pv in analysis.analyse_multipv(board, color, n, SWEEP_TIME):
            scored.setdefault(uci, min(cp, floor_cp))
    best_uci, best_cp, best_pv = top[0]
    second_cp = top[1][1] if len(top) > 1 else None
    only_move = second_cp is not None and (
        expected_points(best_cp) - expected_points(second_cp) >= 0.10
    )
    moves = {}
    for uci, cp in scored.items():
        loss = expected_points(best_cp) - expected_points(cp)
        cls = ("great" if only_move else "best") if loss <= TIE_LOSS \
            else classify_loss(loss)
        moves[uci] = {"class": cls, "loss": round(loss, 4)}
    table = {
        "moves": moves,
        "best_uci": best_uci,
        # every equal-best move, strongest first — what the hint offers
        "best_ucis": sorted(
            (u for u, m in moves.items() if m["loss"] <= TIE_LOSS),
            key=lambda u: scored[u], reverse=True,
        ),
        "best_cp": best_cp,
        "second_cp": second_cp,
        "best_pv": best_pv,
    }
    with classify_lock:
        classify_cache[fen] = table
        while len(classify_cache) > 12:
            classify_cache.pop(next(iter(classify_cache)))
    return table


def handle_classify(payload: dict, analysis: EngineWrapper) -> dict:
    table = compute_move_classes(payload["fen"], analysis)
    return {"moves": {u: v["class"] for u, v in table["moves"].items()}}


def probe_move(
    board: chess.Board, move: chess.Move, analysis: EngineWrapper, fen: str
) -> tuple[str, float, int, list[chess.Move]]:
    """(class, win-probability loss, best eval, best line) for a move.

    Served from the prefetched classification table when it's warm, so the
    common path costs no engine time."""
    mover = board.turn
    with classify_lock:
        table = classify_cache.get(fen)
    if table and move.uci() in table["moves"]:
        entry = table["moves"][move.uci()]
        return (
            entry["class"],
            entry["loss"],
            table["best_cp"],
            [chess.Move.from_uci(u) for u in table["best_pv"]],
        )
    best_cp, second_cp, best_pv = analysis.analyse_top2(board, mover)
    only_move = second_cp is not None and (
        expected_points(best_cp) - expected_points(second_cp) >= 0.10
    )
    if best_pv and move == best_pv[0]:
        return ("great" if only_move else "best", 0.0, best_cp, best_pv)
    board.push(move)
    after_cp = analysis.eval_cp(board, mover, movetime=0.35)
    board.pop()
    loss = expected_points(best_cp) - expected_points(after_cp)
    if loss <= TIE_LOSS:
        return ("great" if only_move else "best", loss, best_cp, best_pv)
    return (classify_loss(loss), loss, best_cp, best_pv)


def expected_ucis(drill: Drill | None, history: list[str], board: chess.Board) -> list[str]:
    """Book moves for the side to move, as UCIs — lets the client badge
    book moves instantly without waiting for the server."""
    if drill is None:
        return []
    out = []
    for san in drill.expected.get(tuple(history)) or []:
        try:
            out.append(board.parse_san(san).uci())
        except ValueError:
            continue
    return out


def practice_targets(mistake_index: MistakeIndex) -> dict[str, int]:
    with progress_lock:
        progress = load_progress()
    return mistake_index.get().downstream_counts(progress)


def handle_new(
    payload: dict, drills: dict[str, Drill], engine: EngineWrapper, mistake_index: MistakeIndex
) -> dict:
    drill_id = payload.get("drill", "")
    if payload.get("practice"):
        options = [k for k in drills if k.startswith("practice:")]
        if options:
            # weight color choice by where the unfixed mistakes are
            with progress_lock:
                prog = load_progress()
            unfixed = {"white": 1, "black": 1}
            mi = mistake_index.get()
            for fen in mi.epd_to_fen.values():
                if not prog.get(fen, {}).get("solved"):
                    side = "white" if fen.split(" ")[1] == "w" else "black"
                    unfixed[side] += 1
            weights = [unfixed[k.split(":")[1]] for k in options]
            drill_id = random.choices(options, weights=weights)[0]
    drill = drills.get(drill_id)
    board = chess.Board(payload["fen"]) if payload.get("fen") else chess.Board()
    intro = drill.intro if drill else "Free play — you move for the side to play."
    start_fen = board.fen()
    pre_moves = []
    game_id = f"g{int(time.time() * 1000):x}{random.randrange(16 ** 4):04x}"
    script = payload.get("script") or []
    if not script and drill is not None and drill.lines:
        # show a line, shallowest first, so the repertoire broadens before it
        # deepens. Nothing is locked: a line you decline simply waits.
        target_line, _ = ladder.next_line(
            drill_id, drill.lines, drill.user_color == chess.WHITE
        )
        if target_line:
            script = target_line
    if drill is not None and board.turn != drill.user_color:
        reply = None
        if script:
            try:
                mv = board.parse_san(script[0])
                san = board.san(mv)
                board.push(mv)
                reply = {"reply_san": san, "reply_uci": mv.uci(), "source": "repeat", "note": None}
            except ValueError:
                reply = None
        if reply is None:
            reply = opponent_reply(
                board, [], drill, engine, practice_targets(mistake_index),
                game_id=game_id,
                fail_next=failed_next_moves(drill, drill_id, []),
            )
        freshen_note(mistake_index, board, reply)
        pre_moves.append(
            {
                "san": reply["reply_san"],
                "uci": reply["reply_uci"],
                "fen": board.fen(),
                "note": reply.get("note"),
            }
        )
    log_event(
        "game_start",
        game=game_id,
        drill=drill_id or None,
        orientation="black" if drill and drill.user_color == chess.BLACK else "white",
        pre_moves=[m["san"] for m in pre_moves],
        repeat_of=payload.get("repeat_of"),
    )
    return {
        **game_state(board),
        "message": intro,
        "game_id": game_id,
        "script": script or None,
        "book_ucis": expected_ucis(drill, [m["san"] for m in pre_moves], board),
        "opening": opening_name([m["san"] for m in pre_moves]),
        "drill_id": drill_id if drill else None,
        "drill_name": drill.name if drill else None,
        "orientation": "black" if drill and drill.user_color == chess.BLACK else "white",
        "start_fen": start_fen,
        "pre_moves": pre_moves,
        "eval_cp": 0,
        "ladder": ladder_state(drill_id, drill) if drill is not None else None,
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

    # The run is steered at a line you have not cleared yet. A line is the
    # opponent's sequence, so your own move does not have to match the book's —
    # play something else and the scripted reply still stands if it is sound
    # here. It is only when that reply stops being sound (the Italian's
    # 4...Nxe4 after anything but 4.Ng5 or 4.d4) that the line goes out of
    # reach and the rung stalls. Name it either way rather than let you guess.
    line_note = None
    script_now = payload.get("script") or []
    ply = len(history)
    if (
        drill is not None
        and len(script_now) > ply + 1
        and script_now[:ply] == history
        and script_now[ply] != user_san
    ):
        line_note = (
            f"Open line here: {script_now[ply]} {script_now[ply + 1]}. "
            f"{user_san} can still reach it if {script_now[ply + 1]} holds up."
        )

    prep_note = None
    mistake_fen = None
    in_prep = None
    move_class = None
    move_loss = 0.0
    move_verified = False  # the move was the engine's own pick — can't lose ground
    if drill is not None:
        mistake_fen = mistake_index.get().epd_to_fen.get(board.epd())
        expected = drill.expected.get(tuple(history))
        in_prep = bool(expected) and user_san in expected
        focused = not str(payload.get("drill", "")).startswith("practice:")
        # only steer while the drill's opening is still reachable from here —
        # once the opponent has left it for good there is no course to hold
        if focused and expected and not in_prep and tuple(history) in drill.on_family:
            # enforce until the position IS this opening (ECO family). A
            # non-book move is still fine if it lands in the family itself.
            def fam(sans):
                op = opening_name(sans)
                return op["name"].split(":")[0].strip() if op else None
            if fam(history) != drill.family and fam(history + [user_san]) != drill.family:
                prep = " or ".join(sorted(expected))
                log_event(
                    "move", game=payload.get("game"), drill=payload.get("drill"),
                    fen=payload["fen"], san=user_san, uci=move.uci(),
                    ply=len(history), rejected=True, redirect=True,
                )
                return {
                    **game_state(board),
                    "rejected": True,
                    "redirect": True,
                    "warning": (
                        f"Not {article(drill.family)} {drill.family} yet — "
                        f"play {prep} to stay on course."
                    ),
                }
        in_opening = len(history) < OPENING_CHECK_PLIES
        if in_prep or expected or mistake_fen or in_opening:
            prep = " or ".join(sorted(expected)) if expected else None
            # book moves are measured like any other: the drill lines are built
            # from your own games, so the prep itself can be the leak. The badge
            # stays "book" unless the move is genuinely bad.
            probe_class, move_loss, best_cp, best_pv = probe_move(
                board, move, analysis, payload["fen"]
            )
            move_verified = probe_class in ("best", "great")
            move_class = probe_class
            if in_prep and probe_class not in ("mistake", "blunder"):
                move_class = "book"
            # every move plays — the eval bar is the judge. Bad moves are
            # still captured as exercises and steer future practice.
            if in_opening and move_class in ("mistake", "blunder"):
                if mistake_fen:
                    record_mistake_result(mistake_fen, False)
                    prep_note = "This spot has cost you in a real game too."
                elif best_pv:
                    record_live_mistake(
                        payload["fen"], user_san, board.san(best_pv[0]),
                        best_pv[0].uci(), best_cp, history,
                    )
                    record_mistake_result(payload["fen"], False)
            if prep and not in_prep:
                prep_note = ((prep_note + " ") if prep_note else "") + f"Book here: {prep}."
    fixed_now = False
    if mistake_fen and move_class not in ("inaccuracy", "mistake", "blunder"):
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
            fixed=fixed_now, game_over=True, move_class=move_class,
        )
        return {
            **game_state(board),
            "user_san": user_san,
            "fen_after_user": fen_after_user,
            "history": history,
            "prep_note": prep_note,
            "line_note": line_note,
            "move_class": move_class,
        }

    if drill is not None:
        user_moves = (
            (len(history) + 1) // 2 if drill.user_color == chess.WHITE
            else len(history) // 2
        )
        graded_depth = run_depth(
            str(payload.get("drill", "")), drill, payload.get("script") or []
        )
        if user_moves == graded_depth:
            # the run ends on the user's last graded move — no engine reply yet
            eval_cp = analysis.eval_cp(board, chess.WHITE, movetime=0.3)
            log_event(
                "move", game=payload.get("game"), drill=payload.get("drill"),
                fen=payload["fen"], san=user_san, uci=move.uci(),
                ply=len(history) - 1, rejected=False, prep=in_prep,
                mistake=bool(mistake_fen), fixed=fixed_now,
                eval_cp=eval_cp, move_class=move_class,
            )
            op = opening_name(history)
            family_record = None
            if op:
                fam = op["name"].split(":")[0].strip()
                color_name = "white" if drill.user_color == chess.WHITE else "black"
                for o in training_summary().get("openings", []):
                    if o["family"] == fam and o["color"] == color_name:
                        family_record = o
                        break
            return {
                **game_state(board),
                "user_san": user_san,
                "fen_after_user": fen_after_user,
                "history": history,
                "prep_note": prep_note,
                "line_note": line_note,
                "move_class": move_class,
                "loss": round(move_loss, 4),
                "move_verified": move_verified,
                "opening": op,
                "eval_cp": eval_cp,
                "opening_complete": True,
                "family_record": family_record,
                "ladder": ladder_state(str(payload.get("drill", "")), drill)
                if drill is not None else None,
                "depth": graded_depth,
            }

    scripted = payload.get("script") or []
    reply = None
    if drill is not None and len(scripted) > len(history):
        # Follow the scripted line by position rather than by exact history
        # match, so your own choice of a sound alternative does not knock the
        # opponent off the line you are trying to clear.
        #
        # But a scripted move was chosen for the position the line expected. If
        # you left that line, replaying it blind hands you material — O-O into
        # a fork, Qg6 en prise. Off-script, the move only stands if it is still
        # sound here; otherwise the opponent thinks for itself.
        try:
            mv = board.parse_san(scripted[len(history)])
        except ValueError:
            mv = None
        if mv is not None and scripted[: len(history)] != history:
            _, loss, _, _ = probe_move(board, mv, analysis, board.fen())
            if loss > SCRIPT_MAX_LOSS:
                mv = None
        if mv is not None:
            san = board.san(mv)
            board.push(mv)
            reply = {"reply_san": san, "reply_uci": mv.uci(), "source": "repeat", "note": None}
    if reply is None:
        hot = practice_targets(mistake_index) if drill is not None else None
        fail_next = (
            failed_next_moves(drill, str(payload.get("drill", "")), history)
            if drill is not None else None
        )
        reply = opponent_reply(
            board, history, drill, engine, hot,
            strong=analysis if drill is not None else None,
            game_id=payload.get("game"),
            fail_next=fail_next,
        )
    if drill is not None:
        freshen_note(mistake_index, board, reply)
    history.append(reply["reply_san"])
    eval_cp = analysis.eval_cp(board, chess.WHITE, movetime=0.3)
    log_event(
        "move", game=payload.get("game"), drill=payload.get("drill"),
        fen=payload["fen"], san=user_san, uci=move.uci(), ply=len(history) - 2,
        rejected=False, prep=in_prep, mistake=bool(mistake_fen), fixed=fixed_now,
        reply=reply["reply_san"], source=reply["source"], eval_cp=eval_cp,
        move_class=move_class,
    )
    return {
        **game_state(board),
        "user_san": user_san,
        "fen_after_user": fen_after_user,
        "history": history,
        "prep_note": prep_note,
        "line_note": line_note,
        "move_class": move_class,
        "loss": round(move_loss, 4),
        "move_verified": move_verified,
        "book_ucis": expected_ucis(drill, history, board),
        "opening": opening_name(history) if drill is not None else None,
        "eval_cp": eval_cp,
        **reply,
    }


def handle_reply(
    payload: dict,
    drills: dict[str, Drill],
    engine: EngineWrapper,
    analysis: EngineWrapper,
    mistake_index: MistakeIndex,
) -> dict:
    """Opponent moves without a user move — used after Play On at a verdict."""
    drill = drills.get(payload.get("drill", ""))
    board = chess.Board(payload["fen"])
    history = list(payload.get("history", []))
    hot = practice_targets(mistake_index) if drill is not None else None
    fail_next = (
        failed_next_moves(drill, str(payload.get("drill", "")), history)
        if drill is not None else None
    )
    reply = opponent_reply(
        board, history, drill, engine, hot,
        strong=analysis if drill is not None else None,
        game_id=payload.get("game"),
        fail_next=fail_next,
    )
    if drill is not None:
        freshen_note(mistake_index, board, reply)
    history.append(reply["reply_san"])
    return {
        **game_state(board),
        "history": history,
        "eval_cp": analysis.eval_cp(board, chess.WHITE, movetime=0.3),
        "book_ucis": expected_ucis(drill, history, board),
        "opening": opening_name(history) if drill is not None else None,
        **reply,
    }


_eco_book: dict | None = None


def eco_book() -> dict:
    global _eco_book
    if _eco_book is None:
        from opening_report import load_eco_book
        _eco_book = load_eco_book(ROOT / "data")
    return _eco_book


def opening_name(history: list[str]) -> dict | None:
    """Deepest ECO classification reached by the game so far."""
    book = eco_book()
    board = chess.Board()
    hit = None
    for san in history[:24]:
        try:
            board.push_san(san)
        except ValueError:
            break
        entry = book.get(board.epd())
        if entry:
            hit = {"eco": entry[0], "name": entry[1]}
    return hit


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
    hinted = bool(payload.get("hinted"))
    with progress_lock:
        first_attempt = p["fen"] in pending_first_attempt
        if first_attempt:
            pending_first_attempt.discard(p["fen"])
            progress = load_progress()
            entry = progress.get(p["fen"], {"solved": False, "misses": 0})
            if correct and not hinted:
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

        def end_headers(self):
            # dev server: never let the browser serve stale app files
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                drills = drill_cache.get()
                if self.path == "/api/drills":
                    body = handle_drills(drills, user)
                elif self.path == "/api/classify":
                    body = handle_classify(payload, analysis)
                elif self.path == "/api/new":
                    body = handle_new(payload, drills, engine, mistake_index)
                elif self.path == "/api/move":
                    body = handle_move(payload, drills, engine, analysis, mistake_index)
                elif self.path == "/api/reply":
                    body = handle_reply(payload, drills, engine, analysis, mistake_index)
                elif self.path == "/api/result":
                    body = handle_result(payload, drills)
                elif self.path == "/api/hint":
                    body = handle_hint(payload, drills, analysis, mistake_index)
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

    global USER
    USER = args.user
    drill_cache = DrillCache(args.user)
    seed_ladder_from_history(drill_cache.get())
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
