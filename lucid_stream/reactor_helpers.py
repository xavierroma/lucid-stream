from __future__ import annotations

import asyncio
from typing import Any

from .errors import GracefulStop, RetryableError


def status_is_ready(status: Any) -> bool:
    status_name = getattr(status, "name", None)
    if isinstance(status_name, str) and status_name.lower() == "ready":
        return True

    normalized = str(status).lower()
    return normalized == "ready" or normalized.endswith(".ready")


async def wait_for_reactor_ready(
    reactor: Any,
    timeout: float,
    stop_event: asyncio.Event,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_status: Any = None

    while True:
        if stop_event.is_set():
            raise GracefulStop

        try:
            last_status = reactor.get_status()
        except Exception:
            last_status = None

        if status_is_ready(last_status):
            return

        if asyncio.get_running_loop().time() >= deadline:
            raise RetryableError(
                f"Timed out after {timeout}s waiting for Reactor READY state "
                f"(last status: {last_status}).",
            )

        await asyncio.sleep(0.1)


async def wait_for_remote_track(
    reactor: Any,
    timeout: float,
    stop_event: asyncio.Event,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout

    while True:
        if stop_event.is_set():
            raise GracefulStop

        track = reactor.get_remote_track()
        if track is not None:
            return track

        if asyncio.get_running_loop().time() >= deadline:
            raise RetryableError(
                f"Timed out after {timeout}s waiting for Reactor remote track.",
            )

        await asyncio.sleep(0.1)


def frame_to_rgb_bytes(frame: Any, width: int, height: int) -> bytes:
    if frame.width != width or frame.height != height or frame.format.name != "rgb24":
        frame = frame.reformat(width=width, height=height, format="rgb24")

    ndarray = frame.to_ndarray()
    return ndarray.tobytes()
