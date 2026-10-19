from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Provider:
    title: str
    base_url: Optional[str]
    models: Tuple[Tuple[str, str], ...]
    custom_url: bool = False


PROVIDERS: Dict[str, Provider] = {
    "gemini": Provider(
        "Google Gemini",
        None,
        (
            ("google/gemini-4-argon", "gemini-4-argon"),
            ("google/gemini-3.8-flash", "gemini-3.8-flash"),
            ("google/gemini-3.6-flash", "gemini-3.6-flash"),
            ("google/gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
        ),
    ),
    "openai": Provider(
        "OpenAI",
        None,
        (
            ("openai/gpt-6-astra", "gpt-6-astra"),
            ("openai/gpt-6.1-sol", "gpt-6.1-sol"),
            ("openai/gpt-6-luna", "gpt-6-luna"),
        ),
    ),
    "anthropic": Provider(
        "Anthropic Claude",
        None,
        (
            ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),
            ("anthropic/claude-opus-5-5", "claude-opus-5-5"),
            ("anthropic/claude-haiku-5-5", "claude-haiku-5-5"),
            ("anthropic/claude-fable-5-1", "claude-fable-5-1"),
        ),
    ),
    "openrouter": Provider(
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        (
            ("openrouter/claude-sonnet-5.5", "anthropic/claude-sonnet-5.5"),
            ("openrouter/gpt-6.1-sol", "openai/gpt-6.1-sol"),
            ("openrouter/gemini-4-argon", "google/gemini-4-argon"),
            ("openrouter/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-1017"),
            ("openrouter/gpt-6-luna", "openai/gpt-6-luna"),
        ),
    ),
    "groq": Provider(
        "Groq",
        "https://api.groq.com/openai/v1",
        (
            ("groq/gpt-oss-120b", "openai/gpt-oss-120b"),
            ("groq/gpt-oss-20b", "openai/gpt-oss-20b"),
            ("groq/qwen3.8-27b", "qwen/qwen3.8-27b"),
        ),
    ),
    "deepseek": Provider(
        "DeepSeek",
        "https://api.deepseek.com",
        (
            ("deepseek/deepseek-flash", "deepseek-flash"),
            ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ),
    ),
    "kimi": Provider(
        "Kimi / Moonshot",
        "https://api.moonshot.ai/v1",
        (
            ("kimi/kimi-k3", "kimi-k3"),
            ("kimi/kimi-k2.7-code", "kimi-k2.7-code"),
            ("kimi/kimi-k2.6", "kimi-k2.6"),
        ),
    ),
    "compatible": Provider("OpenAI Compatible", None, (), custom_url=True),
}

_OUTDATED_EXACT = frozenset({"gemini-3.5-flash", "gemini-3.7-flash"})
_OUTDATED_PREFIXES = (
    "gemini-2.",
    "claude-3",
    "o1",
    "o3-mini",
    "gpt-3.5",
    "gpt-4",
    "deepseek-chat",
    "deepseek-reasoner",
    "moonshot-v1",
    "kimi-k2.5",
    "llama3-",
    "mixtral-",
)


def is_outdated(model: str) -> bool:
    name = model.rsplit("/", 1)[-1].lower()
    return name in _OUTDATED_EXACT or name.startswith(_OUTDATED_PREFIXES)
