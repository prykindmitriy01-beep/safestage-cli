import difflib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

if os.name == "nt":
    os.system("")

_RED = "\033[31m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

_RECORD = re.compile(r"(?P<stamp>\d{8}_\d{6})\.(?P<kind>bak|new)")
_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "env", "dist", "build"}
_PROTECTED = (".git",)


class SafeStageCore:
    def __init__(self) -> None:
        self.root_dir: Path = Path(os.getcwd()).resolve()
        self.sandbox_dir: Path = self.root_dir / ".safe_stage"
        self.backup_dir: Path = self.root_dir / ".safe_backups"
        self._init_environment()

    def _init_environment(self) -> None:
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, relative_path: str, must_exist: bool = False) -> Optional[Path]:
        normalized = relative_path.strip().replace("\\", "/")
        try:
            target = (self.root_dir / normalized).resolve()
        except (OSError, ValueError) as exc:
            self._error(f"invalid path '{relative_path}': {exc}")
            return None

        if not target.is_relative_to(self.root_dir):
            self._error(f"path escapes project root: '{relative_path}'")
            return None

        if target.is_relative_to(self.sandbox_dir) or target.is_relative_to(self.backup_dir):
            self._error(f"path points to service directory: '{relative_path}'")
            return None

        if any(target.is_relative_to(self.root_dir / name) for name in _PROTECTED):
            self._error(f"path points to protected directory: '{relative_path}'")
            return None

        if target.exists() and not target.is_file():
            self._error(f"not a file: '{relative_path}'")
            return None

        if must_exist and not target.exists():
            self._error(f"file not found: '{relative_path}'")
            return None

        return target

    def staged_path(self, target: Path) -> Path:
        return self.sandbox_dir / target.relative_to(self.root_dir)

    def isolate_file(self, relative_path: str) -> bool:
        target = self.resolve_path(relative_path, must_exist=True)
        if target is None:
            return False

        destination = self.staged_path(target)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination)
        except OSError as exc:
            self._error(f"copy failed for '{relative_path}': {exc}")
            return False

        return True

    def stage_file(self, relative_path: str, content: str) -> bool:
        target = self.resolve_path(relative_path)
        if target is None:
            return False

        newline = "\n"
        if target.is_file():
            try:
                if b"\r\n" in target.read_bytes():
                    newline = "\r\n"
            except OSError:
                pass

        destination = self.staged_path(target)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "w", encoding="utf-8", newline=newline) as handle:
                handle.write(content.replace("\r\n", "\n"))
        except OSError as exc:
            self._error(f"stage failed for '{relative_path}': {exc}")
            return False

        return True

    def review_changes(self, relative_path: str) -> bool:
        original = self.resolve_path(relative_path)
        if original is None:
            return False

        staged = self.staged_path(original)
        if not staged.is_file():
            self._error(f"no staged copy for '{relative_path}'")
            return False

        is_new = not original.exists()
        try:
            original_lines = [] if is_new else original.read_text(encoding="utf-8").splitlines(keepends=True)
            staged_lines = staged.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError) as exc:
            self._error(f"read failed for '{relative_path}': {exc}")
            return False

        if is_new:
            if not staged_lines:
                print("Файл пустой, создавать нечего")
                return False
            print(f"[safestage] New file: {relative_path}")
            fromfile, tofile = "/dev/null", f"{relative_path} (new file)"
        else:
            fromfile, tofile = f"{relative_path} (original)", f"{relative_path} (staged)"

        diff = list(difflib.unified_diff(original_lines, staged_lines, fromfile=fromfile, tofile=tofile))
        if not diff:
            print("Изменений нет")
            return False

        for line in diff:
            print(self._colorize(line.rstrip("\n")))

        answer = input("Вы одобряете деплой этих изменений в проект? [y/n]: ")
        if answer.strip().lower() == "y":
            return True

        print("Изменения отклонены")
        return False

    def deploy_file(self, relative_path: str) -> None:
        original = self.resolve_path(relative_path)
        if original is None:
            return

        staged = self.staged_path(original)
        if not staged.is_file():
            self._error(f"no staged copy for '{relative_path}'")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        is_new = not original.exists()
        record = self._record_path(original, stamp, "new" if is_new else "bak")

        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            if is_new:
                original.parent.mkdir(parents=True, exist_ok=True)
                record.touch()
            else:
                shutil.copy2(original, record)
            shutil.copy2(staged, original)
        except OSError as exc:
            self._error(f"deploy failed for '{relative_path}': {exc}")
            return

        if is_new:
            print(f"[safestage] created '{relative_path}'")
        else:
            print(f"[safestage] deployed '{relative_path}', backup: {record.name}")

    def panic_rollback(self, relative_path: str) -> bool:
        target = self.resolve_path(relative_path)
        if target is None:
            return False

        records = self._records(target)
        if not records:
            self._error(f"no backups for '{relative_path}'")
            return False

        _, kind, record = records[-1]
        try:
            if kind == "new":
                if target.exists():
                    target.unlink()
                outcome = f"'{relative_path}' created by SafeStage was removed"
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record, target)
                outcome = f"'{relative_path}' restored from {record.name}"
            self._clear_sandbox()
        except OSError as exc:
            self._error(f"rollback failed for '{relative_path}': {exc}")
            return False

        message = f"[safestage] ROLLBACK COMPLETE: {outcome}, sandbox cleared"
        print(f"{_BOLD}{message}{_RESET}" if sys.stdout.isatty() else message)
        return True

    def list_project_files(self, limit: int = 80, max_depth: int = 3) -> List[str]:
        found: List[str] = []
        for current, dirs, files in os.walk(self.root_dir):
            depth = len(Path(current).relative_to(self.root_dir).parts)
            dirs[:] = sorted(
                name for name in dirs
                if not name.startswith(".") and name not in _SKIP_DIRS and depth < max_depth
            )
            for name in sorted(files):
                if name.startswith("."):
                    continue
                found.append((Path(current) / name).relative_to(self.root_dir).as_posix())
                if len(found) >= limit:
                    return found
        return found

    def _record_path(self, target: Path, stamp: str, kind: str) -> Path:
        folder = self.backup_dir / target.relative_to(self.root_dir).parent
        return folder / f"{target.name}.{stamp}.{kind}"

    def _records(self, target: Path) -> List[Tuple[str, str, Path]]:
        folder = self.backup_dir / target.relative_to(self.root_dir).parent
        if not folder.is_dir():
            return []

        prefix = f"{target.name}."
        found: List[Tuple[str, str, Path]] = []
        for path in folder.iterdir():
            if not path.is_file() or not path.name.startswith(prefix):
                continue
            match = _RECORD.fullmatch(path.name[len(prefix):])
            if match:
                found.append((match.group("stamp"), match.group("kind"), path))
        return sorted(found)

    def _clear_sandbox(self) -> None:
        for item in self.sandbox_dir.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

    @staticmethod
    def _colorize(line: str) -> str:
        if not sys.stdout.isatty():
            return line
        if line.startswith(("+++", "---")):
            return line
        if line.startswith("+"):
            return f"{_GREEN}{line}{_RESET}"
        if line.startswith("-"):
            return f"{_RED}{line}{_RESET}"
        if line.startswith("@@"):
            return f"{_CYAN}{line}{_RESET}"
        return line

    @staticmethod
    def _error(message: str) -> None:
        print(f"[safestage] error: {message}", file=sys.stderr)
