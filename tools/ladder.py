"""What you have grown, node by node.

The tree is a tree of move paths. A node is a position reached by a sequence of
moves, and it carries a leaf once you have played your way onto it with a move
worth keeping — book, best, great or excellent. Leaves belong to positions, not
to runs, so re-walking familiar theory grows nothing and only going further
changes the tree.

A run lasts as long as your moves are worth keeping. The first merely good move
ends it on a pass; an inaccuracy or worse ends it on a fail. Depth is therefore
an outcome rather than a target: how far you got, not how far you were sent.

Nodes off the book count too. When the opponent leaves theory your reply cannot
be book, and the position you reach is still somewhere you played well — so it
grows a leaf and the tree draws the branch that carries it, as yours rather
than as published theory.

The tree that gets drawn is not the whole book. It is what you have grown plus
one ply of book past it, so the picture is a record of your practice with a
frontier on it, and it fills out as you play rather than starting complete.

State lives in data/ladder.json: the move paths you have grown, per drill.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

# moves that keep a run alive and earn the position they reach a leaf
KEEPS_GOING = ("book", "best", "great", "excellent")
# a move at or below this ends the run: "good" passes, the rest fail
PASSES_AND_STOPS = ("good",)
# how much book the tree draws past where you have reached: one ply, so every
# position you have stood in shows the replies still waiting on it
GROWTH_MARGIN = 1


def path_key(history: list[str]) -> str:
    return " ".join(history)


def line_length(line: list[str], user_is_white: bool) -> int:
    """How many of your moves a line contains."""
    return (len(line) + 1) // 2 if user_is_white else len(line) // 2


def user_nodes(line: list[str], user_is_white: bool) -> list[str]:
    """The path key after each of YOUR moves along a line — the nodes a leaf
    can sit on, since a leaf marks a move you played."""
    first = 0 if user_is_white else 1
    return [path_key(line[: i + 1]) for i in range(first, len(line), 2)]


class Ladder:
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

    def grown(self, drill_id: str) -> set[str]:
        self._load()
        return set(self._state.get(drill_id, {}).get("grown", []))

    def grow(self, drill_id: str, history: list[str]) -> bool:
        """Mark the node this move reached. True if it is new."""
        key = path_key(history)
        with self._lock:
            self._load()
            entry = self._state.setdefault(drill_id, {"grown": []})
            if key in entry["grown"]:
                return False
            entry["grown"].append(key)
            self._save()
            return True

    def summary(self, drill_id: str, lines: list[list[str]], is_white: bool) -> dict:
        """Foliage and reach, for the list and the caption."""
        with self._lock:
            grown = self.grown(drill_id)
        depths = []
        touched = 0
        for line in lines:
            nodes = user_nodes(line, is_white)
            hit = [i for i, n in enumerate(nodes, 1) if n in grown]
            if hit:
                touched += 1
                depths.append(max(hit))
        off_book = len(grown) - len(
            {n for line in lines for n in user_nodes(line, is_white)} & grown
        )
        return {
            "lines": len(lines),
            "started": touched,
            "grown": len(grown),
            "off_book": off_book,
            "deepest": max(depths, default=0),
            "average": round(sum(depths) / len(depths), 1) if depths else 0,
        }

    def next_line(
        self, drill_id: str, lines: list[list[str]], is_white: bool
    ) -> list[str] | None:
        """A line with something left on it.

        Weighted by how much of the line you have NOT grown, so you are sent
        where there is most to gain — but never locked to one: a line you keep
        declining simply waits.
        """
        with self._lock:
            grown = self.grown(drill_id)
        scored = []
        for line in lines:
            nodes = user_nodes(line, is_white)
            if not nodes:
                continue
            left = sum(1 for n in nodes if n not in grown)
            if left:
                scored.append((line, left))
        if not scored:
            return random.choice(lines) if lines else None
        return random.choices([s[0] for s in scored],
                              weights=[s[1] for s in scored])[0]

    def resteer(
        self, drill_id: str, lines: list[list[str]], is_white: bool,
        history: list[str],
    ) -> list[str] | None:
        """A line continuing from here that still has ungrown nodes on it.

        Knocked off the line it was showing, the opponent would otherwise drop
        onto whatever book reply came to hand — often ground you have already
        covered, where the run can run its course without growing anything.
        """
        with self._lock:
            grown = self.grown(drill_id)
        played = history[1::2] if is_white else history[0::2]
        options = []
        for line in lines:
            theirs = line[1::2] if is_white else line[0::2]
            if theirs[:len(played)] != played or len(line) <= len(history):
                continue
            left = sum(1 for n in user_nodes(line, is_white) if n not in grown)
            if left:
                options.append((line, left))
        if not options:
            return None
        return random.choices([o[0] for o in options],
                              weights=[o[1] for o in options])[0]

    def tree(
        self, drill_id: str, lines: list[list[str]], is_white: bool
    ) -> list[dict]:
        """The move tree to draw: where you have been, and one ply past it.

        The whole book is not a tree, it is a thicket — the ECO lines behind
        these drills carry some eighteen thousand moves, and drawing them all
        buries the few hundred you have actually played in wood that stands for
        nothing. So the tree is cut back to the positions you have stood in,
        the wood beneath them, and GROWTH_MARGIN plies of book past the point
        each line has reached. That margin is what keeps it a tree rather than
        a record: an opening you have never opened still shows a sprout, and
        every position you have reached shows the replies still waiting on it,
        so there is always somewhere visible to grow into.

        Nodes are moves. `on` marks a leaf, `book` marks an edge that is
        published theory — a node can be grown without being book, which is how
        a position you reached after the opponent left the book gets drawn.
        """
        with self._lock:
            grown = self.grown(drill_id)
        # every position you have stood in: your leaves, and the wood under them
        reached = {
            path_key(key.split()[: i + 1])
            for key in grown
            for i in range(len(key.split()))
        }
        roots: list[dict] = []
        index: dict[str, dict] = {}

        def touch(path: list[str], is_book: bool) -> dict:
            key = path_key(path)
            node = index.get(key)
            if node is None:
                node = {"san": path[-1], "d": len(path), "on": key in grown,
                        "book": is_book, "kids": []}
                index[key] = node
                parent = index.get(path_key(path[:-1])) if len(path) > 1 else None
                (parent["kids"] if parent else roots).append(node)
            node["book"] = node["book"] or is_book
            return node

        for line in lines:
            # how far along this line you have got. `reached` is prefix-closed,
            # so the covered plies are always the first ones
            depth = 0
            while depth < len(line) and path_key(line[: depth + 1]) in reached:
                depth += 1
            for i in range(min(len(line), depth + GROWTH_MARGIN)):
                touch(line[: i + 1], True)
        # branches you made yourself: drawn from the last book position they
        # share with the tree, so they hang off the wood they actually left
        for key in sorted(grown, key=len):
            path = key.split()
            for i in range(len(path)):
                touch(path[: i + 1], False)
        return roots
