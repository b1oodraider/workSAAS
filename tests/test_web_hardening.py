"""Web/queue hardening: commit before response, retry backoff, queue priority, shutdown,
login throttling, scrypt concurrency, Secure cookie, CSRF origin check, CLI process setup."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import security
from app.core.db import session_scope, utcnow
from app.jobs import queue
from app.jobs.queue import JobContext, Worker, enqueue, job_handler, retry_delay
from app.llm.base import LLMUnavailable
from app.models import Job, JobStatus, Resume, User
from app.services import resumes as resume_svc
from app.web.deps import db
from app.web.routes.auth import (
    GLOBAL_MAX_FAILURES,
    KNOWN_DEVICE_COOKIE,
    KNOWN_DEVICE_MAX_USERS,
    MAX_FAILURES,
    USER_MAX_FAILURES,
    LoginThrottle,
    known_device_users,
    throttle,
)

from .conftest import RESUME_TEXT

LOGIN = {"username": "alice", "password": "password123"}


def _app(**kwargs):
    from app.main import create_app

    return create_app(start_background=False, **kwargs)


@pytest.fixture(autouse=True)
def _fresh_throttle():
    throttle.clear()
    yield
    throttle.clear()


# --------------------------------------------------------------------------- commit before response


def _walk(dependant):
    for dep in dependant.dependencies:
        yield dep
        yield from _walk(dep)


def _api_routes(routes):
    """Every APIRoute, also inside included routers (FastAPI 0.13x+ keeps them as wrappers)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(getattr(route, "original_router", None), "routes", None) or getattr(route, "routes", None)
        if inner:
            yield from _api_routes(inner)


def test_every_route_commits_the_session_before_responding():
    app = _app()
    routes = list(_api_routes(app.routes))
    assert len(routes) > 30  # the guard really sees the app's routes
    offenders = [f"{sorted(route.methods)} {route.path}" for route in routes
                 for dep in _walk(route.dependant) if dep.call is db and dep.scope != "function"]
    assert offenders == [], 'declare the session as Depends(db, scope="function")'


def test_failed_commit_is_an_error_not_a_redirect(user_id, monkeypatch):
    with session_scope() as s:
        resume_id = resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT).id
    with TestClient(_app(), raise_server_exceptions=False) as client:
        client.post("/login", data=LOGIN)

        def broken_commit(self):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(Session, "commit", broken_commit)
        r = client.post(f"/resumes/{resume_id}/edit", data={"title": "New", "text": RESUME_TEXT},
                        follow_redirects=False)
        monkeypatch.undo()
    assert r.status_code == 500
    with session_scope() as s:
        assert s.get(Resume, resume_id).title == "CV"


# --------------------------------------------------------------------------- job queue


_hold: dict[str, asyncio.Event] = {}


@job_handler("test_down")
async def _down(ctx: JobContext) -> dict:
    raise LLMUnavailable("provider down")


@job_handler("test_block")
async def _block(ctx: JobContext) -> dict:
    _hold["started"].set()
    await _hold["gate"].wait()
    return {}


@job_handler("test_quick")
async def _quick(ctx: JobContext) -> dict:
    return {}


@job_handler("test_hang")
async def _hang(ctx: JobContext) -> dict:
    await asyncio.sleep(3600)
    return {}


def test_retry_delay_grows_and_is_capped():
    assert [retry_delay(n).total_seconds() for n in (1, 2, 3)] == [30, 60, 120]
    assert retry_delay(20) == timedelta(minutes=10)


async def test_transient_failure_waits_before_retry(env, user_id):
    env.jobs.max_attempts = 3
    job_id = enqueue("test_down", {}, user_id=user_id)
    await queue.run_job(queue._claim_next())
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.queued and job.attempts == 1
        wait = (job.run_after - utcnow()).total_seconds()
    assert 20 < wait <= 30
    assert queue._claim_next() is None  # not before the backoff is over
    with session_scope() as s:
        s.get(Job, job_id).run_after = utcnow() - timedelta(seconds=1)
    claimed = queue._claim_next()
    assert claimed is not None and claimed.id == job_id and claimed.attempts == 2
    with session_scope() as s:
        assert s.get(Job, job_id).run_after is None


def test_bulk_jobs_are_claimed_after_interactive_ones(user_id):
    with session_scope() as s:
        parent = Job(kind="search_run", user_id=user_id, status=JobStatus.done)
        s.add(parent)
        s.flush()
        jobs = [
            Job(kind="match_triage", user_id=user_id, payload={}),
            Job(kind="analysis", user_id=user_id, payload={}, parent_id=parent.id),  # from a search run
            Job(kind="analysis", user_id=user_id, payload={}),  # the user asked for it
            Job(kind="autoapply_send", user_id=user_id, payload={}),
        ]
        s.add_all(jobs)
        s.flush()
        triage, child, own, send = (j.id for j in jobs)
    order = [queue._claim_next().id for _ in range(4)]
    assert order == [own, send, triage, child]


async def test_worker_claims_nothing_after_stop(user_id):
    _hold["started"], _hold["gate"] = asyncio.Event(), asyncio.Event()
    first = enqueue("test_block", {}, user_id=user_id)
    second = enqueue("test_quick", {}, user_id=user_id)
    worker = Worker(concurrency=1, poll_interval=0.01)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(_hold["started"].wait(), 5)
    await asyncio.sleep(0.05)  # the loop now waits for the only slot
    worker.stop()
    _hold["gate"].set()
    await asyncio.wait_for(task, 5)
    with session_scope() as s:
        assert s.get(Job, first).status == JobStatus.done
        assert s.get(Job, second).status == JobStatus.queued


def test_shutdown_does_not_wait_forever_for_running_jobs(user_id, monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "SHUTDOWN_TIMEOUT_S", 0.3)
    job_id = enqueue("test_hang", {}, user_id=user_id)
    with TestClient(main_mod.create_app(start_background=True)):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with session_scope() as s:
                if s.get(Job, job_id).status == JobStatus.running:
                    break
            time.sleep(0.05)
        started = time.monotonic()
    assert time.monotonic() - started < 5
    with session_scope() as s:  # cancelled mid-run: requeued by recover_stale_jobs on the next start
        assert s.get(Job, job_id).status == JobStatus.running


# --------------------------------------------------------------------------- login


def test_username_is_locked_across_ips():
    t = LoginThrottle()
    for i in range(USER_MAX_FAILURES):
        assert t.blocked(f"10.0.0.{i}", "alice") is None
        t.fail(f"10.0.0.{i}", "alice")
    assert "15 минут" in t.blocked("10.9.9.9", "alice")
    assert t.blocked("10.9.9.9", "bob") is None


def test_global_cap_on_failed_logins():
    t = LoginThrottle()
    for i in range(GLOBAL_MAX_FAILURES):
        t.fail(f"10.0.{i}.1", f"user{i}")
    assert "минуту" in t.blocked("192.168.1.1", "someone")


def test_success_does_not_reset_the_username_counter():
    t = LoginThrottle()
    for i in range(USER_MAX_FAILURES - 1):
        t.fail(f"10.0.0.{i}", "alice")
    t.success("127.0.0.1", "alice")
    t.fail("10.0.0.200", "alice")
    assert t.blocked("127.0.0.1", "alice")


def test_per_username_lock_over_http(user_id):
    for i in range(USER_MAX_FAILURES):
        throttle.fail(f"10.0.0.{i}", "alice")
    with TestClient(_app()) as client:
        r = client.post("/login", data=LOGIN)
        assert "Слишком много попыток" in r.text
        assert client.get("/resumes", follow_redirects=False).status_code == 303  # not logged in


def _lock_out_alice_and_everybody():
    """An attacker fails on purpose from many IPs: the per-username and the global windows fill up."""
    for i in range(max(USER_MAX_FAILURES, GLOBAL_MAX_FAILURES)):
        throttle.fail(f"10.0.{i}.1", "alice")


def test_known_device_logs_in_although_an_attacker_locked_the_username(user_id):
    with TestClient(_app()) as admin:
        assert admin.post("/login", data=LOGIN, follow_redirects=False).headers["location"] == "/"
        assert admin.cookies.get(KNOWN_DEVICE_COOKIE)
        admin.post("/logout")
        _lock_out_alice_and_everybody()
        r = admin.post("/login", data=LOGIN, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert admin.get("/resumes", follow_redirects=False).status_code == 200
    with TestClient(_app()) as stranger:  # the same correct password, but no known-device cookie
        r = stranger.post("/login", data=LOGIN)
        assert "Слишком много попыток" in r.text
        assert stranger.get("/resumes", follow_redirects=False).status_code == 303


def test_known_device_cookie_is_per_username_and_signed(user_id):
    with session_scope() as s:
        s.add(User(username="bob", password_hash=security.hash_password("password456")))
    with TestClient(_app()) as client:
        client.post("/login", data={"username": "bob", "password": "password456"})
        client.post("/logout")
        _lock_out_alice_and_everybody()
        assert "Слишком много попыток" in client.post("/login", data=LOGIN).text  # bob's device, not alice's
    with TestClient(_app()) as client:
        client.cookies.set(KNOWN_DEVICE_COOKIE, '["alice"]')  # forged: not signed with the secret key
        assert "Слишком много попыток" in client.post("/login", data=LOGIN).text


def test_known_device_still_limited_per_ip(user_id):
    with TestClient(_app()) as client:
        client.post("/login", data=LOGIN)
        client.post("/logout")
        for _ in range(MAX_FAILURES):
            client.post("/login", data={"username": "alice", "password": "wrong"})
        assert "5 минут" in client.post("/login", data=LOGIN).text


def test_known_device_cookie_flags(env, user_id):
    def cookie(client) -> str:
        r = client.post("/login", data=LOGIN, follow_redirects=False)
        return next(v for k, v in r.headers.multi_items()
                    if k == "set-cookie" and v.startswith(f"{KNOWN_DEVICE_COOKIE}=")).lower()

    with TestClient(_app()) as client:
        value = cookie(client)
        assert "httponly" in value and "samesite=lax" in value and "secure" not in value
    env.telegram.public_url = "https://jobs.example.com"
    with TestClient(_app()) as client:
        assert "secure" in cookie(client)


def test_known_device_cookie_keeps_a_few_usernames(user_id):
    with session_scope() as s:
        s.add_all([User(username=f"u{i}", password_hash=security.hash_password("password456"))
                   for i in range(KNOWN_DEVICE_MAX_USERS + 2)])
    with TestClient(_app()) as client:
        for i in range(KNOWN_DEVICE_MAX_USERS + 2):
            client.post("/login", data={"username": f"u{i}", "password": "password456"})
        request = SimpleNamespace(cookies=dict(client.cookies), app=client.app)
        users = known_device_users(request)
    assert users[0] == f"u{KNOWN_DEVICE_MAX_USERS + 1}" and len(users) == KNOWN_DEVICE_MAX_USERS


def test_scrypt_runs_at_most_four_at_once(monkeypatch):
    active, peak, lock = [0], [0], threading.Lock()

    def slow_scrypt(*args, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return b"x" * kwargs["dklen"]

    stored = security.hash_password("secret")  # real hash, before patching
    monkeypatch.setattr(security.hashlib, "scrypt", slow_scrypt)
    threads = [threading.Thread(target=security.verify_password, args=("bad", stored)) for _ in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert 1 <= peak[0] <= 4


def test_run_trusts_forwarded_headers_only_from_configured_proxies(env, monkeypatch):
    import uvicorn

    from app import cli

    env.trusted_proxies = "172.17.0.1"
    calls = []
    monkeypatch.setattr(cli, "_migrate", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    cli.cmd_run(SimpleNamespace(host="0.0.0.0", port=8000))
    assert calls[0]["proxy_headers"] is True and calls[0]["forwarded_allow_ips"] == "172.17.0.1"


# --------------------------------------------------------------------------- cookie, openapi


def _session_cookie(client) -> str:
    r = client.post("/login", data=LOGIN, follow_redirects=False)
    return next(v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("session="))


def test_session_cookie_is_secure_when_public_url_is_https(env, user_id):
    with TestClient(_app()) as client:
        assert "secure" not in _session_cookie(client).lower()
    env.telegram.public_url = "https://jobs.example.com"
    with TestClient(_app()) as client:
        assert "secure" in _session_cookie(client).lower()
    env.telegram.public_url = "http://127.0.0.1:8000"
    env.session_https_only = True  # explicit override still works
    with TestClient(_app()) as client:
        assert "secure" in _session_cookie(client).lower()


def test_openapi_schema_is_not_public():
    with TestClient(_app()) as client:
        assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------------------- CSRF


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "http://testserver.evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
])
def test_cross_site_posts_are_refused(user_id, headers):
    with TestClient(_app()) as client:
        r = client.post("/login", data=LOGIN, headers=headers, follow_redirects=False)
        assert r.status_code == 403
        assert client.get("/resumes", follow_redirects=False).status_code == 303  # no session


@pytest.mark.parametrize("headers", [
    {},  # curl, old browsers
    {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://testserver:80"},
    {"Origin": "https://jobs.example.com"},  # public_url: behind nginx Host is the upstream address
    {"Origin": "https://proxy.example", "X-Forwarded-Host": "proxy.example"},
])
def test_same_site_posts_pass(env, user_id, headers):
    env.telegram.public_url = "https://jobs.example.com"
    with TestClient(_app()) as client:
        r = client.post("/login", data=LOGIN, headers=headers, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


def test_refused_post_is_logged_with_its_headers(caplog):
    with TestClient(_app()) as client, caplog.at_level("WARNING", logger="app.main"):
        client.post("/login", data=LOGIN, headers={"Origin": "https://evil.example", "X-Forwarded-Host": "x.example"})
    [record] = [r for r in caplog.records if r.name == "app.main"]
    assert all(part in record.getMessage() for part in ("POST", "/login", "evil.example", "testserver", "x.example"))


def test_cross_site_get_is_not_blocked():
    with TestClient(_app()) as client:
        r = client.get("/login", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- CLI


def test_cli_restricts_file_permissions_on_posix(monkeypatch):
    from app import cli

    umask = []
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="posix", umask=umask.append))
    with pytest.raises(SystemExit):
        cli.main(["no-such-command"])
    assert umask == [0o077]
