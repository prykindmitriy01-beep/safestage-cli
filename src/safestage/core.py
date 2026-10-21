import difflib
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

if os.name == "nt":
    os.system("")

_RED = "\033[31m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

# Backup records live next to each other as <name>.<stamp>.<kind>:
#   bak  - content before a deploy replaced the file
#   new  - the file did not exist before a deploy created it
#   undo - content saved by a rollback, so that "redo" can bring it back
# Stamps written by 0.2.0 stop at seconds, newer ones add microseconds so two
# deploys within one second no longer overwrite each other's backup.
_RECORD = re.compile(r"(?P<stamp>\d{8}_\d{6}(?:_\d{6})?)\.(?P<kind>bak|new|undo)")
_ABSENT = "absent"
_KIND_NOTES = {
    "bak": ("before deploy", "previous content was backed up"),
    "new": ("before creation", "the file did not exist"),
    "undo": ("before rollback", "saved by panic, 'redo' brings it back"),
}
_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "env", "dist", "build"}
_PROTECTED = (".git",)


class Record(NamedTuple):
    stamp: str
    kind: str
    path: Path

    @property
    def order(self) -> Tuple[str, int]:
        return self.stamp[:15], int(self.stamp[16:] or 0)


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

        is_new = not original.exists()
        record = self._record_path(original, self._new_stamp(), "new" if is_new else "bak")

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

        self._write_digest(record, self._digest(original))
        if is_new:
            print(f"[safestage] created '{relative_path}'")
        else:
            print(f"[safestage] deployed '{relative_path}', backup: {record.name}")

    def panic_rollback(self, relative_path: str, point: Optional[int] = None) -> bool:
        """Roll the file back to its latest backup, or to point ``point`` of its history.

        The current content is saved first, so a rollback can be undone with ``redo``.
        """
        target = self.resolve_path(relative_path)
        if target is None:
            return False

        records = self._records(target)
        chosen = self._pick_record(relative_path, records, point)
        if chosen is None:
            return False

        current = self._digest(target)
        if current == self._record_digest(chosen):
            print(f"[safestage] '{relative_path}' already matches that state, nothing to roll back")
            return True

        note = "Its current content is saved first, 'redo' brings it back."
        if not self._confirm_overwrite(relative_path, current, self._expected_digest(records), note):
            return False

        try:
            saved = self._save_undo(target)
        except OSError as exc:
            self._error(f"could not save the current state of '{relative_path}', rollback cancelled: {exc}")
            return False

        try:
            if chosen.kind == "new":
                target.unlink(missing_ok=True)
                outcome = f"'{relative_path}' created by SafeStage was removed"
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(chosen.path, target)
                outcome = f"'{relative_path}' restored from {chosen.path.name}"
        except OSError as exc:
            # the saved copy stays where it is: after a half-finished copy it may be the only intact one
            where = f", the previous state is in {saved.path}" if saved else ""
            self._error(f"rollback failed for '{relative_path}': {exc}{where}")
            return False

        if saved is not None:
            self._write_digest(saved.path, self._digest(target))
            outcome += f", previous state saved, 'redo {relative_path}' brings it back"
        try:
            self._clear_sandbox()
        except OSError as exc:
            self._error(f"sandbox was not cleared: {exc}")

        message = f"[safestage] ROLLBACK COMPLETE: {outcome}"
        print(f"{_BOLD}{message}{_RESET}" if sys.stdout.isatty() else message)
        return True

    def redo(self, relative_path: str) -> bool:
        """Bring back the state that the latest rollback of this file replaced."""
        target = self.resolve_path(relative_path)
        if target is None:
            return False

        records = self._records(target)
        undos = [item for item in records if item.kind == "undo"]
        if not undos:
            self._error(f"nothing to redo for '{relative_path}'")
            return False

        saved = undos[-1]
        if any(item.kind != "undo" and item.order > saved.order for item in records):
            self._error(
                f"'{relative_path}' was deployed again after that rollback, redo would drop the newer deploy. "
                f"See 'history {relative_path}' and 'panic {relative_path} N'"
            )
            return False

        current = self._digest(target)
        if not self._confirm_overwrite(relative_path, current, self._read_digest(saved), "Those edits will be lost."):
            return False

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved.path, target)
            saved.path.unlink()
            self._digest_path(saved.path).unlink(missing_ok=True)
        except OSError as exc:
            self._error(f"redo failed for '{relative_path}': {exc}")
            return False

        message = f"[safestage] REDO COMPLETE: '{relative_path}' is back to its state before the rollback"
        print(f"{_BOLD}{message}{_RESET}" if sys.stdout.isatty() else message)
        return True

    def history(self, relative_path: str) -> bool:
        target = self.resolve_path(relative_path)
        if target is None:
            return False

        records = self._records(target)
        if not records:
            print(f"[safestage] no history for '{relative_path}'")
            return True

        print(f"[safestage] History of '{relative_path}', newest first:")
        for number, record in enumerate(reversed(records), 1):
            moment = datetime.strptime(record.stamp[:15], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            label, note = _KIND_NOTES[record.kind]
            print(f"  {number:>2}  {moment}  {label:<16} {note}")
        print(f"[safestage] 'panic {relative_path} N' restores the state of point N.")
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

    def _records(self, target: Path) -> List[Record]:
        folder = self.backup_dir / target.relative_to(self.root_dir).parent
        if not folder.is_dir():
            return []

        prefix = f"{target.name}."
        found: List[Record] = []
        for path in folder.iterdir():
            if not path.is_file() or not path.name.startswith(prefix):
                continue
            match = _RECORD.fullmatch(path.name[len(prefix):])
            if match:
                found.append(Record(match.group("stamp"), match.group("kind"), path))
        return sorted(found, key=lambda item: item.order)

    def _pick_record(self, relative_path: str, records: List[Record], point: Optional[int]) -> Optional[Record]:
        if not records:
            self._error(f"no backups for '{relative_path}'")
            return None
        if point is None:
            deploys = [item for item in records if item.kind != "undo"]
            if deploys:
                return deploys[-1]
            self._error(f"no deploy backups for '{relative_path}', see 'history {relative_path}'")
            return None
        if not 1 <= point <= len(records):
            self._error(f"no point {point} for '{relative_path}', see 'history {relative_path}'")
            return None
        return records[::-1][point - 1]

    def _save_undo(self, target: Path) -> Optional[Record]:
        if not target.exists():
            return None
        stamp = self._new_stamp()
        path = self._record_path(target, stamp, "undo")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, path)
        return Record(stamp, "undo", path)

    # A digest sidecar (<record>.hash) holds the hash of what SafeStage left in
    # the file when it wrote the record. If the file differs from it later, it
    # was edited outside SafeStage and a rollback would throw those edits away.
    # Records from 0.2.0 have no sidecar, for them the check is skipped.

    @staticmethod
    def _new_stamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    @staticmethod
    def _digest(path: Path) -> str:
        if not path.is_file():
            return _ABSENT
        sha = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def _record_digest(self, record: Record) -> str:
        return _ABSENT if record.kind == "new" else self._digest(record.path)

    @staticmethod
    def _digest_path(record_path: Path) -> Path:
        return record_path.with_name(record_path.name + ".hash")

    def _write_digest(self, record_path: Path, digest: str) -> None:
        try:
            self._digest_path(record_path).write_text(digest, encoding="ascii")
        except OSError:
            pass

    def _read_digest(self, record: Record) -> Optional[str]:
        try:
            return self._digest_path(record.path).read_text(encoding="ascii").strip() or None
        except OSError:
            return None

    def _expected_digest(self, records: List[Record]) -> Optional[str]:
        for record in reversed(records):
            digest = self._read_digest(record)
            if digest is not None:
                return digest
        return None

    @staticmethod
    def _confirm_overwrite(relative_path: str, current: str, expected: Optional[str], note: str) -> bool:
        if current == _ABSENT or expected is None or current == expected:
            return True
        print(f"[safestage] Warning: '{relative_path}' was changed after SafeStage last wrote it. {note}")
        return input("Continue? [y/n]: ").strip().lower() == "y"

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
