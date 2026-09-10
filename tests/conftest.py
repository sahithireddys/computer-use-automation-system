"""
Shared pytest fixtures: a live mock_app instance (real Flask dev server,
real HTTP, real Playwright browser against it -- not mocked Playwright
objects) on a dedicated test port so it never collides with a manually
running `python -m mock_app.app` on 5055, plus a session-scoped browser
and function-scoped page.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
from playwright.sync_api import sync_playwright

from mock_app.app import app as flask_app

TEST_PORT = 5099
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def base_url() -> str:
    if not _port_open(TEST_PORT):
        thread = threading.Thread(
            target=lambda: flask_app.run(port=TEST_PORT, debug=False, use_reloader=False),
            daemon=True,
        )
        thread.start()
        for _ in range(50):
            if _port_open(TEST_PORT):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"mock_app did not start on port {TEST_PORT}")
    return BASE_URL


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser, base_url):
    pg = browser.new_page()
    yield pg
    pg.close()


@pytest.fixture()
def isolated_evidence_root(tmp_path, monkeypatch):
    """
    Redirects EvidenceLogger's output to a throwaway tmp dir so running the
    test suite doesn't pollute the real /evidence/runs/ directory (which is
    reserved for the curated discovery/replay demonstrations in
    HANDOFF.md's evidence plan).
    """
    import evidence.logger as logger_module
    monkeypatch.setattr(logger_module, "EVIDENCE_ROOT", tmp_path)
    return tmp_path
