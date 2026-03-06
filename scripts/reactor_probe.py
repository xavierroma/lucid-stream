from __future__ import annotations

import asyncio
import importlib.metadata
import os

from reactor_sdk import Reactor, ReactorStatus


MODEL_NAME = "livecore"
START_PROMPT = "In a sunny valley there is a little dragon playing with sheeps"
FRAME_COUNT = 60


def summarize_message(message: object) -> str:
    if not isinstance(message, dict):
        return f"type={type(message).__name__}"
    msg_type = message.get("type")
    data = message.get("data")
    if not isinstance(data, dict):
        return f"type={msg_type}"
    if msg_type == "state":
        frame = data.get("current_frame")
        paused = data.get("paused")
        prompt = data.get("current_prompt")
        prompt_len = len(prompt) if isinstance(prompt, str) else 0
        return f"type=state frame={frame} paused={paused} prompt_len={prompt_len}"
    if msg_type == "event":
        event = data.get("event") or data.get("type")
        return f"type=event event={event}"
    return f"type={msg_type}"



async def main() -> None:
    try:
        sdk_version = importlib.metadata.version("reactor-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "unknown"
    print(f"reactor-sdk version: {sdk_version}")

    api_key = os.getenv("REACTOR_API_KEY")
    if api_key is None:
        raise RuntimeError("REACTOR_API_KEY is not set")

    reactor = Reactor(model_name=MODEL_NAME, api_key=api_key)
    done = asyncio.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    frame_index = 0
    black_frames = 0

    @reactor.on_status
    def _on_status(status: ReactorStatus) -> None:
        print(f"status={status}")
        if status == ReactorStatus.READY:
            loop.call_soon_threadsafe(ready.set)

    @reactor.on_message
    def _on_message(message: object, scope: object) -> None:
        print(f"message(scope={scope}):", summarize_message(message))

    @reactor.on_frame
    def _on_frame(frame) -> None:
        nonlocal frame_index, black_frames
        frame_index += 1
        # frame is RGB ndarray in decorator API.
        rgb_mean = float(frame.mean())
        rgb_max = int(frame.max())
        rgb_black = rgb_mean <= 1.0 and rgb_max <= 3
        if rgb_black:
            black_frames += 1

        print(
            f"frame={frame_index} shape={getattr(frame, 'shape', None)} "
            f"rgb_mean={rgb_mean:.2f} rgb_max={rgb_max} rgb_black={rgb_black}"
        )
        if frame_index >= FRAME_COUNT:
            loop.call_soon_threadsafe(done.set)

    await reactor.connect()
    await asyncio.wait_for(ready.wait(), timeout=10)
    try:
        await reactor.send_command(
            "schedule_prompt",
            {"new_prompt": START_PROMPT, "timestamp": 0},
        )
        await reactor.send_command("start", {})

        await asyncio.wait_for(done.wait(), timeout=30)
        print(f"summary: black_frames={black_frames}/{FRAME_COUNT}")
    finally:
        await reactor.disconnect()


if __name__ == "__main__":
    asyncio.run(main())