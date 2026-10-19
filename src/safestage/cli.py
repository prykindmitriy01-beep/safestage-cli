from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .banner import print_banner
from .config import change_model, configure, edit_config, load_config
from .core import SafeStageCore
from .llm import MAX_FILES, Turn, parse_edit_reply, parse_request, send_ai_request
from .prompts import CHAT_PROMPT, EDIT_PROMPT, PLAN_PROMPT
from .providers import PROVIDERS, is_outdated
from .ui import error, print_status

MAX_HISTORY = 20

MODES: Dict[str, str] = {
    "edit": "the model edits and creates files, you approve each diff",
    "plan": "the model only plans, files are never changed",
    "chat": "free conversation, no file access",
}

HELP_TEXT = (
    "[safestage] Commands:\n"
    "  <free text>      handled by the current mode; in edit mode the model\n"
    "                   can change existing files and create new ones\n"
    "  edit | plan | chat\n"
    "                   without text: switch mode; with text: run once in that mode\n"
    "  apply            execute the last plan in edit mode\n"
    "  reset            clear chat history\n"
    "  config           change provider, model, API key or base URL\n"
    "  model            change model of the current provider\n"
    "  panic <file>     restore the file from the latest backup,\n"
    "                   or remove it if SafeStage created it\n"
    "  help             show this list\n"
    "  exit             quit"
)

USAGE = (
    "usage: safestage-cli [--version] [--help]\n\n"
    "Run it in the root of the project you want to edit. Files are isolated in\n"
    ".safe_stage, every change goes through a diff review, and every deploy\n"
    "leaves a backup in .safe_backups.\n\n" + HELP_TEXT
)


@dataclass
class Session:
    core: SafeStageCore
    mode: str = "edit"
    history: List[Turn] = field(default_factory=list)
    plan: Optional[Tuple[Optional[str], str]] = None


def _clean_name(value: Optional[str]) -> str:
    return (value or "").strip().strip("'\"")


def _load_target(core: SafeStageCore, filename: str) -> Optional[Tuple[bool, str]]:
    target = core.resolve_path(filename)
    if target is None:
        return None
    if not target.exists():
        return False, ""
    if not core.isolate_file(filename):
        return None
    try:
        return True, core.staged_path(target).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        error(f"read failed for '{filename}': {exc}")
        return None


def _task_prompt(core: SafeStageCore, filename: Optional[str], task: str, source: Optional[str]) -> str:
    parts = [f"Задача:\n{task}"]
    tree = core.list_project_files()
    if tree:
        parts.append("Файлы проекта:\n" + "\n".join(tree))
    if filename:
        if source is None:
            parts.append(f"Файл {filename} не существует, его нужно создать.")
        else:
            parts.append(f"Файл {filename}:\n{source}")
    return "\n\n".join(parts)


def apply_files(core: SafeStageCore, files: List[Tuple[str, str]], primary: Optional[Path]) -> None:
    for path, content in files[:MAX_FILES]:
        target = core.resolve_path(path)
        if target is None:
            continue
        if target.exists() and target != primary:
            print(f"[safestage] Note: '{path}' already exists and was not shown to the model, review carefully.")
        if not core.stage_file(path, content):
            continue
        if core.review_changes(path):
            core.deploy_file(path)

    if len(files) > MAX_FILES:
        print(f"[safestage] Skipped {len(files) - MAX_FILES} extra files, limit is {MAX_FILES} per request.")


def ask_ai_to_fix(core: SafeStageCore, filename: Optional[str], task: str) -> None:
    source: Optional[str] = None
    primary: Optional[Path] = None

    if filename:
        primary = core.resolve_path(filename)
        if primary is None:
            return
        if primary.exists():
            print_status("[safestage] Isolating target file into .safe_stage...")
            loaded = _load_target(core, filename)
            if loaded is None:
                return
            source = loaded[1]
        else:
            print_status(f"[safestage] '{filename}' does not exist, it will be created.")

    print_status("[safestage] Sending context to model...")
    streamed = send_ai_request(EDIT_PROMPT, [("user", _task_prompt(core, filename, task, source))])
    if streamed is None:
        return

    summary, files = parse_edit_reply(streamed.text, filename, fence_fallback=not streamed.thoughts)
    if summary:
        print("[safestage] Summary:")
        print(textwrap.indent(summary, "  "))

    if not files:
        error("no files in model response")
        return

    apply_files(core, files, primary)


def run_edit(core: SafeStageCore, text: str) -> None:
    print_status("[safestage] Analysing user request...")
    parsed = parse_request(text)
    if parsed is None:
        return

    filename = _clean_name(parsed.file)
    if not filename and not parsed.create:
        print("[safestage] Error: Target file not found in request.")
        return

    ask_ai_to_fix(core, filename or None, (parsed.task or "").strip() or text)


def run_plan(session: Session, text: str) -> None:
    print_status("[safestage] Analysing user request...")
    parsed = parse_request(text)
    if parsed is None:
        return

    filename = _clean_name(parsed.file) or None
    task = (parsed.task or "").strip() or text

    source: Optional[str] = None
    if filename:
        print_status("[safestage] Reading target file (read-only)...")
        loaded = _load_target(session.core, filename)
        if loaded is None:
            return
        source = loaded[1] if loaded[0] else None

    streamed = send_ai_request(PLAN_PROMPT, [("user", _task_prompt(session.core, filename, task, source))], live=True)
    if streamed is None:
        return

    plan = streamed.text.strip()
    if plan:
        session.plan = (filename, plan)
        print("[safestage] Plan stored. Type 'apply' to execute it in edit mode.")


def run_chat(session: Session, text: str) -> None:
    turns = session.history + [("user", text)]
    streamed = send_ai_request(CHAT_PROMPT, turns, live=True)
    if streamed is None:
        return

    reply = streamed.text.strip()
    if reply:
        session.history = (turns + [("assistant", reply)])[-MAX_HISTORY:]


def dispatch(session: Session, mode: str, text: str) -> None:
    if mode == "plan":
        run_plan(session, text)
    elif mode == "chat":
        run_chat(session, text)
    else:
        run_edit(session.core, text)


def run_apply(session: Session) -> None:
    if session.plan is None:
        print("[safestage] No plan stored. Use 'plan <request>' first.")
        return
    filename, plan = session.plan
    ask_ai_to_fix(session.core, filename, f"Реализуй этот план изменений:\n{plan}")


def show_banner(session: Session) -> None:
    config = load_config()
    if config is None:
        return
    warning = None
    if is_outdated(config.model):
        warning = f"model '{config.model}' is outdated or retired, type 'model' to pick another"
    print_banner(
        __version__,
        PROVIDERS[config.provider].title,
        config.model,
        str(session.core.root_dir),
        session.mode,
        warning,
    )


def main(argv: Optional[List[str]] = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-V", "--version"):
        print(f"safestage-cli {__version__}")
        return
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return
    if args:
        print(f"safestage-cli: unknown argument '{args[0]}'", file=sys.stderr)
        print("Try 'safestage-cli --help'.", file=sys.stderr)
        sys.exit(2)

    if load_config() is None:
        print("[safestage] First-time setup")
        if configure(None) is None:
            print("[safestage] Setup cancelled.")
            return

    session = Session(SafeStageCore())
    show_banner(session)

    while True:
        label = "safestage" if session.mode == "edit" else f"safestage[{session.mode}]"
        try:
            line = input(f"{label}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        bare = len(parts) == 1

        if command == "exit" and bare:
            break
        if command == "help" and bare:
            print(HELP_TEXT)
            continue
        if command in MODES:
            if bare:
                session.mode = command
                print(f"[safestage] Mode: {command} ({MODES[command]})")
            else:
                dispatch(session, command, parts[1])
            continue
        if command == "apply" and bare:
            run_apply(session)
            continue
        if command == "reset" and bare:
            session.history.clear()
            print("[safestage] Chat history cleared.")
            continue
        if command == "config" and bare:
            edit_config(load_config())
            continue
        if command == "model" and bare:
            current = load_config()
            if current is None:
                configure(None)
            else:
                change_model(current)
            continue
        if command == "panic":
            if len(parts) == 2:
                session.core.panic_rollback(parts[1].strip().strip("'\""))
            else:
                print("panic <file>")
            continue

        dispatch(session, session.mode, line)


if __name__ == "__main__":
    main()
