from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from typing import Callable, Sequence, TypeAlias

from dotenv import load_dotenv

from .config import (
    StreamConfig,
    build_config,
    parse_args,
    resolve_youtube_url,  # noqa: F401
    validate_youtube_rtmp_url,  # noqa: F401
)
from .constants import CHAT_PROMPT_OFFSET_FRAMES, SILENT_AUDIO_CHANNELS, SILENT_AUDIO_SAMPLE_RATE
from .errors import ConfigError, GracefulStop, RetryableError
from .youtube import (
    extract_current_frame,
    extract_paused_flag,
    extract_prompt_command,  # noqa: F401
    is_generation_reset_event,  # noqa: F401
    monitor_youtube_prompt_commands,
)

try:
    from reactor_sdk import Reactor, ReactorStatus
except ImportError:  # pragma: no cover - handled at runtime
    Reactor = None  # type: ignore[assignment]
    ReactorStatus = None  # type: ignore[assignment]

try:
    from reactor_egress import (
        AudioOptions,
        ConfigError as ReactorEgressConfigError,
        EgressError as ReactorEgressError,
        RtmpTarget,
        VideoOptions,
        to_rtmp,
    )
except ImportError:  # pragma: no cover - handled at runtime
    AudioOptions = None  # type: ignore[assignment]
    ReactorEgressConfigError = Exception  # type: ignore[assignment]
    ReactorEgressError = Exception  # type: ignore[assignment]
    RtmpTarget = None  # type: ignore[assignment]
    VideoOptions = None  # type: ignore[assignment]
    to_rtmp = None  # type: ignore[assignment]


JsonValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | dict[str, "JsonValue"]
    | list["JsonValue"]
)


def summarize_reactor_message(message: JsonValue) -> str:
    if not isinstance(message, dict):
        return f"type={type(message).__name__}"

    message_type = message.get("type")
    data = message.get("data")
    if not isinstance(data, dict):
        return f"type={message_type}"

    if message_type == "state":
        current_frame = data.get("current_frame")
        paused = data.get("paused")
        prompt = data.get("current_prompt")
        prompt_len = len(prompt) if isinstance(prompt, str) else 0
        scheduled = data.get("scheduled_prompts")
        scheduled_count = len(scheduled) if isinstance(scheduled, dict) else 0
        return (
            "type=state "
            f"frame={current_frame} paused={paused} "
            f"prompt_len={prompt_len} scheduled={scheduled_count}"
        )

    if message_type == "event":
        event_name = data.get("event") or data.get("type")
        return f"type=event event={event_name}"

    return f"type={message_type}"


class ReactorPromptController:
    def __init__(
        self,
        reactor: Reactor,
        *,
        start_prompt: str | None,
        log_reactor_messages: bool = False,
    ) -> None:
        self._reactor = reactor
        self._start_prompt = start_prompt
        self._log_reactor_messages = log_reactor_messages
        self.current_frame: int | None = None
        self.model_paused: bool | None = None
        self.saw_state_message = False
        self.latest_prompt = start_prompt
        self._handler: Callable[[JsonValue], None] | None = None

    async def attach(self) -> None:
        self._handler = self._on_reactor_message
        self._reactor.on("message", self._handler)

    async def close(self) -> None:
        if self._handler is not None:
            with contextlib.suppress(Exception):
                self._reactor.off("message", self._handler)
            self._handler = None

    async def send_start_prompt_if_configured(self) -> None:
        if not self._start_prompt:
            logging.warning("Start prompt is not configured, skipping schedule prompt at t=0")
            return

        status = self._reactor.get_status()
        if status != ReactorStatus.READY:
            raise RetryableError(
                "Reactor must be READY before sending startup prompt/start "
                f"(current status: {status})."
            )

        logging.info("Scheduling start prompt at frame 0, then sending start.")
        await self.schedule_prompt(self._start_prompt, source="startup", timestamp=0)
        await self._reactor.send_command("start", {})

    async def schedule_prompt(
        self,
        prompt: str,
        *,
        source: str,
        timestamp: int | None = None,
    ) -> None:
        self.latest_prompt = prompt
        prompt_timestamp = timestamp
        if prompt_timestamp is None:
            if self.model_paused:
                prompt_timestamp = 0
            elif self.current_frame is not None:
                prompt_timestamp = self.current_frame + CHAT_PROMPT_OFFSET_FRAMES
            else:
                prompt_timestamp = CHAT_PROMPT_OFFSET_FRAMES

        await self._reactor.send_command(
            "schedule_prompt",
            {
                "new_prompt": prompt,
                "timestamp": prompt_timestamp,
            },
        )
        logging.info(
            "Scheduled prompt (%s) at frame %s (len=%s).",
            source,
            prompt_timestamp,
            len(prompt),
        )

    async def on_chat_prompt(self, prompt: str) -> None:
        if self.model_paused or not self.saw_state_message:
            logging.info(
                "Model paused or state unavailable; applying chat prompt at frame 0 and starting.",
            )
            await self.schedule_prompt(prompt, source="youtube-comment", timestamp=0)
            await self._reactor.send_command("start", {})
            return

        await self.schedule_prompt(prompt, source="youtube-comment")

    def _on_reactor_message(self, message: JsonValue) -> None:
        if self._log_reactor_messages:
            logging.info("Reactor message: %s", summarize_reactor_message(message))

        current_frame = extract_current_frame(message)
        if current_frame is not None:
            self.current_frame = current_frame
            self.saw_state_message = True

        paused = extract_paused_flag(message)
        if paused is not None:
            self.model_paused = paused
            self.saw_state_message = True



async def sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not stop_event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


async def init_reactor(
    *,
    reactor: Reactor,
    timeout: float,
    stop_event: asyncio.Event,
) -> None:
    if stop_event.is_set():
        raise GracefulStop

    ready = asyncio.Event()

    def on_status(status: ReactorStatus) -> None:
        if status == ReactorStatus.READY:
            ready.set()

    reactor.on("status_changed", on_status)
    try:
        await reactor.connect()

        if reactor.get_status() == ReactorStatus.READY:
            return

        ready_task = asyncio.create_task(ready.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {ready_task, stop_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for pending_task in pending:
            pending_task.cancel()
        for pending_task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await pending_task

        if ready_task in done:
            return
        if stop_task in done:
            raise GracefulStop

        raise RetryableError(
            f"Timed out after {timeout}s waiting for Reactor READY state "
            f"(last status: {reactor.get_status()}).",
        )
    finally:
        with contextlib.suppress(Exception):
            reactor.off("status_changed", on_status)


async def connect_reactor_session(
    config: StreamConfig,
    stop_event: asyncio.Event,
) -> Reactor:
    if Reactor is None or ReactorStatus is None:
        raise ConfigError("reactor-sdk is not installed. Run `uv sync` before starting.")

    reactor = Reactor(model_name=config.model_name, api_key=config.reactor_api_key)

    await init_reactor(
        reactor=reactor,
        timeout=config.track_wait_timeout,
        stop_event=stop_event,
    )
    return reactor


def start_chat_prompt_relay(
    *,
    config: StreamConfig,
    controller: ReactorPromptController,
    stop_event: asyncio.Event,
) -> asyncio.Task[None] | None:
    if config.youtube_api_key and config.youtube_video_id:
        youtube_api_key = config.youtube_api_key
        youtube_video_id = config.youtube_video_id

        async def run_youtube_prompt_relay() -> None:
            while not stop_event.is_set():
                try:
                    await monitor_youtube_prompt_commands(
                        api_key=youtube_api_key,
                        video_id=youtube_video_id,
                        stop_event=stop_event,
                        on_prompt=controller.on_chat_prompt,
                        sleep_with_stop=sleep_with_stop,
                    )
                    return
                except Exception as exc:
                    if stop_event.is_set():
                        return
                    logging.warning(
                        "YouTube /prompt relay error: %s. Retrying in 3s.",
                        exc,
                    )
                    await sleep_with_stop(3.0, stop_event)

        return asyncio.create_task(run_youtube_prompt_relay())

    if config.youtube_api_key or config.youtube_video_id:
        logging.warning(
            "YouTube /prompt relay disabled: set both YOUTUBE_API_KEY and YOUTUBE_VIDEO_ID.",
        )

    return None


def _check_background_task(
    task: asyncio.Task[None] | None,
    *,
    task_name: str,
    retryable_on_exception: bool,
) -> asyncio.Task[None] | None:
    if task is None or not task.done():
        return task

    if task.cancelled():
        logging.info("%s cancelled.", task_name)
        return None

    exc = task.exception()
    if exc is None:
        return None

    if retryable_on_exception:
        raise RetryableError(f"{task_name} failed: {exc}") from exc

    logging.warning("%s stopped: %s", task_name, exc)
    return None


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run_reactor_egress(
    *,
    reactor: Reactor,
    config: StreamConfig,
) -> None:
    if VideoOptions is None or AudioOptions is None or RtmpTarget is None or to_rtmp is None:
        raise ConfigError("reactor-egress is not installed. Run `uv sync` before starting.")

    target = RtmpTarget(url=config.youtube_rtmp_url)
    video = VideoOptions(
        width=832,
        height=480,
        fps=config.fps,
        bitrate_kbps=config.video_bitrate_kbps,
    )
    audio = AudioOptions(
        inject_silence=True,
        sample_rate=SILENT_AUDIO_SAMPLE_RATE,
        channels=SILENT_AUDIO_CHANNELS,
    )

    try:
        await to_rtmp(
            reactor_client=reactor,
            target=target,
            video=video,
            audio=audio,
            track_wait_timeout_sec=config.track_wait_timeout,
        )
    except ReactorEgressConfigError as exc:
        raise ConfigError(str(exc)) from exc
    except ReactorEgressError as exc:
        raise RetryableError(f"reactor-egress failed: {exc}") from exc


async def stream_once(config: StreamConfig, stop_event: asyncio.Event) -> None:
    if Reactor is None or ReactorStatus is None:
        raise ConfigError("reactor-sdk is not installed. Run `uv sync` before starting.")

    reactor: Reactor | None = None
    controller: ReactorPromptController | None = None
    youtube_prompt_task: asyncio.Task[None] | None = None
    egress_task: asyncio.Task[None] | None = None

    try:
        reactor = await connect_reactor_session(config, stop_event)
        controller = ReactorPromptController(
            reactor,
            start_prompt=config.start_prompt,
            log_reactor_messages=config.reactor_message_diagnostics,
        )
        await controller.attach()
        await controller.send_start_prompt_if_configured()

        youtube_prompt_task = start_chat_prompt_relay(
            config=config,
            controller=controller,
            stop_event=stop_event,
        )
        egress_task = asyncio.create_task(
            _run_reactor_egress(
                reactor=reactor,
                config=config,
            )
        )

        while True:
            if stop_event.is_set():
                raise GracefulStop

            youtube_prompt_task = _check_background_task(
                youtube_prompt_task,
                task_name="YouTube /prompt relay task",
                retryable_on_exception=False,
            )

            if egress_task.done():
                await egress_task
                return

            await asyncio.sleep(0.25)

    finally:
        await _cancel_task(youtube_prompt_task)
        await _cancel_task(egress_task)
        if controller is not None:
            await controller.close()
        if reactor is not None:
            with contextlib.suppress(Exception):
                await reactor.disconnect()


async def run_with_retries(config: StreamConfig, stop_event: asyncio.Event) -> int:
    for attempt in range(1, config.max_retries + 1):
        try:
            logging.info("Streaming attempt %s/%s", attempt, config.max_retries)
            await stream_once(config, stop_event)
            return 0
        except GracefulStop:
            logging.info("Shutdown requested, stopping stream.")
            return 0
        except ConfigError:
            raise
        except Exception as exc:
            if stop_event.is_set():
                logging.info("Shutdown requested during failure handling.")
                return 0

            if attempt >= config.max_retries:
                logging.error("Attempt %s failed and no retries remain: %s", attempt, exc)
                return 1

            backoff = 2**attempt
            logging.warning(
                "Attempt %s failed: %s. Retrying in %ss.",
                attempt,
                exc,
                backoff,
            )
            await sleep_with_stop(backoff, stop_event)

    return 1


def register_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue


async def main_async(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    config = build_config(args=args, env=os.environ)
    stop_event = asyncio.Event()
    register_signal_handlers(stop_event)
    return await run_with_retries(config, stop_event)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(main_async(argv))
    except ConfigError as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
