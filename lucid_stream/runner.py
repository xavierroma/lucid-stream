from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from typing import Any, Callable, Sequence

from dotenv import load_dotenv

from .config import (
    StreamConfig,
    build_config,
    derive_livekit_api_url,
    parse_args,
    resolve_youtube_url,
    validate_youtube_rtmp_url,
)
from .constants import (
    CHAT_PROMPT_OFFSET_FRAMES,
    SILENT_AUDIO_CHANNELS,
    SILENT_AUDIO_FRAME_MS,
    SILENT_AUDIO_SAMPLE_RATE,
)
from .errors import ConfigError, GracefulStop, RetryableError
from .reactor_helpers import (
    frame_to_rgb_bytes,
    status_is_ready,
    wait_for_reactor_ready,
    wait_for_remote_track,
)
from .youtube import (
    extract_current_frame,
    extract_paused_flag,
    extract_prompt_command,
    is_generation_reset_event,
    monitor_youtube_prompt_commands,
)

try:
    from reactor_sdk import Reactor
except ImportError:  # pragma: no cover - handled at runtime
    Reactor = None  # type: ignore[assignment]

try:
    from livekit import api as lk_api
    from livekit import rtc
except ImportError:  # pragma: no cover - handled at runtime
    lk_api = None  # type: ignore[assignment]
    rtc = None  # type: ignore[assignment]


async def sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not stop_event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


def build_livekit_token(config: StreamConfig) -> str:
    if lk_api is None:
        raise ConfigError("livekit-api is not installed. Run `uv sync` before starting.")

    return (
        lk_api.AccessToken(config.livekit_api_key, config.livekit_api_secret)
        .with_identity(config.livekit_identity)
        .with_name("Reactor Bridge")
        .with_grants(
            lk_api.VideoGrants(
                room_join=True,
                room=config.livekit_room_name,
            )
        )
        .to_jwt()
    )


def build_track_composite_request(
    *,
    config: StreamConfig,
    width: int,
    height: int,
    video_track_id: str,
    audio_track_id: str,
) -> Any:
    if lk_api is None:
        raise ConfigError("livekit-api is not installed. Run `uv sync` before starting.")

    return lk_api.TrackCompositeEgressRequest(
        room_name=config.livekit_room_name,
        video_track_id=video_track_id,
        audio_track_id=audio_track_id,
        stream_outputs=[
            lk_api.StreamOutput(
                protocol=lk_api.StreamProtocol.RTMP,
                urls=[config.youtube_rtmp_url],
            )
        ],
        advanced=lk_api.EncodingOptions(
            width=width,
            height=height,
            framerate=config.fps,
            video_bitrate=config.video_bitrate_kbps * 1000,
            audio_bitrate=config.audio_bitrate_kbps * 1000,
            key_frame_interval=2.0,
        ),
    )


async def publish_video_frame(
    video_source: Any,
    frame: Any,
    width: int,
    height: int,
    *,
    timestamp_us: int,
) -> None:
    if rtc is None:
        raise ConfigError("livekit is not installed. Run `uv sync` before starting.")

    frame_bytes = frame_to_rgb_bytes(frame, width, height)
    lk_video_frame = rtc.VideoFrame(
        width=width,
        height=height,
        type=rtc.VideoBufferType.RGB24,
        data=frame_bytes,
    )
    video_source.capture_frame(lk_video_frame, timestamp_us=timestamp_us)


async def run_silent_audio_publisher(
    *,
    audio_source: Any,
    stop_event: asyncio.Event,
) -> None:
    if rtc is None:
        raise ConfigError("livekit is not installed. Run `uv sync` before starting.")

    samples_per_channel = (SILENT_AUDIO_SAMPLE_RATE * SILENT_AUDIO_FRAME_MS) // 1000
    frame_size_bytes = samples_per_channel * SILENT_AUDIO_CHANNELS * 2
    silent_payload = b"\x00" * frame_size_bytes

    while not stop_event.is_set():
        audio_frame = rtc.AudioFrame(
            data=silent_payload,
            sample_rate=SILENT_AUDIO_SAMPLE_RATE,
            num_channels=SILENT_AUDIO_CHANNELS,
            samples_per_channel=samples_per_channel,
        )
        await audio_source.capture_frame(audio_frame)
        await sleep_with_stop(SILENT_AUDIO_FRAME_MS / 1000.0, stop_event)


async def stop_egress(
    *,
    api_client: Any,
    egress_id: str | None,
) -> None:
    if lk_api is None or api_client is None or not egress_id:
        return

    try:
        await api_client.egress.stop_egress(lk_api.StopEgressRequest(egress_id=egress_id))
        logging.info("Stopped LiveKit egress %s.", egress_id)
    except Exception as exc:
        logging.warning("Failed stopping LiveKit egress %s: %s", egress_id, exc)


async def stream_once(config: StreamConfig, stop_event: asyncio.Event) -> None:
    if Reactor is None:
        raise ConfigError(
            "reactor-sdk is not installed. Run `uv sync` before starting.",
        )
    if lk_api is None or rtc is None:
        raise ConfigError(
            "livekit/livekit-api are not installed. Run `uv sync` before starting.",
        )

    reactor = Reactor(model_name=config.model_name, api_key=config.reactor_api_key)
    room = rtc.Room()
    lk_client: Any = None
    youtube_prompt_task: asyncio.Task[None] | None = None
    reset_task: asyncio.Task[None] | None = None
    audio_task: asyncio.Task[None] | None = None
    on_reactor_message: Callable[[Any], None] | None = None

    audio_source: Any = None
    egress_id: str | None = None

    current_frame: int | None = None
    model_paused: bool | None = None
    saw_state_message = False
    latest_prompt = config.start_prompt

    try:
        await reactor.connect()
        await wait_for_reactor_ready(
            reactor=reactor,
            timeout=config.track_wait_timeout,
            stop_event=stop_event,
        )

        async def schedule_prompt(
            prompt: str,
            *,
            source: str,
            timestamp: int | None = None,
        ) -> None:
            nonlocal latest_prompt, current_frame, model_paused
            latest_prompt = prompt
            prompt_timestamp = timestamp
            if prompt_timestamp is None:
                if model_paused:
                    prompt_timestamp = 0
                elif current_frame is not None:
                    prompt_timestamp = current_frame + CHAT_PROMPT_OFFSET_FRAMES
                else:
                    prompt_timestamp = CHAT_PROMPT_OFFSET_FRAMES
            await reactor.send_command(
                "schedule_prompt",
                {"new_prompt": prompt, "timestamp": prompt_timestamp},
            )
            logging.info(
                "Scheduled prompt from %s at frame %s.",
                source,
                prompt_timestamp,
            )

        async def handle_generation_reset() -> None:
            nonlocal model_paused
            if not latest_prompt:
                return
            await reactor.send_command(
                "schedule_prompt",
                {"new_prompt": latest_prompt, "timestamp": 0},
            )
            await reactor.send_command("start", {})
            model_paused = False
            logging.info("Generation reset detected; sent start with latest prompt.")

        def reactor_message_handler(message: Any, _scope: Any = None) -> None:
            nonlocal current_frame, reset_task, model_paused, saw_state_message

            frame_no = extract_current_frame(message)
            if frame_no is not None:
                current_frame = frame_no
                saw_state_message = True

            paused = extract_paused_flag(message)
            if paused is not None:
                model_paused = paused
                saw_state_message = True

            if is_generation_reset_event(message):
                if reset_task is None or reset_task.done():
                    reset_task = asyncio.create_task(handle_generation_reset())

        on_reactor_message = reactor_message_handler
        reactor.on("new_message", on_reactor_message)

        if config.start_prompt:
            logging.info("Scheduling start prompt at frame 0 and sending start command.")
            await schedule_prompt(config.start_prompt, source="startup", timestamp=0)
            await reactor.send_command("start", {})

        if config.youtube_api_key and config.youtube_video_id:
            youtube_api_key = config.youtube_api_key
            youtube_video_id = config.youtube_video_id

            async def on_chat_prompt(prompt: str) -> None:
                nonlocal model_paused, latest_prompt

                if not saw_state_message:
                    latest_prompt = prompt
                    await reactor.send_command("reset", {})
                    await reactor.send_command(
                        "schedule_prompt",
                        {"new_prompt": prompt, "timestamp": 0},
                    )
                    await reactor.send_command("start", {})
                    model_paused = False
                    logging.info(
                        "No Reactor state telemetry; forced reset/start for chat prompt.",
                    )
                    return

                await schedule_prompt(prompt, source="youtube-comment")
                if model_paused:
                    await reactor.send_command("start", {})
                    model_paused = False
                    logging.info("Model was paused; sent start after chat prompt.")

            async def run_youtube_prompt_relay() -> None:
                while not stop_event.is_set():
                    try:
                        await monitor_youtube_prompt_commands(
                            api_key=youtube_api_key,
                            video_id=youtube_video_id,
                            stop_event=stop_event,
                            on_prompt=on_chat_prompt,
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

            youtube_prompt_task = asyncio.create_task(run_youtube_prompt_relay())
        elif config.youtube_api_key or config.youtube_video_id:
            logging.warning(
                "YouTube /prompt relay disabled: set both YOUTUBE_API_KEY and "
                "YOUTUBE_VIDEO_ID.",
            )

        track = await wait_for_remote_track(
            reactor=reactor,
            timeout=config.track_wait_timeout,
            stop_event=stop_event,
        )

        first_frame = await asyncio.wait_for(track.recv(), timeout=config.frame_timeout)
        width = first_frame.width
        height = first_frame.height

        token = build_livekit_token(config)
        await room.connect(config.livekit_url, token)
        logging.info(
            "Connected to LiveKit room '%s' as '%s'.",
            config.livekit_room_name,
            config.livekit_identity,
        )

        video_source = rtc.VideoSource(width, height)
        video_track = rtc.LocalVideoTrack.create_video_track("reactor-video", video_source)
        video_pub = await room.local_participant.publish_track(
            video_track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                simulcast=False,
                video_encoding=rtc.VideoEncoding(
                    max_framerate=float(config.fps),
                    max_bitrate=config.video_bitrate_kbps * 1000,
                ),
            ),
        )

        audio_source = rtc.AudioSource(
            sample_rate=SILENT_AUDIO_SAMPLE_RATE,
            num_channels=SILENT_AUDIO_CHANNELS,
            queue_size_ms=1000,
        )
        audio_track = rtc.LocalAudioTrack.create_audio_track("silent-audio", audio_source)
        audio_pub = await room.local_participant.publish_track(
            audio_track,
            rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_MICROPHONE,
                audio_encoding=rtc.AudioEncoding(
                    max_bitrate=config.audio_bitrate_kbps * 1000,
                ),
            ),
        )
        audio_task = asyncio.create_task(
            run_silent_audio_publisher(audio_source=audio_source, stop_event=stop_event)
        )

        lk_client = lk_api.LiveKitAPI(
            url=config.livekit_api_url,
            api_key=config.livekit_api_key,
            api_secret=config.livekit_api_secret,
        )
        egress_req = build_track_composite_request(
            config=config,
            width=width,
            height=height,
            video_track_id=video_pub.sid,
            audio_track_id=audio_pub.sid,
        )
        egress_info = await lk_client.egress.start_track_composite_egress(egress_req)
        egress_id = egress_info.egress_id
        logging.info(
            "Started LiveKit track-composite egress %s to YouTube.",
            egress_id,
        )

        monotonic_start = time.monotonic()
        frames_sent = 0
        frames_dropped = 0
        last_publish_monotonic = 0.0
        min_frame_interval = 1.0 / config.fps
        last_heartbeat = monotonic_start

        await publish_video_frame(
            video_source,
            first_frame,
            width,
            height,
            timestamp_us=0,
        )
        frames_sent += 1
        last_publish_monotonic = time.monotonic()

        while True:
            if stop_event.is_set():
                raise GracefulStop

            if youtube_prompt_task is not None and youtube_prompt_task.done():
                if youtube_prompt_task.cancelled():
                    logging.info("YouTube /prompt relay task cancelled.")
                else:
                    exc = youtube_prompt_task.exception()
                    if exc is not None:
                        logging.warning("YouTube /prompt relay stopped: %s", exc)
                youtube_prompt_task = None

            if reset_task is not None and reset_task.done():
                if reset_task.cancelled():
                    logging.info("Generation-reset task cancelled.")
                else:
                    exc = reset_task.exception()
                    if exc is not None:
                        logging.warning("Failed to handle generation reset: %s", exc)
                reset_task = None

            if audio_task is not None and audio_task.done():
                if audio_task.cancelled():
                    logging.info("Silent audio task cancelled.")
                else:
                    exc = audio_task.exception()
                    if exc is not None:
                        raise RetryableError(f"Silent audio task failed: {exc}") from exc
                audio_task = None

            try:
                frame = await asyncio.wait_for(
                    track.recv(),
                    timeout=config.frame_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RetryableError(
                    f"Timed out after {config.frame_timeout}s waiting for frame.",
                ) from exc

            if frame.width != width or frame.height != height:
                raise RetryableError(
                    "Reactor frame dimensions changed "
                    f"from {width}x{height} to {frame.width}x{frame.height}.",
                )

            now = time.monotonic()
            if now - last_publish_monotonic < min_frame_interval:
                frames_dropped += 1
                continue

            timestamp_us = int((now - monotonic_start) * 1_000_000)
            await publish_video_frame(
                video_source,
                frame,
                width,
                height,
                timestamp_us=timestamp_us,
            )
            frames_sent += 1
            last_publish_monotonic = now

            if now - last_heartbeat >= 5.0:
                logging.info(
                    "Stream heartbeat: frames_sent=%s frames_dropped=%s reactor_frame=%s paused=%s state_seen=%s",
                    frames_sent,
                    frames_dropped,
                    current_frame,
                    model_paused,
                    saw_state_message,
                )
                last_heartbeat = now

    finally:
        if youtube_prompt_task is not None:
            youtube_prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await youtube_prompt_task

        if reset_task is not None:
            reset_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reset_task

        if audio_task is not None:
            audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await audio_task

        await stop_egress(api_client=lk_client, egress_id=egress_id)

        if lk_client is not None:
            with contextlib.suppress(Exception):
                await lk_client.aclose()

        if audio_source is not None:
            with contextlib.suppress(Exception):
                await audio_source.aclose()

        with contextlib.suppress(Exception):
            await room.disconnect()

        if on_reactor_message is not None:
            with contextlib.suppress(Exception):
                reactor.off("new_message", on_reactor_message)

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
