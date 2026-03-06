import argparse
import asyncio
import sys
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lucid_stream import runner as app


def make_args(**overrides: object) -> argparse.Namespace:
    args = app.parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_env(**overrides: str) -> dict[str, str]:
    env = {
        "REACTOR_API_KEY": "rk_test",
        "YOUTUBE_RTMP_URL": "rtmp://a.rtmp.youtube.com/live2/key-123",
    }
    env.update(overrides)
    return env


def make_config(
    max_retries: int = 5,
    start_prompt: str | None = None,
    youtube_api_key: str | None = None,
    youtube_video_id: str | None = None,
    track_wait_timeout: float = 1.0,
) -> app.StreamConfig:
    return app.StreamConfig(
        model_name="livecore",
        fps=30,
        video_bitrate_kbps=2500,
        audio_bitrate_kbps=128,
        max_retries=max_retries,
        track_wait_timeout=track_wait_timeout,
        reactor_message_diagnostics=False,
        reactor_api_key="rk_test",
        youtube_rtmp_url="rtmp://a.rtmp.youtube.com/live2/key-123",
        start_prompt=start_prompt,
        youtube_api_key=youtube_api_key,
        youtube_video_id=youtube_video_id,
    )


def test_resolve_youtube_url_prefers_direct_url() -> None:
    env = {
        "YOUTUBE_RTMP_URL": "rtmp://direct/url",
        "YT_STREAM_KEY": "stream-key",
        "YT_RTMP_BASE": "rtmp://ignored/base",
    }
    assert app.resolve_youtube_url(env) == "rtmp://direct/url"


def test_resolve_youtube_url_uses_stream_key_and_base() -> None:
    env = {
        "YT_STREAM_KEY": "stream-key",
        "YT_RTMP_BASE": "rtmp://custom/base/",
    }
    assert app.resolve_youtube_url(env) == "rtmp://custom/base/stream-key"


def test_build_config_requires_reactor_key() -> None:
    args = make_args()
    env = make_env()
    del env["REACTOR_API_KEY"]
    with pytest.raises(app.ConfigError):
        app.build_config(args=args, env=env)


def test_build_config_uses_cli_start_prompt_over_env() -> None:
    args = make_args(start_prompt="cli prompt")
    config = app.build_config(
        args=args,
        env=make_env(REACTOR_START_PROMPT="env prompt"),
    )
    assert config.start_prompt == "cli prompt"


def test_extract_prompt_command_parses_valid_command() -> None:
    assert app.extract_prompt_command("/prompt  cinematic skyline") == "cinematic skyline"


def test_extract_prompt_command_ignores_non_command_text() -> None:
    assert app.extract_prompt_command("hello chat") is None
    assert app.extract_prompt_command("/prompting test") is None
    assert app.extract_prompt_command("/prompt   ") is None


def test_is_generation_reset_event_accepts_data_event_field() -> None:
    message = {"type": "event", "data": {"event": "generation_reset"}}
    assert app.is_generation_reset_event(message) is True


def test_extract_paused_flag_reads_state_message() -> None:
    message = {"type": "state", "data": {"paused": True}}
    assert app.extract_paused_flag(message) is True


def test_start_prompt_requires_ready_status() -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []

    class FakeReactor:
        def get_status(self) -> app.ReactorStatus:
            return app.ReactorStatus.WAITING

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            sent_commands.append((command, data))

    controller = app.ReactorPromptController(FakeReactor(), start_prompt="A sunset")
    with pytest.raises(app.RetryableError):
        asyncio.run(controller.send_start_prompt_if_configured())
    assert sent_commands == []


def test_start_prompt_schedules_frame_zero_before_start_when_ready() -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []

    class FakeReactor:
        def get_status(self) -> app.ReactorStatus:
            return app.ReactorStatus.READY

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            sent_commands.append((command, data))

    controller = app.ReactorPromptController(FakeReactor(), start_prompt="A sunset")
    asyncio.run(controller.send_start_prompt_if_configured())
    assert sent_commands == [
        ("schedule_prompt", {"new_prompt": "A sunset", "timestamp": 0}),
        ("start", {}),
    ]


def test_run_with_retries_stops_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_stream_once(config: app.StreamConfig, stop_event: asyncio.Event) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    async def fake_sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
        return None

    monkeypatch.setattr(app, "stream_once", fake_stream_once)
    monkeypatch.setattr(app, "sleep_with_stop", fake_sleep_with_stop)

    rc = asyncio.run(app.run_with_retries(make_config(max_retries=5), asyncio.Event()))
    assert rc == 1
    assert attempts == 5


def test_retry_backoff_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_stream_once(config: app.StreamConfig, stop_event: asyncio.Event) -> None:
        raise RuntimeError("retry me")

    async def fake_sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
        delays.append(seconds)

    monkeypatch.setattr(app, "stream_once", fake_stream_once)
    monkeypatch.setattr(app, "sleep_with_stop", fake_sleep_with_stop)

    rc = asyncio.run(app.run_with_retries(make_config(max_retries=5), asyncio.Event()))
    assert rc == 1
    assert delays == [2, 4, 8, 16]


def _install_fake_reactor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sent_commands: list[tuple[str, dict[str, object]]],
    disconnected: list[bool],
    emit_ready_on_connect: bool = True,
) -> None:
    class FakeReactor:
        def __init__(self, model_name: str, api_key: str) -> None:
            self.handlers: dict[str, list[Callable[..., None]]] = {}
            self.status = app.ReactorStatus.WAITING

        async def connect(self) -> None:
            if emit_ready_on_connect:
                self.status = app.ReactorStatus.READY
                for handler in self.handlers.get("status_changed", []):
                    handler(self.status)

        async def disconnect(self) -> None:
            disconnected.append(True)

        def get_status(self) -> app.ReactorStatus:
            return self.status

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            sent_commands.append((command, data))

        def on(self, event: str, callback: Callable[..., None]) -> None:
            self.handlers.setdefault(event, []).append(callback)

        def off(self, event: str, callback: Callable[..., None]) -> None:
            callbacks = self.handlers.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)

    monkeypatch.setattr(app, "Reactor", FakeReactor)


def test_connect_reactor_session_waits_for_ready_event(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    _install_fake_reactor(
        monkeypatch,
        sent_commands=sent_commands,
        disconnected=disconnected,
        emit_ready_on_connect=True,
    )

    reactor = asyncio.run(app.connect_reactor_session(make_config(), asyncio.Event()))
    assert reactor.get_status() == app.ReactorStatus.READY


def test_connect_reactor_session_timeout_without_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    _install_fake_reactor(
        monkeypatch,
        sent_commands=sent_commands,
        disconnected=disconnected,
        emit_ready_on_connect=False,
    )

    with pytest.raises(app.RetryableError):
        asyncio.run(
            app.connect_reactor_session(
                make_config(track_wait_timeout=0.01),
                asyncio.Event(),
            )
        )


def test_connect_reactor_session_respects_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    _install_fake_reactor(
        monkeypatch,
        sent_commands=sent_commands,
        disconnected=disconnected,
        emit_ready_on_connect=False,
    )

    stop_event = asyncio.Event()
    stop_event.set()

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.connect_reactor_session(make_config(), stop_event))


def test_stream_once_invokes_to_rtmp_with_expected_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    _install_fake_reactor(monkeypatch, sent_commands=sent_commands, disconnected=disconnected)

    calls: list[dict[str, object]] = []

    async def fake_to_rtmp(
        *,
        reactor_client: app.Reactor,
        target: app.RtmpTarget,
        video: app.VideoOptions,
        audio: app.AudioOptions,
        track_wait_timeout_sec: float,
    ) -> None:
        calls.append(
            {
                "reactor_client": reactor_client,
                "target": target,
                "video": video,
                "audio": audio,
                "track_wait_timeout_sec": track_wait_timeout_sec,
            }
        )

    monkeypatch.setattr(app, "to_rtmp", fake_to_rtmp)

    asyncio.run(app.stream_once(make_config(track_wait_timeout=7.5), asyncio.Event()))

    assert len(calls) == 1
    call = calls[0]
    assert call["target"].url == "rtmp://a.rtmp.youtube.com/live2/key-123"
    assert call["video"].fps == 30
    assert call["video"].bitrate_kbps == 2500
    assert call["audio"].sample_rate == 48000
    assert call["audio"].channels == 2
    assert call["track_wait_timeout_sec"] == 7.5
    assert disconnected == [True]


def test_stream_once_graceful_stop_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    stop_event = asyncio.Event()

    _install_fake_reactor(monkeypatch, sent_commands=sent_commands, disconnected=disconnected)

    async def fake_to_rtmp(
        *,
        reactor_client: app.Reactor,
        target: app.RtmpTarget,
        video: app.VideoOptions,
        audio: app.AudioOptions,
        track_wait_timeout_sec: float,
    ) -> None:
        stop_event.set()
        while True:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(app, "to_rtmp", fake_to_rtmp)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(), stop_event))

    assert disconnected == [True]


def test_stream_once_maps_reactor_egress_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []

    _install_fake_reactor(monkeypatch, sent_commands=sent_commands, disconnected=disconnected)

    class FakeEgressConfigError(Exception):
        pass

    async def fake_to_rtmp(
        *,
        reactor_client: app.Reactor,
        target: app.RtmpTarget,
        video: app.VideoOptions,
        audio: app.AudioOptions,
        track_wait_timeout_sec: float,
    ) -> None:
        raise FakeEgressConfigError("bad config")

    monkeypatch.setattr(app, "ReactorEgressConfigError", FakeEgressConfigError)
    monkeypatch.setattr(app, "to_rtmp", fake_to_rtmp)

    with pytest.raises(app.ConfigError):
        asyncio.run(app.stream_once(make_config(), asyncio.Event()))

    assert disconnected == [True]


def test_stream_once_sends_start_prompt_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    stop_event = asyncio.Event()

    _install_fake_reactor(monkeypatch, sent_commands=sent_commands, disconnected=disconnected)

    async def fake_to_rtmp(
        *,
        reactor_client: app.Reactor,
        target: app.RtmpTarget,
        video: app.VideoOptions,
        audio: app.AudioOptions,
        track_wait_timeout_sec: float,
    ) -> None:
        stop_event.set()
        while True:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(app, "to_rtmp", fake_to_rtmp)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(start_prompt="A cinematic ocean"), stop_event))

    assert sent_commands[:2] == [
        ("schedule_prompt", {"new_prompt": "A cinematic ocean", "timestamp": 0}),
        ("start", {}),
    ]
