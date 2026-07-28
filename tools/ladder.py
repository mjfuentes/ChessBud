"""Per-line depth.

Every line carries its own depth — how many of your moves you have played
through it cleanly — and grows one move at a time as you clear it. Nothing is
gated: an opening is not a level you unlock, it is a set of lines you have each
taken as far as you have taken them. Decline a line and it simply stays where
it is, waiting, while the ones you do play keep growing.

A line is identified by the opponent's moves, so meeting it with a different
sound move of your own still counts as having played it.

State lives in data/ladder.json, keyed by drill id: cleared line keys per depth.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

MIN_DEPTH = 3    # a fresh line starts here — enough to be a real test
MAX_DEPTH = 15


def plies_at_depth(depth: int, user_is_white: bool) -> int:
    """Plies played when the user has just made their `depth`-th move."""
    return 2 * depth - 1 if user_is_white else 2 * depth


def line_key(history: list[str], depth: int, user_is_white: bool) -> str | None:
    """Identity of a line at a given depth: the moves the OPPONENT played.

    Which sound move you chose to meet it is what the verdict already judges;
    keying on your moves too would mean an equally good alternative never
    ticked anything off.
    """
    n = plies_at_depth(depth, user_is_white)
    if len(history) < n:
        return None
    return " ".join(history[1:n:2] if user_is_white else history[0:n:2])


def line_length(line: list[str], user_is_white: bool) -> int:
    """The deepest rung this line's book moves can carry."""
    for depth in range(MAX_DEPTH, 0, -1):
        if len(line) >= plies_at_depth(depth, user_is_white):
            return depth
    return 0


def lines_at_depth(lines: list[list[str]], depth: int, user_is_white: bool) -> set[str]:
    keys = {line_key(sans, depth, user_is_white) for sans in lines}
    return {k for k in keys if k is not None}


class Ladder:
    """Cleared line keys per depth, persisted as JSON."""

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

    def has_state(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._state)

    def _cleared(self, drill_id: str) -> dict[str, set[str]]:
        self._load()
        raw = self._state.get(drill_id, {}).get("cleared", {})
        return {d: set(keys) for d, keys in raw.items()}

    def depth_of(
        self, drill_id: str, line: list[str], is_white: bool, cleared=None
    ) -> int:
        """The rung this line is waiting on: the shallowest one not yet cleared.

        Depth only ever advances one rung at a time, so a line you have never
        touched opens at MIN_DEPTH and a line you have played to move 8 offers
        move 9 next.
        """
        done = self._cleared(drill_id) if cleared is None else cleared
        cap = line_length(line, is_white)
        for depth in range(1, min(cap, MAX_DEPTH) + 1):
            key = line_key(line, depth, is_white)
            if key is None:
                break
            if key not in done.get(str(depth), set()):
                return max(MIN_DEPTH, depth) if depth < MIN_DEPTH else depth
        return min(cap, MAX_DEPTH) + 1 if cap else MIN_DEPTH

    def summary(self, drill_id: str, lines: list[list[str]], is_white: bool) -> dict:
        """What this opening looks like as a whole — for the list, not for
        gating anything.

        `grown` counts the leaves the tree actually draws, so the number on the
        home screen and the foliage on the branch are the same thing. Counting
        stored keys instead would include lines the book no longer has (the
        regeneration replaced them) and rungs beyond where a line has reached.
        """
        with self._lock:
            done = self._cleared(drill_id)
            depths = [self.depth_of(drill_id, ln, is_white, done) for ln in lines]

        def leaves(nodes: list[dict]) -> int:
            return sum((1 if n["on"] else 0) + leaves(n["kids"]) for n in nodes)

        return {
            "lines": len(lines),
            "started": sum(1 for d in depths if d > MIN_DEPTH),
            "grown": leaves(self.trie(drill_id, lines, is_white)),
            "deepest": max(depths, default=0) - 1,
            "average": round(sum(d - 1 for d in depths) / len(depths), 1) if lines else 0,
        }

    def record_pass(
        self, drill_id: str, history: list[str], is_white: bool, depth: int,
        lines: list[list[str]] | None = None,
    ) -> dict:
        """Mark a line cleared to `depth`.

        Every shallower rung is marked too: reaching move 8 cleanly means the
        moves before it were played cleanly, and it keeps 'the shallowest rung
        not yet cleared' honest as the line's depth.

        Only keys the book actually contains are recorded. A run where the
        opponent left the book ends on a sequence no line has, and storing it
        grew the state while the tree — which counts book lines — stayed
        exactly the same.
        """
        with self._lock:
            self._load()
            entry = self._state.setdefault(drill_id, {"cleared": {}})
            marked = []
            for d in range(1, depth + 1):
                key = line_key(history, d, is_white)
                if key is None:
                    break
                if lines is not None and key not in lines_at_depth(lines, d, is_white):
                    continue
                bucket = entry["cleared"].setdefault(str(d), [])
                if key not in bucket:
                    bucket.append(key)
                    marked.append(d)
            self._save()
        return {"depth": depth, "marked": marked, "next": depth + 1,
                "off_book": lines is not None and not marked}

    def depth_for_history(
        self, drill_id: str, lines: list[list[str]], is_white: bool,
        history: list[str],
    ) -> int | None:
        """The rung the line you are ACTUALLY on is waiting for.

        A run is steered at one line, but your own moves decide which line
        happens: answer with something else and the opponent's reply changes
        with it. Grading at the served line's depth then finishes runs on a
        line that was already cleared, which passes and marks nothing.
        """
        played = history[1::2] if is_white else history[0::2]
        best = None
        with self._lock:
            done = self._cleared(drill_id)
            for line in lines:
                theirs = line[1::2] if is_white else line[0::2]
                if theirs[:len(played)] != played:
                    continue
                depth = self.depth_of(drill_id, line, is_white, done)
                if depth <= line_length(line, is_white):
                    best = depth if best is None else min(best, depth)
        return best

    def next_line(
        self, drill_id: str, lines: list[list[str]], is_white: bool
    ) -> tuple[list[str] | None, int]:
        """A line to play, and the rung it is waiting on.

        Weighted towards the lines you have taken furthest, so a session keeps
        building rather than resetting: serving the shallowest first meant that
        the moment your worked lines reached move 6, the untouched ones still
        at move 3 became "next" and every run got short again.

        The weight is only linear, so new lines keep surfacing — a line you
        never play simply waits at its own depth, blocking nothing.
        """
        with self._lock:
            done = self._cleared(drill_id)
            scored = [
                (ln, self.depth_of(drill_id, ln, is_white, done))
                for ln in lines
                if line_length(ln, is_white) >= MIN_DEPTH
            ]
        open_lines = [(ln, d) for ln, d in scored if d <= line_length(ln, is_white)]
        if not open_lines:
            return (random.choice(lines) if lines else None, MIN_DEPTH)
        return random.choices(open_lines, weights=[d for _, d in open_lines])[0]

    def trie(
        self, drill_id: str, lines: list[list[str]], is_white: bool
    ) -> list[dict]:
        """The book as a nested tree of the opponent's choices, each line drawn
        only as deep as you have taken it, plus the one rung it is offering
        next. Every fork is a real fork in your preparation."""
        with self._lock:
            done = self._cleared(drill_id)
        roots: list[dict] = []
        index: dict[str, dict] = {}
        for line in lines:
            reach = min(self.depth_of(drill_id, line, is_white, done),
                        line_length(line, is_white))
            for d in range(1, reach + 1):
                key = line_key(line, d, is_white)
                if key is None or key in index:
                    continue
                moves = key.split()
                node = {
                    "san": moves[-1] if moves else "",
                    "d": d,
                    "on": key in done.get(str(d), set()),
                    "kids": [],
                }
                index[key] = node
                parent = index.get(" ".join(moves[:-1])) if moves[:-1] else None
                (parent["kids"] if parent else roots).append(node)
        return roots
