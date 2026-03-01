from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from dotenv import load_dotenv

try:
    from reactor_sdk import Reactor
except ImportError:  # pragma: no cover - handled at runtime in stream_once
    Reactor = None  # type: ignore[assignment]

YOUTUBE_DEFAULT_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
CHAT_PROMPT_OFFSET_FRAMES = 5


class ConfigError(Exception):
    """Raised when CLI or environment configuration is invalid."""


class RetryableError(Exception):
    """Raised for transient failures that should trigger a retry."""


class GracefulStop(Exception):
    """Raised when shutdown is requested."""


@dataclass(frozen=True)
class StreamConfig:
    model_name: str
    fps: int
    video_bitrate_kbps: int
    max_retries: int
    track_wait_timeout: float
    frame_timeout: float
    reactor_api_key: str
    youtube_rtmp_url: str
    start_prompt: str | None
    youtube_api_key: str | None
    youtube_video_id: str | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a Reactor remote video track to YouTube Live.",
    )
    parser.add_argument("--model-name", default="livecore")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--video-bitrate-kbps", type=int, default=2500)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--track-wait-timeout", type=float, default=30.0)
    parser.add_argument("--frame-timeout", type=float, default=10.0)
    parser.add_argument(
        "--start-prompt",
        default=None,
        help="Prompt to schedule at frame 0 before sending Reactor start command.",
    )
    parser.add_argument(
        "--youtube-api-key",
        default=None,
        help="YouTube Data API key (or set YOUTUBE_API_KEY) for /prompt chat relay.",
    )
    parser.add_argument(
        "--youtube-video-id",
        default=None,
        help="YouTube live video ID (or set YOUTUBE_VIDEO_ID) for /prompt chat relay.",
    )
    return parser.parse_args(argv)


def resolve_youtube_url(env: Mapping[str, str]) -> str:
    direct_url = env.get("YOUTUBE_RTMP_URL", "").strip()
    if direct_url:
        validate_youtube_rtmp_url(direct_url)
        return direct_url

    stream_key = env.get("YT_STREAM_KEY", "").strip()
    if not stream_key:
        raise ConfigError(
            "Missing YouTube target. Set YOUTUBE_RTMP_URL or YT_STREAM_KEY.",
        )

    base_url = env.get("YT_RTMP_BASE", YOUTUBE_DEFAULT_RTMP_BASE).strip()
    base_url = base_url.rstrip("/")
    resolved_url = f"{base_url}/{stream_key}"
    validate_youtube_rtmp_url(resolved_url)
    return resolved_url


def validate_youtube_rtmp_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"rtmp", "rtmps"}:
        return

    host = parsed.netloc.lower()
    if "youtube.com" not in host:
        return

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ConfigError(
            "YOUTUBE_RTMP_URL is missing the stream key. "
            "Expected: rtmp://a.rtmp.youtube.com/live2/<stream_key>",
        )


def build_config(args: argparse.Namespace, env: Mapping[str, str]) -> StreamConfig:
    api_key = env.get("REACTOR_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("Missing REACTOR_API_KEY environment variable.")

    if args.fps <= 0:
        raise ConfigError("--fps must be a positive integer.")
    if args.video_bitrate_kbps <= 0:
        raise ConfigError("--video-bitrate-kbps must be a positive integer.")
    if args.max_retries <= 0:
        raise ConfigError("--max-retries must be a positive integer.")
    if args.track_wait_timeout <= 0:
        raise ConfigError("--track-wait-timeout must be positive.")
    if args.frame_timeout <= 0:
        raise ConfigError("--frame-timeout must be positive.")

    start_prompt = args.start_prompt
    if start_prompt is None:
        start_prompt = env.get("REACTOR_START_PROMPT")
    if start_prompt is not None:
        start_prompt = start_prompt.strip() or None

    youtube_api_key = args.youtube_api_key
    if youtube_api_key is None:
        youtube_api_key = env.get("YOUTUBE_API_KEY")
    youtube_api_key = youtube_api_key.strip() if youtube_api_key else None

    youtube_video_id = args.youtube_video_id
    if youtube_video_id is None:
        youtube_video_id = env.get("YOUTUBE_VIDEO_ID")
    youtube_video_id = youtube_video_id.strip() if youtube_video_id else None

    return StreamConfig(
        model_name=args.model_name,
        fps=args.fps,
        video_bitrate_kbps=args.video_bitrate_kbps,
        max_retries=args.max_retries,
        track_wait_timeout=args.track_wait_timeout,
        frame_timeout=args.frame_timeout,
        reactor_api_key=api_key,
        youtube_rtmp_url=resolve_youtube_url(env),
        start_prompt=start_prompt,
        youtube_api_key=youtube_api_key,
        youtube_video_id=youtube_video_id,
    )


def build_ffmpeg_cmd(
    width: int,
    height: int,
    fps: int,
    rtmp_url: str,
    video_bitrate_kbps: int,
) -> list[str]:
    bitrate = f"{video_bitrate_kbps}k"
    bufsize = f"{video_bitrate_kbps * 2}k"

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        bitrate,
        "-maxrate",
        bitrate,
        "-minrate",
        bitrate,
        "-bufsize",
        bufsize,
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1",
        "-g",
        str(fps * 2),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-flvflags",
        "no_duration_filesize",
        "-f",
        "flv",
        rtmp_url,
    ]


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


def extract_prompt_command(message_text: str) -> str | None:
    text = message_text.strip()
    if not text:
        return None

    lowered = text.lower()
    if not lowered.startswith("/prompt"):
        return None

    # Ensure "/prompting" does not match.
    if len(text) > 7 and not text[7].isspace():
        return None

    prompt = text[7:].strip()
    return prompt or None


def fetch_json(url: str, params: Mapping[str, str]) -> dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=15) as response:
            return json.load(response)
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
) -> None:
    chat_id = await fetch_active_live_chat_id(api_key=api_key, video_id=video_id)
    logging.info("YouTube /prompt relay enabled for video %s.", video_id)

    next_page_token: str | None = None
    initialized = False
    recent_ids: deque[str] = deque(maxlen=1000)
    recent_lookup: set[str] = set()

    while not stop_event.is_set():
        params: dict[str, str] = {
            "part": "snippet",
            "liveChatId": chat_id,
            "key": api_key,
        }
        if next_page_token:
            params["pageToken"] = next_page_token

        data = await asyncio.to_thread(
            fetch_json,
            f"{YOUTUBE_API_BASE}/liveChat/messages",
            params,
        )

        if "nextPageToken" in data and isinstance(data["nextPageToken"], str):
            next_page_token = data["nextPageToken"]

        items = data.get("items")
        if isinstance(items, list):
            if initialized:
                for item in items:
                    if not isinstance(item, dict):
                        continue

                    message_id = item.get("id")
                    if isinstance(message_id, str) and message_id:
                        if message_id in recent_lookup:
                            continue
                        if len(recent_ids) >= recent_ids.maxlen:
                            evicted = recent_ids.popleft()
                            recent_lookup.discard(evicted)
                        recent_ids.append(message_id)
                        recent_lookup.add(message_id)

                    snippet = item.get("snippet")
                    if not isinstance(snippet, dict):
                        continue
                    raw_message = snippet.get("displayMessage", "")
                    if not isinstance(raw_message, str):
                        continue

                    prompt = extract_prompt_command(raw_message)
                    if prompt:
                        await on_prompt(prompt)
            else:
                # Avoid replaying historical backlog when relay starts.
                initialized = True

        polling_ms = data.get("pollingIntervalMillis")
        if isinstance(polling_ms, int) and polling_ms > 0:
            sleep_seconds = polling_ms / 1000
        else:
            sleep_seconds = 2.0
        await sleep_with_stop(sleep_seconds, stop_event)


def extract_current_frame(message: Any) -> int | None:
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


def is_generation_reset_event(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("type") != "event":
        return False
    data = message.get("data")
    if not isinstance(data, dict):
        return False
    event_name = data.get("event") or data.get("type")
    return event_name == "generation_reset"


def extract_paused_flag(message: Any) -> bool | None:
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


def frame_to_rgb_bytes(frame: Any, width: int, height: int) -> bytes:
    if frame.width != width or frame.height != height or frame.format.name != "rgb24":
        frame = frame.reformat(width=width, height=height, format="rgb24")

    ndarray = frame.to_ndarray()
    return ndarray.tobytes()


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


async def close_ffmpeg_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return

    if proc.stdin is not None:
        try:
            proc.stdin.close()
            await proc.stdin.wait_closed()
        except Exception:
            pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def write_frame_to_ffmpeg(
    proc: asyncio.subprocess.Process,
    frame: Any,
    width: int,
    height: int,
    *,
    drain_timeout: float,
) -> None:
    if proc.stdin is None:
        raise RetryableError("ffmpeg stdin is unavailable.")

    if proc.returncode is not None:
        raise RetryableError(f"ffmpeg exited with code {proc.returncode}.")

    frame_bytes = frame_to_rgb_bytes(frame, width, height)

    try:
        proc.stdin.write(frame_bytes)
        await asyncio.wait_for(proc.stdin.drain(), timeout=drain_timeout)
    except asyncio.TimeoutError as exc:
        raise RetryableError(
            f"Timed out after {drain_timeout}s writing frame to ffmpeg.",
        ) from exc
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise RetryableError("ffmpeg stdin closed unexpectedly.") from exc


async def stream_once(config: StreamConfig, stop_event: asyncio.Event) -> None:
    if Reactor is None:
        raise ConfigError(
            "reactor-sdk is not installed. Run `uv sync` before starting.",
        )

    reactor = Reactor(model_name=config.model_name, api_key=config.reactor_api_key)
    ffmpeg_proc: asyncio.subprocess.Process | None = None
    youtube_prompt_task: asyncio.Task[None] | None = None
    reset_task: asyncio.Task[None] | None = None
    current_frame: int | None = None
    model_paused: bool | None = None
    saw_state_message = False
    latest_prompt = config.start_prompt
    on_reactor_message: Callable[[Any], None] | None = None

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

            frame = extract_current_frame(message)
            if frame is not None:
                current_frame = frame
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

        first_frame = await asyncio.wait_for(
            track.recv(),
            timeout=config.frame_timeout,
        )
        width = first_frame.width
        height = first_frame.height

        ffmpeg_cmd = build_ffmpeg_cmd(
            width=width,
            height=height,
            fps=config.fps,
            rtmp_url=config.youtube_rtmp_url,
            video_bitrate_kbps=config.video_bitrate_kbps,
        )
        logging.info("Starting ffmpeg: %s", " ".join(ffmpeg_cmd))

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
        )

        await write_frame_to_ffmpeg(
            ffmpeg_proc,
            first_frame,
            width,
            height,
            drain_timeout=config.frame_timeout,
        )
        frames_sent = 1
        last_heartbeat = asyncio.get_running_loop().time()

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

            if ffmpeg_proc.returncode is not None:
                raise RetryableError(
                    f"ffmpeg exited with code {ffmpeg_proc.returncode}.",
                )

            try:
                frame = await asyncio.wait_for(
                    track.recv(),
                    timeout=config.frame_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RetryableError(
                    f"Timed out after {config.frame_timeout}s waiting for frame.",
                ) from exc

            await write_frame_to_ffmpeg(
                ffmpeg_proc,
                frame,
                width,
                height,
                drain_timeout=config.frame_timeout,
            )
            frames_sent += 1

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= 5.0:
                logging.info(
                    "Stream heartbeat: frames_sent=%s reactor_frame=%s paused=%s state_seen=%s",
                    frames_sent,
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

        if on_reactor_message is not None:
            with contextlib.suppress(Exception):
                reactor.off("new_message", on_reactor_message)

        await close_ffmpeg_process(ffmpeg_proc)
        try:
            await reactor.disconnect()
        except Exception:
            logging.exception("Reactor disconnect failed.")


async def sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not stop_event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


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


if __name__ == "__main__":
    raise SystemExit(main())
