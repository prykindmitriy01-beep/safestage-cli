from __future__ import annotations

import itertools
import shutil
import sys
import threading
import time
from enum import Enum, auto
from typing import Callable, Optional

GRAY = "\033[90m"
RESET = "\033[0m"
CLEAR_LINE = "\r\033[K"
BULLET = "•"
TICK = 0.1
FRAMES = ("[ / ]", "[ - ]", "[ \\ ]", "[ | ]")


def error(message: str) -> None:
    print(f"[safestage] error: {message}", file=sys.stderr)


def hint_config() -> None:
    print("[safestage] Provider, model or API key may be wrong. Type 'config' to change them.")


def print_status(message: str, delay: float = 0.02) -> None:
    if delay <= 0 or not sys.stdout.isatty():
        print(message)
        return
    for char in message:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


class Spinner:
    def __init__(self, label: str) -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self) -> None:
        if not sys.stdout.isatty() or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        sys.stdout.write(CLEAR_LINE)
        sys.stdout.flush()

    def _spin(self) -> None:
        for frame in itertools.cycle(FRAMES):
            if self._stop.is_set():
                return
            sys.stdout.write(f"\r{frame} {self._label}")
            sys.stdout.flush()
            self._stop.wait(TICK)


class _Mode(Enum):
    BODY = auto()
    PREFIX = auto()
    HEADER = auto()


class StepTracker:
    def __init__(self, on_first_step: Callable[[], None]) -> None:
        self._on_first_step = on_first_step
        self._enabled = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mode = _Mode.BODY
        self._line_start = True
        self._title_buf = ""
        self._step: Optional[str] = None
        self._started = 0.0
        self._steps_shown = 0
        self._finished = False

    def feed(self, text: str) -> None:
        if not self._enabled or self._finished:
            return
        with self._lock:
            for char in text:
                self._consume(char)

    def finish(self) -> None:
        if not self._enabled or self._finished:
            return
        self._finished = True
        self._halt_timer()
        with self._lock:
            self._close_step("SUCCESS")
            if self._steps_shown:
                width = min(shutil.get_terminal_size().columns - 1, 60)
                sys.stdout.write(f"{GRAY}{'-' * width}{RESET}\n")
                sys.stdout.flush()

    def abort(self) -> None:
        if not self._enabled:
            return
        self._finished = True
        self._halt_timer()
        with self._lock:
            self._close_step("ABORTED")

    def _consume(self, char: str) -> None:
        if self._mode is _Mode.BODY:
            if char == "\n":
                self._line_start = True
            elif self._line_start and char == "*":
                self._mode = _Mode.PREFIX
                self._line_start = False
            elif not char.isspace():
                self._line_start = False
        elif self._mode is _Mode.PREFIX:
            if char == "*":
                self._mode = _Mode.HEADER
                self._title_buf = ""
            else:
                self._mode = _Mode.BODY
                self._line_start = char == "\n"
        else:
            if char == "\n":
                self._mode = _Mode.BODY
                self._line_start = True
                self._title_buf = ""
                return
            self._title_buf += char
            if self._title_buf.endswith("**"):
                title = self._title_buf[:-2].strip()
                self._title_buf = ""
                self._mode = _Mode.BODY
                self._line_start = False
                if title:
                    self._open_step(title)

    def _open_step(self, title: str) -> None:
        if self._steps_shown == 0:
            self._on_first_step()
        self._close_step("SUCCESS")
        self._step = title
        self._started = time.time()
        self._steps_shown += 1
        self._render("PROCESS", 0.0, newline=False)
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def _close_step(self, status: str) -> None:
        if self._step is None:
            return
        self._render(status, time.time() - self._started, newline=True)
        self._step = None

    def _render(self, status: str, elapsed: float, newline: bool) -> None:
        if self._step is None:
            return
        suffix = f"... [{elapsed:.1f}s]"
        prefix = f"{BULLET} [{status}] "
        room = shutil.get_terminal_size().columns - len(prefix) - len(suffix) - 2
        title = self._step if len(self._step) <= room else self._step[: max(room, 1)]
        end = "\n" if newline else ""
        sys.stdout.write(f"{CLEAR_LINE}{prefix}{title}{suffix}{end}")
        sys.stdout.flush()

    def _tick(self) -> None:
        while not self._stop.wait(TICK):
            with self._lock:
                if self._step is not None:
                    self._render("PROCESS", time.time() - self._started, newline=False)

    def _halt_timer(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join()
        self._thread = None
