from __future__ import annotations

import asyncio
import json
import re
import time
from json import JSONDecodeError
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config
from .messages import AssistantTurn, normalize_tool_call_ids, parse_tool_call


class ModelError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Small async adapter for OpenAI-compatible chat completions."""

    def __init__(self, config: Config):
        self.config = config
        self._concurrency = asyncio.Semaphore(max(1, self.config.model_max_concurrency))
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._adaptive_min_interval_s = 0.0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> AssistantTurn:
        if not self.config.api_key:
            raise ModelError("Missing CCPY_API_KEY or OPENAI_API_KEY")

        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": normalize_tool_call_ids(messages),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = self._chat_completions_url(self.config.base_url)
        request_url = url
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            response = await self._post_with_retries(client, request_url, headers, payload)
            if self._looks_like_web_console(response):
                fallback_url = self._fallback_v1_url(self.config.base_url)
                if fallback_url and fallback_url != url:
                    request_url = fallback_url
                    response = await self._post_with_retries(client, request_url, headers, payload)
            if self._needs_reasoning_content_compat(response):
                compat_payload = {**payload, "messages": self._add_empty_reasoning_content(payload["messages"])}
                response = await self._post_with_retries(client, request_url, headers, compat_payload)
        if response.status_code >= 400:
            raise ModelError(f"Model request failed {response.status_code}: {response.text[:1000]}")

        body = self._parse_response_body(response)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [parse_tool_call(item) for item in message.get("tool_calls") or []]
        return AssistantTurn(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            raw=body,
        )

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        response: httpx.Response | None = None
        last_error: httpx.HTTPError | None = None
        for attempt in range(self.config.model_max_retries + 1):
            try:
                response = await self._post_once(client, url, headers, payload)
                last_error = None
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.config.model_max_retries or not self._is_retryable_exception(exc):
                    raise ModelError(
                        f"Model request failed after transport error {type(exc).__name__}: {exc}"
                    ) from exc
                await asyncio.sleep(self._exception_retry_delay(attempt))
                continue
            if not self._is_retryable_response(response):
                return response
            self._learn_rate_limit(response)
            if attempt >= self.config.model_max_retries:
                return response
            await asyncio.sleep(self._retry_delay(response, attempt))
        if last_error is not None:
            raise ModelError(f"Model request failed after retries: {last_error}") from last_error
        return response

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        async with self._concurrency:
            await self._throttle()
            return await client.post(url, headers=headers, json=payload)

    async def _throttle(self) -> None:
        min_interval = max(self.config.model_min_interval_s, self._adaptive_min_interval_s)
        if min_interval <= 0:
            return
        async with self._request_lock:
            now = time.monotonic()
            wait_s = self._last_request_at + min_interval - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
                now = time.monotonic()
            self._last_request_at = now

    def _parse_response_body(self, response: httpx.Response) -> dict[str, Any]:
        text = response.text
        try:
            body = response.json()
        except JSONDecodeError:
            body = self._parse_sse_body(text)
            if body is None:
                content_type = response.headers.get("content-type", "<missing>")
                preview = text[:1000] if text else "<empty body>"
                hint = self._non_json_hint(content_type, text)
                raise ModelError(
                    "Model response was not valid JSON. "
                    f"status={response.status_code}, content-type={content_type}. "
                    f"{hint} body-preview={preview!r}"
                ) from None

        if not isinstance(body, dict):
            raise ModelError(f"Model response JSON must be an object, got {type(body).__name__}")
        if "choices" not in body:
            raise ModelError(f"Model response missing 'choices': {str(body)[:1000]}")
        return body

    def _chat_completions_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def _fallback_v1_url(self, base_url: str) -> str | None:
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            return None
        return f"{base_url.rstrip('/')}/v1/chat/completions"

    def _looks_like_web_console(self, response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        text = response.text[:500].lower()
        return response.status_code == 200 and "text/html" in content_type and ("<html" in text or "<!doctype" in text)

    def _non_json_hint(self, content_type: str, text: str) -> str:
        lowered = text[:1000].lower()
        if "text/html" in content_type.lower() and ("new api" in lowered or "<html" in lowered):
            return (
                "The endpoint returned an HTML web console page. "
                "Set CCPY_BASE_URL to the OpenAI-compatible API root, usually 'https://<host>/v1', "
                "not the dashboard URL. "
            )
        return ""

    def _is_retryable_response(self, response: httpx.Response) -> bool:
        text = response.text.lower()
        if response.status_code == 429:
            return True
        if response.status_code in {500, 502, 503, 504}:
            non_retryable_markers = ("model_not_found", "no available channel", "invalid_request_error")
            return not any(marker in text for marker in non_retryable_markers)
        return False

    def _is_retryable_exception(self, exc: httpx.HTTPError) -> bool:
        return isinstance(
            exc,
            (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
            ),
        )

    def _exception_retry_delay(self, attempt: int) -> float:
        delay = self.config.model_retry_base_delay_s * (2**attempt)
        return min(delay, self.config.model_retry_max_delay_s)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), self.config.model_retry_max_delay_s)
            except ValueError:
                pass

        match = re.search(r"try again after\s+(\d+(?:\.\d+)?)\s*seconds?", response.text, re.IGNORECASE)
        if match:
            return min(float(match.group(1)), self.config.model_retry_max_delay_s)

        delay = self.config.model_retry_base_delay_s * (2**attempt)
        return min(delay, self.config.model_retry_max_delay_s)

    def _learn_rate_limit(self, response: httpx.Response) -> None:
        interval = self._rate_limit_interval_from_response(response)
        if interval is not None:
            self._adaptive_min_interval_s = max(self._adaptive_min_interval_s, interval)

    def _rate_limit_interval_from_response(self, response: httpx.Response) -> float | None:
        match = re.search(r"max RPM:\s*(\d+(?:\.\d+)?)", response.text, re.IGNORECASE)
        if not match:
            return None
        rpm = float(match.group(1))
        if rpm <= 0:
            return None
        return min(60.0 / rpm + 0.25, self.config.model_retry_max_delay_s)

    def _needs_reasoning_content_compat(self, response: httpx.Response) -> bool:
        if response.status_code != 400:
            return False
        text = response.text.lower()
        return "reasoning_content is missing" in text and "assistant tool call message" in text

    def _add_empty_reasoning_content(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        patched: list[dict[str, Any]] = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "assistant" and item.get("tool_calls") and "reasoning_content" not in item:
                item["reasoning_content"] = ""
            patched.append(item)
        return patched

    def _parse_sse_body(self, text: str) -> dict[str, Any] | None:
        """Accept providers that return OpenAI streaming chunks despite non-streaming requests."""
        if not text.lstrip().startswith("data:"):
            return None

        content_parts: list[str] = []
        tool_call_chunks: dict[int, dict[str, Any]] = {}
        final_message: dict[str, Any] | None = None
        finish_reason: str | None = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            message = choice.get("message")
            if isinstance(message, dict):
                final_message = message
                continue
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tool_delta in delta.get("tool_calls") or []:
                index = int(tool_delta.get("index", 0))
                current = tool_call_chunks.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_delta.get("id"):
                    current["id"] += tool_delta["id"]
                function_delta = tool_delta.get("function") or {}
                if function_delta.get("name"):
                    current["function"]["name"] += function_delta["name"]
                if function_delta.get("arguments"):
                    current["function"]["arguments"] += function_delta["arguments"]

        if final_message is not None:
            return {"choices": [{"message": final_message, "finish_reason": finish_reason}]}
        if content_parts or tool_call_chunks:
            tool_calls = [tool_call_chunks[index] for index in sorted(tool_call_chunks)]
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "".join(content_parts),
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": finish_reason,
                    }
                ]
            }
        return None
