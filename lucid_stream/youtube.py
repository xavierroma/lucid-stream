from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Awaitable, Callable, Mapping, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .constants import YOUTUBE_API_BASE
from .errors import RetryableError

RECENT_MESSAGE_IDS_MAXLEN = 1000
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


def extract_prompt_command(message_text: str) -> str | None:
    text = message_text.strip()
    if not text:
        return None

    lowered = text.lower()
    if not lowered.startswith("/prompt"):
        return None

    if len(text) > 7 and not text[7].isspace():
        return None

    prompt = text[7:].strip()
    return prompt or None


def fetch_json(url: str, params: Mapping[str, str]) -> dict[str, object]:
    full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=15) as response:
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise RetryableError("YouTube API returned a non-object payload.")
            return cast(dict[str, object], payload)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RetryableError(
            f"YouTube API HTTP {exc.code}: {detail[:200]}",
        ) from exc
    except URLError as exc:
        raise RetryableError(f"YouTube API request failed: {exc}") from exc


async def fetch_active_live_chat_id(api_key: str, video_id: str) -> str:
    data = await asyncio.to_thread(
        fetch_json,
        f"{YOUTUBE_API_BASE}/videos",
        {
            "part": "liveStreamingDetails",
            "id": video_id,
            "key": api_key,
        },
    )
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise RetryableError(
            f"Could not find YouTube video '{video_id}' via Data API.",
        )

    details = items[0].get("liveStreamingDetails", {})
    chat_id = details.get("activeLiveChatId")
    if not isinstance(chat_id, str) or not chat_id:
        raise RetryableError(
            "YouTube video has no active live chat yet.",
        )
    return chat_id


async def monitor_youtube_prompt_commands(
    *,
    api_key: str,
    video_id: str,
    stop_event: asyncio.Event,
    on_prompt: Callable[[str], Awaitable[None]],
    sleep_with_stop: Callable[[float, asyncio.Event], Awaitable[None]],
) -> None:
    chat_id = await fetch_active_live_chat_id(api_key=api_key, video_id=video_id)
    logging.info("YouTube /prompt relay enabled for video %s.", video_id)

    next_page_token: str | None = None
    initialized = False
    recent_ids: deque[str] = deque(maxlen=RECENT_MESSAGE_IDS_MAXLEN)
    recent_lookup: set[str] = set()

    while not stop_event.is_set():
        data = await asyncio.to_thread(
            fetch_json,
            f"{YOUTUBE_API_BASE}/liveChat/messages",
            build_live_chat_messages_params(
                chat_id=chat_id,
                api_key=api_key,
                next_page_token=next_page_token,
            ),
        )

        next_page_token = extract_next_page_token(data)

        items = data.get("items")
        if isinstance(items, list):
            if initialized:
                await process_live_chat_items(
                    items=items,
                    recent_ids=recent_ids,
                    recent_lookup=recent_lookup,
                    on_prompt=on_prompt,
                )
            initialized = True

        await sleep_with_stop(extract_poll_interval_seconds(data), stop_event)


def build_live_chat_messages_params(
    *,
    chat_id: str,
    api_key: str,
    next_page_token: str | None,
) -> dict[str, str]:
    params: dict[str, str] = {
        "part": "snippet",
        "liveChatId": chat_id,
        "key": api_key,
    }
    if next_page_token:
        params["pageToken"] = next_page_token
    return params


def extract_next_page_token(data: Mapping[str, object]) -> str | None:
    token = data.get("nextPageToken")
    if isinstance(token, str):
        return token
    return None


def extract_poll_interval_seconds(data: Mapping[str, object]) -> float:
    polling_ms = data.get("pollingIntervalMillis")
    if isinstance(polling_ms, int) and polling_ms > 0:
        return polling_ms / 1000
    return DEFAULT_POLL_INTERVAL_SECONDS


def mark_message_seen(
    *,
    message_id: str,
    recent_ids: deque[str],
    recent_lookup: set[str],
) -> bool:
    if message_id in recent_lookup:
        return False

    if len(recent_ids) >= recent_ids.maxlen:
        evicted = recent_ids.popleft()
        recent_lookup.discard(evicted)

    recent_ids.append(message_id)
    recent_lookup.add(message_id)
    return True


async def process_live_chat_items(
    *,
    items: list[object],
    recent_ids: deque[str],
    recent_lookup: set[str],
    on_prompt: Callable[[str], Awaitable[None]],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue

        message_id = item.get("id")
        if isinstance(message_id, str) and message_id:
            if not mark_message_seen(
                message_id=message_id,
                recent_ids=recent_ids,
                recent_lookup=recent_lookup,
            ):
                continue

        snippet = item.get("snippet")
        if not isinstance(snippet, dict):
            continue
        raw_message = snippet.get("displayMessage", "")
        if not isinstance(raw_message, str):
            continue

        prompt = extract_prompt_command(raw_message)
        if prompt:
            await on_prompt(prompt)


def extract_current_frame(message: object) -> int | None:
    if not isinstance(message, dict):
        return None
    if message.get("type") != "state":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    current_frame = data.get("current_frame")
    if isinstance(current_frame, int):
        return current_frame
    return None


def is_generation_reset_event(message: object) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("type") != "event":
        return False
    data = message.get("data")
    if not isinstance(data, dict):
        return False
    event_name = data.get("event") or data.get("type")
    return event_name == "generation_reset"


def extract_paused_flag(message: object) -> bool | None:
    if not isinstance(message, dict):
        return None
    if message.get("type") != "state":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    paused = data.get("paused")
    if isinstance(paused, bool):
        return paused
    return None
