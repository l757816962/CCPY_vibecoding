from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .messages import AssistantTurn
from .model import OpenAICompatibleClient


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    name: str
    base_url: str
    model: str
    env_key: str


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset("openai", "https://api.openai.com/v1", "gpt-4o", "OPENAI_API_KEY"),
    "openai-compatible": ProviderPreset(
        "openai-compatible",
        "https://api.openai.com/v1",
        "kimi-k2.6",
        "CCPY_API_KEY",
    ),
    "kimi": ProviderPreset("kimi", "https://api.moonshot.cn/v1", "kimi-k2.6", "KIMI_API_KEY"),
    "moonshot": ProviderPreset("moonshot", "https://api.moonshot.cn/v1", "kimi-k2.6", "MOONSHOT_API_KEY"),
    "minimax": ProviderPreset("minimax", "https://api.minimaxi.com/v1", "MiniMax-M2.7", "MINIMAX_API_KEY"),
    "deepseek": ProviderPreset("deepseek", "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "qwen": ProviderPreset(
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "QWEN_API_KEY",
    ),
}


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
    ) -> AssistantTurn:
        ...


def apply_provider_preset(config: Config) -> Config:
    """Fill unset provider defaults while preserving explicit CCPY_* settings."""
    preset = PROVIDER_PRESETS.get(config.provider.lower())
    if preset is None:
        return config
    if not config.base_url or config.base_url == "https://api.openai.com/v1":
        config.base_url = preset.base_url.rstrip("/")
    if not config.model or config.model == "kimi-k2.6":
        config.model = preset.model
    return config


def create_provider(config: Config) -> LLMProvider:
    """Create the configured provider.

    CCPY keeps the hardened OpenAI-compatible client as the default path because
    it contains the New API/K2.6 compatibility fixes discovered during testing.
    """
    apply_provider_preset(config)
    return OpenAICompatibleClient(config)
