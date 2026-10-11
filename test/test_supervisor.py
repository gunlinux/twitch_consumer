"""Tests for the combined Docker entry point (src.supervisor).

The supervisor is exercised with fakes: no real child processes, signals, or
sleeps are used.
"""

import sys
from types import SimpleNamespace

import pytest

from src import supervisor


class FakeProcess:
    def __init__(self, cmd: list[str], returncode: int | None = None) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def wait(self) -> int:
        return self.returncode if self.returncode is not None else -15


class FakeSubprocess:
    def __init__(self, exit_codes: list[int | None] | None = None) -> None:
        self._exit_codes = exit_codes or []
        self.calls: list[list[str]] = []
        self.procs: list[FakeProcess] = []

    def Popen(self, cmd: list[str]) -> FakeProcess:
        index = len(self.procs)
        code = self._exit_codes[index] if index < len(self._exit_codes) else None
        self.calls.append(cmd)
        proc = FakeProcess(cmd, returncode=code)
        self.procs.append(proc)
        return proc


class FakeSignal:
    SIGTERM = 15
    SIGINT = 2

    def __init__(self) -> None:
        self.handlers: dict[int, object] = {}

    def signal(self, sig: int, handler: object) -> None:
        self.handlers[sig] = handler


def test_launch_builds_both_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSubprocess()
    monkeypatch.setattr(supervisor, "subprocess", fake)

    procs = supervisor._launch()

    assert len(procs) == 2
    assert fake.calls[0] == [sys.executable, "-m", "src"]
    assert fake.calls[1] == [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "17563",
    ]


def test_main_exits_with_child_code(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSubprocess(exit_codes=[3, None])
    monkeypatch.setattr(supervisor, "subprocess", fake)
    monkeypatch.setattr(supervisor, "signal", FakeSignal())
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(sleep=lambda _: None))

    with pytest.raises(SystemExit) as excinfo:
        supervisor.main()

    assert excinfo.value.code == 3
    assert fake.procs[1].terminated


def test_signal_shuts_down_both_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSubprocess()
    fake_signal = FakeSignal()
    monkeypatch.setattr(supervisor, "subprocess", fake)
    monkeypatch.setattr(supervisor, "signal", fake_signal)
    slept = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        slept["n"] += 1
        if slept["n"] == 1:
            fake_signal.handlers[FakeSignal.SIGTERM](FakeSignal.SIGTERM, None)

    monkeypatch.setattr(supervisor, "time", SimpleNamespace(sleep=fake_sleep))

    with pytest.raises(SystemExit) as excinfo:
        supervisor.main()

    assert excinfo.value.code == 0
    assert all(proc.terminated for proc in fake.procs)
