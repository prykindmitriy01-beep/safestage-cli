from __future__ import annotations

import shutil
import sys
import time
from typing import Dict, List, Optional, Tuple

Segment = Tuple[str, str]

GLYPHS: Dict[str, Tuple[str, ...]] = {
    "S": (".####", "#....", ".###.", "....#", "####."),
    "A": (".###.", "#...#", "#####", "#...#", "#...#"),
    "F": ("#####", "#....", "####.", "#....", "#...."),
    "E": ("#####", "#....", "###..", "#....", "#####"),
    "T": ("#####", "..#..", "..#..", "..#..", "..#.."),
    "G": (".####", "#....", "#..##", "#...#", ".###."),
    "C": (".####", "#....", "#....", "#....", ".####"),
    "L": ("#....", "#....", "#....", "#....", "#####"),
    "I": ("#####", "..#..", "..#..", "..#..", "#####"),
    "-": (".....", ".....", ".###.", ".....", "....."),
}

MAIN_WORD = "SAFESTAGE"
TAIL_WORD = "-CLI"
TAGLINE = "AI edits. You approve."
FULL_WIDTH = 80

_ANSI = {
    "green": "\033[1;32m",
    "white": "\033[97m",
    "gray": "\033[90m",
    "yellow": "\033[33m",
}
_RESET = "\033[0m"


def _block_char() -> str:
    try:
        "█".encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return "#"
    return "█"


def _word_row(word: str, row: int, block: str) -> str:
    cells = " ".join(GLYPHS[char][row] for char in word)
    return cells.replace("#", block).replace(".", " ")


def _field(label: str, value: str, style: str = "white") -> List[Segment]:
    return [("  " + label.ljust(10), "gray"), (value, style)]


def _shorten(path: str, room: int) -> str:
    if len(path) <= room:
        return path
    return "..." + path[-(room - 3):]


def build_banner(
    version: str,
    provider: str,
    model: str,
    project: str,
    mode: str,
    warning: Optional[str],
    width: int,
    block: str = "█",
) -> List[List[Segment]]:
    lines: List[List[Segment]] = [[]]

    if width >= FULL_WIDTH:
        for row in range(5):
            lines.append(
                [
                    ("  ", ""),
                    (_word_row(MAIN_WORD, row, block), "green"),
                    (" ", ""),
                    (_word_row(TAIL_WORD, row, block), "white"),
                ]
            )
    else:
        lines.append([("  SafeStage-CLI", "green")])

    lines.append([])
    lines.append([("  " + TAGLINE, "white"), (f"   v{version}", "gray")])
    lines.append([])
    lines.append(_field("provider", f"{provider} / {model}"))
    lines.append(_field("project", _shorten(project, max(width - 14, 20))))
    lines.append(
        [("  " + "mode".ljust(10), "gray"), (mode, "white"), ("   type 'help' for commands", "gray")]
    )
    if warning:
        lines.append(_field("warning", warning, "yellow"))
    lines.append([])
    return lines


def print_banner(
    version: str,
    provider: str,
    model: str,
    project: str,
    mode: str,
    warning: Optional[str] = None,
) -> None:
    width = shutil.get_terminal_size(fallback=(100, 24)).columns
    lines = build_banner(version, provider, model, project, mode, warning, width, _block_char())
    tty = sys.stdout.isatty()

    for line in lines:
        text = "".join(
            f"{_ANSI[style]}{chunk}{_RESET}" if tty and style else chunk
            for chunk, style in line
        )
        print(text)
        if tty and line:
            sys.stdout.flush()
            time.sleep(0.03)
