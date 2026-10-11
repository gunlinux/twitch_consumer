"""Run the chat consumer and the OAuth server together in one container.

The Docker image is a single service; this module starts both long-lived
entry points as separate child processes — the consumer (`python -m src`) and
the OAuth callback server (`uvicorn src.main:app`) — forwards SIGTERM/SIGINT
to both, and exits as soon as either one stops so the container is managed as
one unit. They stay in separate processes because each owns its own asyncio
event loop and the shared `DatabaseManager` pool is bound to the loop that
created it.

Run with `python -m src.supervisor`.
"""

import signal
import subprocess
import sys
import time
from types import FrameType

from src.infrastructure.config import settings


def _launch() -> list[subprocess.Popen[bytes]]:
    """Spawn the consumer and the OAuth callback server, one process each."""
    return [
        subprocess.Popen([sys.executable, "-m", "src"]),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "src.main:app",
                "--host",
                settings.oauth_bind_host,
                "--port",
                str(settings.oauth_bind_port),
            ]
        ),
    ]


def main() -> None:
    procs = _launch()
    stopping = False

    def terminate_all() -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        terminate_all()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while True:
        for proc in procs:
            code = proc.poll()
            if code is not None:
                # One process stopped: bring the other down and exit with the
                # child's code (0 when we were asked to shut down gracefully).
                terminate_all()
                for other in procs:
                    other.wait()
                raise SystemExit(0 if stopping else code)
        time.sleep(0.25)


if __name__ == "__main__":
    main()
