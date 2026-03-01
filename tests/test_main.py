import argparse
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app


def make_args(**overrides: object) -> argparse.Namespace:
    args = app.parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_config(max_retries: int = 5, start_prompt: str | None = None) -> app.StreamConfig:
    return app.StreamConfig(
        model_name="livecore",
        fps=30,
        video_bitrate_kbps=2500,
        max_retries=max_retries,
        track_wait_timeout=1.0,
        frame_timeout=1.0,
        reactor_api_key="rk_test",
        youtube_rtmp_url="rtmp://example/live2/key",
        start_prompt=start_prompt,
        youtube_api_key=None,
        youtube_video_id=None,
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


def test_resolve_youtube_url_rejects_youtube_url_without_stream_key() -> None:
    with pytest.raises(app.ConfigError):
        app.resolve_youtube_url({"YOUTUBE_RTMP_URL": "rtmp://a.rtmp.youtube.com/live2"})


def test_build_config_requires_reactor_api_key() -> None:
    args = make_args()
    with pytest.raises(app.ConfigError):
        app.build_config(args=args, env={"YOUTUBE_RTMP_URL": "rtmp://x"})


def test_build_config_uses_cli_start_prompt_over_env() -> None:
    args = make_args(start_prompt="cli prompt")
    config = app.build_config(
        args=args,
        env={
            "REACTOR_API_KEY": "rk_test",
            "YOUTUBE_RTMP_URL": "rtmp://x",
            "REACTOR_START_PROMPT": "env prompt",
        },
    )
    assert config.start_prompt == "cli prompt"


def test_build_config_uses_env_start_prompt_when_cli_missing() -> None:
    args = make_args()
    config = app.build_config(
        args=args,
        env={
            "REACTOR_API_KEY": "rk_test",
            "YOUTUBE_RTMP_URL": "rtmp://x",
            "REACTOR_START_PROMPT": "env prompt",
        },
    )
    assert config.start_prompt == "env prompt"


def test_build_config_reads_youtube_prompt_relay_env() -> None:
    args = make_args()
    config = app.build_config(
        args=args,
        env={
            "REACTOR_API_KEY": "rk_test",
            "YOUTUBE_RTMP_URL": "rtmp://x",
            "YOUTUBE_API_KEY": "yt-api-key",
            "YOUTUBE_VIDEO_ID": "abc123xyz00",
        },
    )
    assert config.youtube_api_key == "yt-api-key"
    assert config.youtube_video_id == "abc123xyz00"


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


def test_build_ffmpeg_cmd_includes_silent_audio_and_shortest() -> None:
    cmd = app.build_ffmpeg_cmd(
        width=1280,
        height=720,
        fps=30,
        rtmp_url="rtmp://out",
        video_bitrate_kbps=2500,
    )
    cmd_str = " ".join(cmd)
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in cmd_str
    assert "-shortest" in cmd
    assert "-map" in cmd
    assert "0:v:0" in cmd
    assert "1:a:0" in cmd
    assert "-flvflags" in cmd
    assert "no_duration_filesize" in cmd
    assert "-b:v" in cmd and "2500k" in cmd
    assert "nal-hrd=cbr:force-cfr=1" in cmd
    assert cmd[-2:] == ["flv", "rtmp://out"]


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


def test_frame_dimension_change_is_reformatted() -> None:
    reformat_calls: list[tuple[int, int, str]] = []

    class FakeArray:
        def __init__(self, size: int) -> None:
            self._size = size

        def tobytes(self) -> bytes:
            return b"\x01" * self._size

    class FakeFrame:
        def __init__(self) -> None:
            self.width = 1920
            self.height = 1080
            self.format = types.SimpleNamespace(name="yuv420p")

        def reformat(self, width: int, height: int, format: str) -> "FakeFrame":
            reformat_calls.append((width, height, format))
            self.width = width
            self.height = height
            self.format = types.SimpleNamespace(name=format)
            return self

        def to_ndarray(self) -> FakeArray:
            return FakeArray(self.width * self.height * 3)

    raw = app.frame_to_rgb_bytes(FakeFrame(), width=1280, height=720)
    assert reformat_calls == [(1280, 720, "rgb24")]
    assert len(raw) == 1280 * 720 * 3


def test_wait_for_reactor_ready_polls_until_ready() -> None:
    class FakeReactor:
        def __init__(self) -> None:
            self.statuses = ["connecting", "waiting", "ready"]

        def get_status(self) -> str:
            return self.statuses.pop(0)

    asyncio.run(
        app.wait_for_reactor_ready(FakeReactor(), timeout=1.0, stop_event=asyncio.Event()),
    )


def test_stream_once_closes_ffmpeg_and_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = asyncio.Event()
    created: dict[str, object] = {}

    class FakeArray:
        def __init__(self, size: int) -> None:
            self.size = size

        def tobytes(self) -> bytes:
            return b"\x00" * self.size

    class FakeFrame:
        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height
            self.format = types.SimpleNamespace(name="rgb24")

        def to_ndarray(self) -> FakeArray:
            return FakeArray(self.width * self.height * 3)

    class FakeTrack:
        async def recv(self) -> FakeFrame:
            return FakeFrame(64, 64)

    class FakeReactor:
        last_instance: "FakeReactor | None" = None

        def __init__(self, model_name: str, api_key: str) -> None:
            self.model_name = model_name
            self.api_key = api_key
            self.connected = False
            self.disconnected = False
            self.track = FakeTrack()
            self.sent_commands: list[tuple[str, dict[str, object]]] = []
            self.handlers: dict[str, object] = {}
            FakeReactor.last_instance = self

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnected = True

        def get_remote_track(self) -> FakeTrack:
            return self.track

        def get_status(self) -> str:
            return "ready"

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            self.sent_commands.append((command, data))

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

        def off(self, event: str, callback: object) -> None:
            if self.handlers.get(event) is callback:
                del self.handlers[event]

    class FakeStdin:
        def __init__(self) -> None:
            self.closed = False
            self.wait_closed_called = False

        def write(self, data: bytes) -> None:
            assert data

        async def drain(self) -> None:
            stop_event.set()

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.wait_closed_called = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.returncode: int | None = None
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        proc = FakeProcess()
        created["proc"] = proc
        return proc

    monkeypatch.setattr(app, "Reactor", FakeReactor)
    monkeypatch.setattr(app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(), stop_event))

    proc = created["proc"]
    assert isinstance(proc, FakeProcess)
    assert proc.stdin.closed is True
    assert proc.stdin.wait_closed_called is True

    reactor = FakeReactor.last_instance
    assert reactor is not None
    assert reactor.connected is True
    assert reactor.disconnected is True
    assert reactor.sent_commands == []
    assert "new_message" not in reactor.handlers


def test_stream_once_sends_start_prompt_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = asyncio.Event()
    created: dict[str, object] = {}

    class FakeArray:
        def __init__(self, size: int) -> None:
            self.size = size

        def tobytes(self) -> bytes:
            return b"\x00" * self.size

    class FakeFrame:
        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height
            self.format = types.SimpleNamespace(name="rgb24")

        def to_ndarray(self) -> FakeArray:
            return FakeArray(self.width * self.height * 3)

    class FakeTrack:
        async def recv(self) -> FakeFrame:
            return FakeFrame(64, 64)

    class FakeReactor:
        last_instance: "FakeReactor | None" = None

        def __init__(self, model_name: str, api_key: str) -> None:
            self.model_name = model_name
            self.api_key = api_key
            self.connected = False
            self.disconnected = False
            self.track = FakeTrack()
            self.sent_commands: list[tuple[str, dict[str, object]]] = []
            self.handlers: dict[str, object] = {}
            FakeReactor.last_instance = self

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnected = True

        def get_remote_track(self) -> FakeTrack:
            return self.track

        def get_status(self) -> str:
            return "ready"

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            self.sent_commands.append((command, data))

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

        def off(self, event: str, callback: object) -> None:
            if self.handlers.get(event) is callback:
                del self.handlers[event]

    class FakeStdin:
        def write(self, data: bytes) -> None:
            assert data

        async def drain(self) -> None:
            stop_event.set()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.returncode: int | None = None

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        proc = FakeProcess()
        created["proc"] = proc
        return proc

    monkeypatch.setattr(app, "Reactor", FakeReactor)
    monkeypatch.setattr(app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(start_prompt="A cinematic ocean"), stop_event))

    reactor = FakeReactor.last_instance
    assert reactor is not None
    assert reactor.sent_commands == [
        ("schedule_prompt", {"new_prompt": "A cinematic ocean", "timestamp": 0}),
        ("start", {}),
    ]
    assert "new_message" not in reactor.handlers
