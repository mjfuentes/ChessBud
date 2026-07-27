"""Per-opening depth ladder.

Each opening carries its own depth — how many of your moves a run is graded
over. Your blitz rating sets the ceiling (floor(rating/100)); an opening climbs
toward it one move at a time, and only when you have cleared *every* book line
at the current depth. Openings you have already proven start where you proved
them; untouched ones start shallow, where success is reachable.

State lives in data/ladder.json and is keyed by drill id.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

MIN_DEPTH = 4
MAX_DEPTH = 15
DEFAULT_TARGET = 10  # used when the rating is unknown (offline)


def target_depth(rating: int | None) -> int:
    """The ceiling every opening climbs toward: one move per 100 rating."""
    if not rating:
        return DEFAULT_TARGET
    return max(MIN_DEPTH, min(MAX_DEPTH, rating // 100))


def plies_at_depth(depth: int, user_is_white: bool) -> int:
    """Plies played when the user has just made their `depth`-th move."""
    return 2 * depth - 1 if user_is_white else 2 * depth


def line_key(history: list[str], depth: int, user_is_white: bool) -> str | None:
    """Identity of a line: the moves the OPPONENT played, not your answers.

    A line is the challenge you were set. Which good move you chose to meet it
    is what the pass verdict already judges — keying on your moves too would
    mean an equally sound alternative never ticked anything off.
    """
    n = plies_at_depth(depth, user_is_white)
    if len(history) < n:
        return None
    return " ".join(history[1:n:2] if user_is_white else history[0:n:2])


def lines_at_depth(lines: list[list[str]], depth: int, user_is_white: bool) -> set[str]:
    """Every distinct challenge the book can pose at this depth."""
    keys = {line_key(sans, depth, user_is_white) for sans in lines}
    return {k for k in keys if k is not None}


def earned_depth(entry: dict, lines: list[list[str]], is_white: bool) -> int:
    """The rung promotion would leave an opening on: the deepest one whose
    every book line is cleared, plus one."""
    cleared = entry.get("cleared", {})
    depth = MIN_DEPTH
    for d in range(1, MAX_DEPTH + 1):
        total = lines_at_depth(lines, d, is_white)
        if not total:
            break
        done = [k for k in cleared.get(str(d), []) if k in total]
        if len(done) < len(total):
            return max(MIN_DEPTH, d)
        depth = d + 1
    return max(MIN_DEPTH, depth)


def deepest_supported(lines: list[list[str]], user_is_white: bool) -> int:
    """How deep the drill data can honestly take you.

    Not simply the longest line: a book thins out as it deepens, because most
    lines end while a few carry an engine tail. A rung where only one or two
    lines survive is easier than the rung below it, which makes the ladder run
    backwards. So the ceiling is the deepest rung that still holds at least
    half the widest coverage this opening ever offers.
    """
    counts = {}
    for depth in range(1, MAX_DEPTH + 1):
        n = len(lines_at_depth(lines, depth, user_is_white))
        if n:
            counts[depth] = n
    if not counts:
        return 0
    floor = max(2, max(counts.values()) / 2)
    honest = [d for d, n in counts.items() if n >= floor]
    # a drill with a single line everywhere has no breadth to measure; let it
    # run to the end of its book rather than pinning it at depth 1
    return max(honest) if honest else max(counts)


class Ladder:
    """Depth and cleared-line state per drill, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._state = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._state = {}
        self._loaded = True

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=1, sort_keys=True), encoding="utf-8")
        tmp.rename(self.path)

    def seed(
        self,
        passed_runs: list[tuple[str, list[str], bool]],
        book: dict[str, list[list[str]]],
    ) -> bool:
        """Build initial state from practice history, once.

        `passed_runs` is (drill_id, history, user_is_white) for every run that
        passed. Passes are credited to the rungs whose book lines they match,
        and the starting depth is then the same thing promotion means: the
        deepest rung where EVERY line is cleared, plus one. Seeding on "any
        pass reached this far" instead put openings ten rungs up with two
        leaves under them.
        """
        with self._lock:
            self._load()
            if self._state:
                return False
            colours: dict[str, bool] = {}
            for drill_id, history, is_white in passed_runs:
                lines = book.get(drill_id)
                if not lines:
                    continue
                colours[drill_id] = is_white
                entry = self._state.setdefault(drill_id, {"depth": MIN_DEPTH, "cleared": {}})
                for depth in range(1, MAX_DEPTH + 1):
                    key = line_key(history, depth, is_white)
                    if key is None:
                        break
                    if key not in lines_at_depth(lines, depth, is_white):
                        continue
                    bucket = entry["cleared"].setdefault(str(depth), [])
                    if key not in bucket:
                        bucket.append(key)
            for drill_id, entry in self._state.items():
                entry["depth"] = earned_depth(
                    entry, book.get(drill_id) or [], colours.get(drill_id, True)
                )
            self._save()
            return True

    def state(
        self, drill_id: str, lines: list[list[str]], is_white: bool, target: int
    ) -> dict:
        """Current rung for a drill, clamped to the rating target and to what
        the drill data can support."""
        with self._lock:
            self._load()
            entry = self._state.get(drill_id) or {"depth": MIN_DEPTH, "cleared": {}}
            supported = deepest_supported(lines, is_white)
            cap = min(target, supported) if supported else target
            depth = max(1, min(entry.get("depth", MIN_DEPTH), cap))
            total = lines_at_depth(lines, depth, is_white)
            cleared = [k for k in entry.get("cleared", {}).get(str(depth), []) if k in total]
            return {
                "depth": depth,
                "cleared": len(cleared),
                "total": len(total),
                "target": target,
                "supported": supported,
                "at_ceiling": depth >= cap,
            }

    def rungs(
        self, drill_id: str, lines: list[list[str]], is_white: bool, depth: int
    ) -> list[dict]:
        """Cleared/total for every rung up to and including the current one.

        The branch drawn on the home screen uses this as its depth axis: rungs
        below the tip keep the foliage you earned there.
        """
        with self._lock:
            self._load()
            cleared = self._state.get(drill_id, {}).get("cleared", {})
        out = []
        for d in range(1, depth + 1):
            total = lines_at_depth(lines, d, is_white)
            if not total:
                continue
            done = [k for k in cleared.get(str(d), []) if k in total]
            out.append({"depth": d, "cleared": len(done), "total": len(total)})
        return out

    def trie(
        self, drill_id: str, lines: list[list[str]], is_white: bool, depth: int
    ) -> list[dict]:
        """The book as a nested tree of the opponent's choices.

        Every fork here is a real fork in your preparation, and a node's key is
        the line identity at its own depth — so a node knows whether you have
        cleared it. This is what the home screen draws: the branching is the
        data, not decoration.
        """
        with self._lock:
            self._load()
            cleared = self._state.get(drill_id, {}).get("cleared", {})
        done = {int(d): set(keys) for d, keys in cleared.items()}
        roots: list[dict] = []
        index: dict[str, dict] = {}
        for d in range(1, depth + 1):
            for sans in lines:
                key = line_key(sans, d, is_white)
                if key is None or key in index:
                    continue
                moves = key.split()
                node = {
                    "san": moves[-1] if moves else "",
                    "d": d,
                    "on": key in done.get(d, set()),
                    "kids": [],
                }
                index[key] = node
                parent_key = " ".join(moves[:-1])
                parent = index.get(parent_key) if moves[:-1] else None
                (parent["kids"] if parent else roots).append(node)
        return roots

    def record_pass(
        self, drill_id: str, history: list[str], lines: list[list[str]],
        is_white: bool, target: int,
    ) -> dict:
        """Tick off the line just passed; promote when the depth is complete."""
        before = self.state(drill_id, lines, is_white, target)
        depth = before["depth"]
        key = line_key(history, depth, is_white)
        if key is None or key not in lines_at_depth(lines, depth, is_white):
            # the opponent ended up somewhere the book does not cover at this
            # depth, so there is no line to tick off
            return {**before, "promoted": False, "off_book": True}
        with self._lock:
            self._load()
            entry = self._state.setdefault(drill_id, {"depth": depth, "cleared": {}})
            entry["depth"] = depth
            bucket = entry["cleared"].setdefault(str(depth), [])
            if key not in bucket:
                bucket.append(key)
            total = lines_at_depth(lines, depth, is_white)
            done = len([k for k in bucket if k in total])
            supported = deepest_supported(lines, is_white)
            cap = min(target, supported) if supported else target
            promoted = done >= len(total) and depth < cap
            if promoted:
                entry["depth"] = depth + 1
            self._save()
        after = self.state(drill_id, lines, is_white, target)
        return {**after, "promoted": promoted, "cleared_now": key}

    def next_line(
        self, drill_id: str, lines: list[list[str]], is_white: bool, target: int
    ) -> list[str] | None:
        """A full book line whose depth-key you have not cleared yet — what the
        opponent should steer into next. Longest first, so the run has book
        cover for the whole graded stretch."""
        st = self.state(drill_id, lines, is_white, target)
        depth = st["depth"]
        with self._lock:
            self._load()
            done = set(self._state.get(drill_id, {}).get("cleared", {}).get(str(depth), []))
        candidates = []
        for sans in lines:
            key = line_key(sans, depth, is_white)
            if key is not None and key not in done:
                candidates.append(sans)
        if not candidates:
            return None
        # rotate through what is left rather than always serving the longest —
        # a line you keep failing would otherwise be the only thing you ever see
        return random.choice(candidates)
