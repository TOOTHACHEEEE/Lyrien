"""OpenAI 兼容 LLM 客户端。"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
    return _client


def _extract_json(text: str) -> str:
    """从可能包裹 markdown 代码块的文本中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉首尾的代码围栏
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_response(text: str) -> Any:
    """解析 LLM 返回的 JSON，失败则抛出异常。"""
    cleaned = _extract_json(text)
    return json.loads(cleaned)


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_retries: int = 2,
    json_mode: bool = False,
) -> str:
    """非流式调用，返回文本内容；支持失败重试。"""
    client = get_client()
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            kwargs: dict[str, Any] = {}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = await client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                continue
    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


async def chat_completion_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.5,
    max_retries: int = 3,
) -> Any:
    """调用 LLM 并解析返回的 JSON，自动处理 markdown 代码块与重试。"""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            content = await chat_completion(
                messages,
                temperature=temperature,
                max_retries=0,
                json_mode=(attempt == 0),  # 首次用 json_mode，失败后降级
            )
            return parse_json_response(content)
        except (json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < max_retries:
                # 重试时追加一条系统提示要求严格 JSON
                messages.append(
                    {
                        "role": "user",
                        "content": "请只输出合法 JSON，不要包含 markdown 代码块或解释。",
                    }
                )
                continue
    raise RuntimeError(f"LLM JSON 解析失败（已重试 {max_retries} 次）: {last_error}")


async def chat_completion_stream(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """流式调用，逐字返回文本（SSE 用）。"""
    client = get_client()
    stream = await client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
