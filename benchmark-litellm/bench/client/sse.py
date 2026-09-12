from __future__ import annotations

import orjson
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class StreamResult:
    content: str
    completion_tokens: Optional[int]
    terminated: bool
    chunk_count: int


def _iter_data_payloads(lines: Iterable[str]) -> Iterable[str]:
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        yield line[len("data:"):].strip()


def parse_openai_chat_stream(lines: Iterable[str]) -> StreamResult:
    parts = []
    completion_tokens = None
    terminated = False
    chunk_count = 0
    for payload in _iter_data_payloads(lines):
        if payload == "[DONE]":
            terminated = True
            break
        try:
            obj = orjson.loads(payload)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                parts.append(content)
                chunk_count += 1
        usage = obj.get("usage")
        if usage and usage.get("completion_tokens") is not None:
            completion_tokens = usage["completion_tokens"]
    return StreamResult(
        content="".join(parts), completion_tokens=completion_tokens,
        terminated=terminated, chunk_count=chunk_count,
    )


def parse_anthropic_messages_stream(lines: Iterable[str]) -> StreamResult:
    parts = []
    completion_tokens = None
    terminated = False
    chunk_count = 0
    for payload in _iter_data_payloads(lines):
        try:
            obj = orjson.loads(payload)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event_type = obj.get("type")
        if event_type == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                parts.append(delta.get("text", ""))
                chunk_count += 1
        elif event_type == "message_delta":
            usage = obj.get("usage") or {}
            if usage.get("output_tokens") is not None:
                completion_tokens = usage["output_tokens"]
        elif event_type == "message_stop":
            terminated = True
    return StreamResult(
        content="".join(parts), completion_tokens=completion_tokens,
        terminated=terminated, chunk_count=chunk_count,
    )
