from __future__ import annotations

import os

# The scanner tests import app.main, which now imports the DB/auth layer; that layer
# validates DATABASE_URL + AUTH_SECRET at import time. These placeholders let the
# scanner suite import the app without a live DB (no scanner test touches /pulls or
# the DB). Set before any app import. AUTH_SECRET must be >= 32 bytes (HS256 guard).
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("AUTH_SECRET", "test-placeholder-secret-not-used-0123456789")

import socket
import threading
import time

import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pokewallet_stub() -> str:
    from tests.pokewallet_stub import app as stub_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(stub_app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("pokewallet stub failed to start")
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    _prev_base = os.environ.get("POKEWALLET_BASE_URL")
    _prev_key = os.environ.get("POKEWALLET_API_KEY")
    os.environ["POKEWALLET_BASE_URL"] = base
    os.environ["POKEWALLET_API_KEY"] = "test-key"
    yield base
    server.should_exit = True
    if _prev_base is None:
        os.environ.pop("POKEWALLET_BASE_URL", None)
    else:
        os.environ["POKEWALLET_BASE_URL"] = _prev_base
    if _prev_key is None:
        os.environ.pop("POKEWALLET_API_KEY", None)
    else:
        os.environ["POKEWALLET_API_KEY"] = _prev_key


@pytest.fixture()
def client(pokewallet_stub: str):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
