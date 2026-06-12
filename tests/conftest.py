from __future__ import annotations

import os
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
    os.environ["POKEWALLET_BASE_URL"] = base
    os.environ["POKEWALLET_API_KEY"] = "test-key"
    yield base
    server.should_exit = True


@pytest.fixture()
def client(pokewallet_stub: str):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
