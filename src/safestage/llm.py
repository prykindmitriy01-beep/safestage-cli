from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Callable, Dict, Iterator, List, Optional, Tuple, cast

from pydantic import BaseModel, ValidationError

from .config import Config, load_config
from .prompts import CODE_MARKER, FILE_MARKER, MARKER_KEEP, PARSER_INSTRUCTION, SUMMARY_MARKER
from .ui import Spinner, StepTracker, error, hint_config

if TYPE_CHECKING:
    import anthropic
    import openai
    from google import genai
    from openai.types.chat import ChatCompletionMessageParam

Turn = Tuple[str, str]

MAX_FILES = 10

_FENCE = re.compile(r"^```[\w+-]*\n(.*?)\n?```$", re.DOTALL)
_FENCE_ANY = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_FILE_LINE = re.compile(r"^" + re.escape(FILE_MARKER) + r"[ \t]*(.+?)[ \t]*===[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Streamed:
    text: str
    thoughts: bool


class ParsedRequest(BaseModel):
    file: Optional[str] = None
    task: Optional[str] = None
    create: Optional[bool] = False


def _first_marker(text: str) -> int:
    found = [
        index
        for index in (text.find(SUMMARY_MARKER), text.find(CODE_MARKER), text.find(FILE_MARKER))
        if index >= 0
    ]
    return min(found) if found else -1


class ReplyCollector:
    def __init__(self, tracker: StepTracker, on_output: Callable[[], None], live: bool) -> None:
        self._tracker = tracker
        self._on_output = on_output
        self._live = live
        self._parts: List[str] = []
        self._tail = ""
        self._thoughts_seen = False
        self._steps_done = False
        self._output_started = False
        self._last_char = "\n"

    def thought(self, text: str) -> None:
        self._thoughts_seen = True
        self._tracker.feed(text)

    def text(self, text: str) -> None:
        self._parts.append(text)
        if self._live:
            self._emit(text)
        elif not (self._thoughts_seen or self._steps_done):
            self._route(text)

    def close(self) -> None:
        if self._live:
            if self._output_started and self._last_char != "\n":
                sys.stdout.write("\n")
                sys.stdout.flush()
            return
        if self._tail and not self._steps_done:
            self._tracker.feed(self._tail)
        self._tail = ""

    def result(self) -> Streamed:
        return Streamed("".join(self._parts), self._thoughts_seen)

    def _emit(self, text: str) -> None:
        if not self._output_started:
            self._on_output()
            self._tracker.finish()
            text = text.lstrip("\n")
            if not text:
                return
            self._output_started = True
        sys.stdout.write(text)
        sys.stdout.flush()
        self._last_char = text[-1]

    def _route(self, text: str) -> None:
        self._tail += text
        cut = _first_marker(self._tail)
        if cut >= 0:
            self._tracker.feed(self._tail[:cut])
            self._tail = ""
            self._steps_done = True
            return
        if len(self._tail) > MARKER_KEEP:
            self._tracker.feed(self._tail[:-MARKER_KEEP])
            self._tail = self._tail[-MARKER_KEEP:]


def _missing(package: str) -> RuntimeError:
    return RuntimeError(f"package '{package}' is not installed, run: pip install {package}")


@lru_cache(maxsize=None)
def _gemini_client(api_key: str) -> genai.Client:
    try:
        from google import genai
    except ImportError:
        raise _missing("google-genai") from None
    return genai.Client(api_key=api_key)


@lru_cache(maxsize=None)
def _anthropic_client(api_key: str) -> anthropic.Anthropic:
    try:
        import anthropic
    except ImportError:
        raise _missing("anthropic") from None
    return anthropic.Anthropic(api_key=api_key)


@lru_cache(maxsize=None)
def _openai_client(api_key: str, base_url: Optional[str]) -> openai.OpenAI:
    try:
        import openai
    except ImportError:
        raise _missing("openai") from None
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def _chat_messages(model: str, system: str, turns: List[Turn]) -> List[ChatCompletionMessageParam]:
    items: List[Dict[str, str]] = [{"role": role, "content": text} for role, text in turns]
    if model.startswith("o1"):
        items[0] = {"role": "user", "content": f"{system}\n\n{items[0]['content']}"}
    else:
        items.insert(0, {"role": "system", "content": system})
    return cast("List[ChatCompletionMessageParam]", items)


def _stream_gemini(config: Config, system: str, turns: List[Turn]) -> Iterator[Tuple[bool, str]]:
    from google.genai import types

    contents = [
        types.Content(role="model" if role == "assistant" else "user", parts=[types.Part(text=text)])
        for role, text in turns
    ]
    stream = _gemini_client(config.api_key).models.generate_content_stream(
        model=config.model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_budget=1024),
        ),
    )
    for chunk in stream:
        candidates = chunk.candidates or []
        content = candidates[0].content if candidates else None
        for part in (content.parts if content else None) or []:
            if part.text:
                yield bool(part.thought), part.text


def _stream_anthropic(config: Config, system: str, turns: List[Turn]) -> Iterator[Tuple[bool, str]]:
    stream = _anthropic_client(config.api_key).messages.create(
        model=config.model,
        max_tokens=8192,
        system=system,
        messages=[{"role": role, "content": text} for role, text in turns],
        stream=True,
    )
    for event in stream:
        if event.type == "content_block_delta" and event.delta.type == "text_delta":
            yield False, event.delta.text


def _stream_openai_compatible(config: Config, system: str, turns: List[Turn]) -> Iterator[Tuple[bool, str]]:
    stream = _openai_client(config.api_key, config.base_url).chat.completions.create(
        model=config.model,
        messages=_chat_messages(config.model, system, turns),
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if isinstance(reasoning, str) and reasoning:
            yield True, reasoning
        if delta.content:
            yield False, delta.content


def _stream(config: Config, system: str, turns: List[Turn]) -> Iterator[Tuple[bool, str]]:
    if config.provider == "gemini":
        return _stream_gemini(config, system, turns)
    if config.provider == "anthropic":
        return _stream_anthropic(config, system, turns)
    return _stream_openai_compatible(config, system, turns)


def _complete_json(config: Config, text: str) -> str:
    if config.provider == "gemini":
        from google.genai import types

        response = _gemini_client(config.api_key).models.generate_content(
            model=config.model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=PARSER_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=ParsedRequest,
                temperature=0,
            ),
        )
        return response.text or ""

    if config.provider == "anthropic":
        message = _anthropic_client(config.api_key).messages.create(
            model=config.model,
            max_tokens=256,
            system=PARSER_INSTRUCTION,
            messages=[{"role": "user", "content": text}],
            temperature=0,
        )
        return "".join(block.text for block in message.content if block.type == "text")

    completion = _openai_client(config.api_key, config.base_url).chat.completions.create(
        model=config.model,
        messages=_chat_messages(config.model, PARSER_INSTRUCTION, [("user", text)]),
    )
    return completion.choices[0].message.content or ""


def _last_fence(text: str) -> str:
    blocks = _FENCE_ANY.findall(text)
    return blocks[-1] if blocks else ""


def clean_content(text: str) -> str:
    text = text.strip()
    match = _FENCE.match(text)
    if match:
        text = match.group(1)
    text = text.strip("\n")
    return f"{text}\n" if text.strip() else ""


def _normalize_path(raw: str) -> str:
    return raw.strip().strip("`'\" ").replace("\\", "/")


def parse_edit_reply(
    full: str, default_name: Optional[str], fence_fallback: bool
) -> Tuple[str, List[Tuple[str, str]]]:
    matches = list(_FILE_LINE.finditer(full))
    code_at = full.rfind(CODE_MARKER)

    body_start = len(full)
    if matches:
        body_start = matches[0].start()
    elif code_at >= 0:
        body_start = code_at

    summary = ""
    summary_at = full.find(SUMMARY_MARKER)
    if summary_at >= 0:
        begin = summary_at + len(SUMMARY_MARKER)
        summary = full[begin:body_start].strip() if body_start > begin else ""

    files: List[Tuple[str, str]] = []
    if matches:
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(full)
            path = _normalize_path(match.group(1))
            content = clean_content(full[match.end():end])
            if path and content and all(path != existing for existing, _ in files):
                files.append((path, content))
        return summary, files

    if default_name is None:
        return summary, files

    if code_at >= 0:
        content = clean_content(full[code_at + len(CODE_MARKER):])
    elif SUMMARY_MARKER in full:
        content = ""
    else:
        content = clean_content(_last_fence(full) if fence_fallback else full)

    if content:
        files.append((default_name, content))
    return summary, files


def parse_request(text: str) -> Optional[ParsedRequest]:
    config = load_config()
    if config is None:
        error("config is missing or invalid, run 'config'")
        return None

    try:
        with Spinner("Parsing request"):
            raw = _complete_json(config, text)
    except KeyboardInterrupt:
        print("\n[safestage] Cancelled.")
        return None
    except Exception as exc:
        error(f"parser request failed: {exc}")
        hint_config()
        return None

    match = _JSON_OBJECT.search(raw)
    if match is None:
        error("parser returned no JSON")
        return None
    try:
        return ParsedRequest.model_validate_json(match.group(0))
    except ValidationError as exc:
        error(f"invalid parser response: {exc}")
        return None


def send_ai_request(system: str, turns: List[Turn], live: bool = False) -> Optional[Streamed]:
    config = load_config()
    if config is None:
        error("config is missing or invalid, run 'config'")
        return None

    spinner = Spinner("Waiting for model")
    tracker = StepTracker(on_first_step=spinner.stop)
    collector = ReplyCollector(tracker, spinner.stop, live)
    spinner.start()

    try:
        for is_thought, text in _stream(config, system, turns):
            if is_thought:
                collector.thought(text)
            else:
                collector.text(text)
        collector.close()
        spinner.stop()
        tracker.finish()
    except KeyboardInterrupt:
        spinner.stop()
        tracker.abort()
        print("\n[safestage] Cancelled.")
        return None
    except Exception as exc:
        spinner.stop()
        tracker.abort()
        error(f"{config.provider} request failed: {exc}")
        hint_config()
        return None
    finally:
        spinner.stop()

    return collector.result()
