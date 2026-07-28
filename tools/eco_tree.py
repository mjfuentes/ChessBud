"""The ECO book as a tree of named theory.

data/eco_*.tsv is the lichess-org/chess-openings list: every named opening and
variation with the moves that reach it — 3,807 of them, up to 36 plies deep.
Merged into a trie it is a far better basis for preparation than any one
player's game archive, which only ever samples what a handful of opponents
happened to choose.

Nodes carry the deepest name that applies to them, so a branch can be labelled
"Winawer Variation" rather than "you faced this 3 times".
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import chess
import chess.pgn


class EcoNode:
    __slots__ = ("san", "name", "eco", "kids", "weight")

    def __init__(self, san: str):
        self.san = san
        self.name: str | None = None
        self.eco: str | None = None
        self.kids: dict[str, "EcoNode"] = {}
        # how many named variations run through here. The main road of an
        # opening is the one the most theory is written about, which is a
        # better guide to what to prepare than an engine's 0.3s preference.
        self.weight = 0

    def child(self, san: str) -> "EcoNode":
        node = self.kids.get(san)
        if node is None:
            node = EcoNode(san)
            self.kids[san] = node
        return node


def load_eco_lines(data_dir: Path) -> list[tuple[str, str, list[str]]]:
    """(eco, name, sans) for every entry in the book."""
    out = []
    for tsv in sorted(data_dir.glob("eco_*.tsv")):
        with open(tsv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                game = chess.pgn.read_game(io.StringIO(row["pgn"]))
                if game is None:
                    continue
                board = chess.Board()
                sans = []
                for move in game.mainline_moves():
                    sans.append(board.san(move))
                    board.push(move)
                if sans:
                    out.append((row["eco"], row["name"], sans))
    return out


def build_tree(entries: list[tuple[str, str, list[str]]]) -> EcoNode:
    root = EcoNode("")
    for eco, name, sans in entries:
        node = root
        node.weight += 1
        for san in sans:
            node = node.child(san)
            node.weight += 1
        # the deepest entry reaching a node wins its label; shorter prefixes
        # keep the broader name they were given
        node.name, node.eco = name, eco
    return root


def family_of(name: str) -> str:
    return name.split(":")[0].strip()


def name_segments(name: str) -> set[str]:
    """Every opening this entry belongs to.

    An opening is not always the first part of its name: the London System is
    filed under "Indian Defense: London System" and "Queen's Pawn Game: London
    System" as often as under "London System", so matching only the head of the
    name found four of its lines and missed the rest. Splitting on both colons
    and commas catches it wherever it sits, while keeping whole phrases — so
    "London Defensive System" is not mistaken for "London System".
    """
    out = set()
    for part in name.split(":"):
        part = part.strip()
        if part:
            out.add(part)
        for piece in part.split(","):
            piece = piece.strip()
            if piece:
                out.add(piece)
    return out


def line_epds(sans: list[str]) -> list[str]:
    board = chess.Board()
    out = []
    for san in sans:
        try:
            board.push_san(san)
        except ValueError:
            break
        out.append(board.epd())
    return out


def entries_for(entries, family: str):
    """Every line of an opening, found by position rather than by name.

    An opening is a position, not a string. Names only locate it: the entries
    carrying the family somewhere in their name give its root positions, and
    then any line passing through one of those positions belongs — however it
    is labelled and whatever move order it arrives by.

    Matching on names alone missed most of the book, because a family's own
    name is rarely the head of every line that reaches it. The King's Gambit
    had one named line and 190 that pass through its position; the Three
    Knights had six and 48.
    """
    roots = set()
    for _, name, sans in entries:
        if family in name_segments(name):
            epds = line_epds(sans)
            if epds:
                roots.add(epds[-1])
    if not roots:
        return []
    return [e for e in entries if roots & set(line_epds(e[2]))]


def walk(node: EcoNode, path: list[str]):
    """Every (path, node) in the tree, depth first."""
    yield path, node
    for san, kid in node.kids.items():
        yield from walk(kid, path + [san])
