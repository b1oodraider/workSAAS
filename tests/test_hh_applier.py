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
    "/vacancy/6": "vacancy_external.html",
    "/vacancy/7": "vacancy_single.html",
    "/vacancy/8": "vacancy_nosuccess.html",
    "/vacancy/9": "vacancy_closed.html",
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
    hh = HHBrowserApplier(base_url=mock_site)
    hh.success_timeout_ms = 1500
    hh.save_session(user_id, {"cookies": [{"name": "hhtoken", "value": "x", "domain": ".hh.ru", "path": "/"}],
                              "origins": []})
    return hh


def req(user_id, vid, **kw):
    return ApplyRequest(user_id=user_id, source="hh", external_id=str(vid), url=None,
                        letter=kw.get("letter", "Здравствуйте! Письмо."),
                        site_resume_title=kw.get("title", "python backend"))


async def test_applies_with_chosen_resume_and_letter(applier, user_id, monkeypatch):
    seen = {}
    real = applier._flow

    async def spy(page, url, r, sel):
        result = await real(page, url, r, sel)
        seen["typed"] = await page.locator(sel.success).first.inner_text()
        return result

    monkeypatch.setattr(applier, "_flow", spy)
    result = await applier.apply(req(user_id, 1))
    assert result.status == "applied", result.reason
    assert seen["typed"] == str(len("Здравствуйте! Письмо."))  # the letter really went into the form


async def test_wrong_resume_title_fails(applier, user_id):
    result = await applier.apply(req(user_id, 1, title="дизайнер"))
    assert result.status == "failed" and "дизайнер" in result.reason


async def test_single_resume_needs_no_choice(applier, user_id):
    result = await applier.apply(req(user_id, 7, title="что угодно"))
    assert result.status == "applied", result.reason


@pytest.mark.parametrize("vid,status,word", [
    (2, "skipped", "уже откликались"),
    (3, "skipped", "вопросы"),
    (6, "skipped", "своём сайте"),
    (9, "skipped", "нет кнопки"),
    (4, "blocked", "не робот"),
    (5, "blocked", "войдите заново"),
])
async def test_decisions(applier, user_id, vid, status, word):
    result = await applier.apply(req(user_id, vid))
    assert result.status == status and word in result.reason


async def test_unconfirmed_submit_may_have_been_sent(applier, user_id):
    result = await applier.apply(req(user_id, 8))
    assert result.status == "failed" and result.maybe_sent and "Отклики" in result.reason


async def test_login_page_marks_session_stale_until_new_login(applier, user_id):
    assert (await applier.apply(req(user_id, 5))).status == "blocked"
    ok, hint = applier.is_ready(user_id)
    assert not ok and "войдите заново" in hint
    result = await applier.apply(req(user_id, 1))
    assert result.status == "blocked"  # no browser run with a session hh.ru already rejected
    applier.save_session(user_id, {"cookies": [{"name": "hhtoken", "value": "y", "domain": ".hh.ru"}]})
    assert applier.is_ready(user_id)[0]


async def test_non_numeric_vacancy_id_never_opens_the_browser(applier, user_id):
    result = await applier.apply(req(user_id, "../applicant/settings"))
    assert result.status == "skipped"


async def test_browser_error_is_transient(applier, user_id, env):
    env.apply.browser_executable = "/nonexistent/chrome"
    result = await applier.apply(req(user_id, 1))
    assert result.status == "failed" and result.transient


async def test_missing_session_is_blocked(env, tmp_path, user_id, mock_site):
    env.data_dir = str(tmp_path / "empty")
    env.apply.browser_executable = CHROMIUM
    result = await HHBrowserApplier(base_url=mock_site).apply(req(user_id, 1))
    assert result.status == "blocked" and "войдите" in result.reason


def test_session_file_is_filtered_and_private(env, tmp_path, user_id):
    env.data_dir = str(tmp_path)
    hh = HHBrowserApplier()
    hh.save_session(user_id, {"cookies": [
        {"name": "a", "value": "1", "domain": ".hh.ru"},
        {"name": "evil", "value": "2", "domain": ".google.com"}], "origins": [
        {"origin": "https://hh.ru", "localStorage": []}, {"origin": "https://spb.hh.ru", "localStorage": []},
        {"origin": "https://hh.ru.evil.com", "localStorage": []},
        {"origin": "https://evil.com//hh.ru", "localStorage": []}]})
    state = hh.load_session(user_id)
    assert [c["name"] for c in state["cookies"]] == ["a"]
    assert [o["origin"] for o in state["origins"]] == ["https://hh.ru", "https://spb.hh.ru"]
    path = sessions.session_path("hh", user_id)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(path.parent).st_mode & 0o777) == "0o700"
    assert list(path.parent.glob("*.tmp")) == []
    from app.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        hh.save_session_bytes(user_id, b'{"cookies": [{"domain": "evil.com"}]}')
