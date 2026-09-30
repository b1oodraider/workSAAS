"""Browser applier against a local mock of hh.ru pages (real headless Chromium).

The mock follows hh.ru's data-qa markup as known at the time of writing; it verifies the
click flow and the decisions (applied / skipped / blocked), not the live site.
Skipped when Playwright or a Chromium binary is not available.
"""

from __future__ import annotations

import glob
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.apply import sessions
from app.apply.base import ApplyRequest
from app.apply.hh_browser import HHBrowserApplier

FIXTURES = Path(__file__).parent / "fixtures" / "hh_apply"
ROUTES = {
    "/vacancy/1": "vacancy_ok.html",
    "/vacancy/2": "vacancy_applied.html",
    "/vacancy/3": "vacancy_questionnaire.html",
    "/vacancy/4": "vacancy_captcha.html",
    "/vacancy/5": "vacancy_login.html",
}


def _chromium() -> str | None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return None
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return found[-1] if found else os.environ.get("WS_TEST_CHROMIUM")


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="no Playwright/Chromium available")


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        name = ROUTES.get(path) or ("vacancy_response.html" if path.startswith("/applicant/") else None)
        if name is None:
            self.send_error(404)
            return
        body = (FIXTURES / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def mock_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def applier(env, tmp_path, mock_site, user_id):
    env.data_dir = str(tmp_path)
    env.apply.browser_executable = CHROMIUM
    sessions.save_session("hh", user_id, {"cookies": [
        {"name": "hhtoken", "value": "x", "domain": ".hh.ru", "path": "/"}], "origins": []})
    return HHBrowserApplier(base_url=mock_site)


def req(user_id, vid, **kw):
    return ApplyRequest(user_id=user_id, source="hh", external_id=str(vid), url=None,
                        letter=kw.get("letter", "Здравствуйте! Письмо."),
                        site_resume_title=kw.get("title", "python backend"))


async def test_applies_with_chosen_resume_and_letter(applier, user_id):
    result = await applier.apply(req(user_id, 1))
    assert result.status == "applied", result.reason


async def test_wrong_resume_title_fails(applier, user_id):
    result = await applier.apply(req(user_id, 1, title="дизайнер"))
    assert result.status == "failed" and "дизайнер" in result.reason


@pytest.mark.parametrize("vid,status,word", [
    (2, "skipped", "уже откликались"),
    (3, "skipped", "вопросы"),
    (4, "blocked", "капчу"),
    (5, "blocked", "войдите"),
])
async def test_decisions(applier, user_id, vid, status, word):
    result = await applier.apply(req(user_id, vid))
    assert result.status == status and word in result.reason


async def test_missing_session_is_blocked(env, tmp_path, user_id, mock_site):
    env.data_dir = str(tmp_path / "empty")
    env.apply.browser_executable = CHROMIUM
    result = await HHBrowserApplier(base_url=mock_site).apply(req(user_id, 1))
    assert result.status == "blocked" and "hh-login" in result.reason


def test_session_file_is_filtered_and_private(env, tmp_path, user_id):
    env.data_dir = str(tmp_path)
    sessions.save_session("hh", user_id, {"cookies": [
        {"name": "a", "value": "1", "domain": ".hh.ru"},
        {"name": "evil", "value": "2", "domain": ".google.com"}], "origins": [
        {"origin": "https://hh.ru", "localStorage": []}, {"origin": "https://evil.com", "localStorage": []}]})
    state = sessions.load_session("hh", user_id)
    assert [c["name"] for c in state["cookies"]] == ["a"] and len(state["origins"]) == 1
    assert oct(os.stat(sessions.session_path("hh", user_id)).st_mode & 0o777) == "0o600"
    from app.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        sessions.save_session_bytes("hh", user_id, b'{"cookies": [{"domain": "evil.com"}]}')
