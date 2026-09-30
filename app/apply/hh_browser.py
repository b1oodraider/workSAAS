"""hh.ru applications through a real (headless) browser with the user's saved login session.

hh.ru's applicant API needs an OAuth app key, so the applier does what the user would do:
open the vacancy, press «Откликнуться», pick the resume, paste the cover letter, submit.
Selectors live in config ([apply.hh_selectors]) because hh.ru markup changes; they were
written from the public markup and not verified against the live site during development.

Safety: one application per call, the caller enforces rate limits; anything that looks
like a captcha, a login page or an account restriction returns "blocked", which pauses
auto-apply for the user until they look at it. The browser never follows the employer
off hh.ru: external application forms are left to the user.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.apply import sessions
from app.apply.base import Applier, ApplyRequest, ApplyResult
from app.core.config import get_settings
from app.core.http import chromium_launch_options

log = logging.getLogger(__name__)

SITE = "https://hh.ru"
# Vacancies with a mandatory questionnaire redirect here instead of opening the popup.
QUESTIONNAIRE_MARKERS = ("/applicant/vacancy_response", "vacancy_response?vacancyId")
LOGIN_MARKERS = ("/account/login", "/account/signup")
CAPTCHA_ELEMENTS = "iframe[src*='captcha'], [data-qa*='captcha'], form[action*='captcha']"
CAPTCHA_REASON = ("hh.ru попросил пройти проверку «я не робот». Зайдите на hh.ru в обычном браузере, "
                  "а через пару часов нажмите «Продолжить». Если повторяется — уменьшите число откликов в день")
LOGIN_REASON = "вход в hh.ru истёк — войдите заново (раздел «Вход в hh.ru» на странице автооткликов)"


class HHBrowserApplier(Applier):
    source = "hh"
    title = "hh.ru"
    login_url = "https://hh.ru/account/login"
    account_url = "https://hh.ru/applicant/resumes"
    session_domains = ("hh.ru", "hh.kz", "hh.uz")
    success_timeout_ms = 15_000

    def __init__(self, base_url: str = SITE) -> None:
        self.base_url = base_url.rstrip("/")
        base_host = urlsplit(self.base_url).hostname or ""
        self._allowed_hosts = self.session_domains + ((base_host,) if base_host else ())

    # A saved session that hh.ru rejected is kept but marked, so the page asks to log in again.

    def _stale_marker(self, user_id: int) -> Path:
        return sessions.session_path(self.source, user_id).with_suffix(".stale")

    def save_session(self, user_id: int, raw: Any) -> None:
        super().save_session(user_id, raw)
        self._stale_marker(user_id).unlink(missing_ok=True)

    def delete_session(self, user_id: int) -> None:
        super().delete_session(user_id)
        self._stale_marker(user_id).unlink(missing_ok=True)

    def is_ready(self, user_id: int) -> tuple[bool, str]:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False, "автоотклики не настроены на сервере (не установлен браузер) — напишите администратору"
        if not self.has_session(user_id):
            return False, "войдите в hh.ru — инструкция ниже"
        if self._stale_marker(user_id).exists():
            return False, "hh.ru перестал принимать сохранённый вход — войдите заново"
        return True, ""

    def _on_site(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self._allowed_hosts)

    async def apply(self, req: ApplyRequest) -> ApplyResult:
        if not req.external_id.isdigit():
            return ApplyResult("skipped", "у вакансии нет номера hh.ru — откликнитесь вручную")
        ready, hint = self.is_ready(req.user_id)
        if not ready:
            return ApplyResult("blocked", hint)
        state = self.load_session(req.user_id)
        if state is None:
            return ApplyResult("blocked", "сохранённый вход в hh.ru повреждён — войдите заново")

        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        cfg = get_settings().apply
        url = f"{self.base_url}/vacancy/{req.external_id}"
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**chromium_launch_options(
                    headless=cfg.headless, use_proxy=cfg.use_proxy, executable=cfg.browser_executable))
                try:
                    # Headless Chromium announces itself as "HeadlessChrome"; look like the normal one.
                    ua = (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          f"Chrome/{browser.version} Safari/537.36")
                    context = await browser.new_context(storage_state=state, locale="ru-RU", user_agent=ua,
                                                        viewport={"width": 1366, "height": 900})
                    page = await context.new_page()
                    result = await self._flow(page, url, req, cfg.hh_selectors)
                    if result.status == "blocked" and result.reason == LOGIN_REASON:
                        self._stale_marker(req.user_id).touch()
                    if result.status in ("applied", "skipped"):
                        await self._refresh_session(req.user_id, context)
                    return result
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            first = (str(exc).splitlines() or [type(exc).__name__])[0]
            log.warning("hh applier browser error: %s", first[:300])
            return ApplyResult("failed", "hh.ru не открылся или страница не загрузилась — попробую позже",
                               transient=True)

    async def _refresh_session(self, user_id: int, context: Any) -> None:
        """hh.ru rotates cookies: keep the session alive, unless the user deleted it meanwhile."""
        if not self.has_session(user_id):
            return
        try:
            self.save_session(user_id, await context.storage_state())
        except Exception as exc:  # noqa: BLE001 - the application itself already went through
            log.warning("could not refresh hh.ru session for user %s: %s", user_id, type(exc).__name__)

    async def _flow(self, page: Any, url: str, req: ApplyRequest, sel: Any) -> ApplyResult:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(1500)
        blocked = await self._blocked(page, sel)
        if blocked:
            return ApplyResult("blocked", blocked)
        if await page.locator(sel.already_applied).count():
            return ApplyResult("skipped", "вы уже откликались на эту вакансию")

        button = page.locator(sel.response_button).first
        if not await button.count():
            return ApplyResult("skipped", "на странице нет кнопки отклика (вакансия закрыта или в архиве)")
        await button.click()
        await page.wait_for_timeout(2000)

        if not self._on_site(page.url):
            return ApplyResult("skipped", "работодатель принимает отклики на своём сайте — откликнитесь вручную")
        if any(m in page.url for m in QUESTIONNAIRE_MARKERS):
            return ApplyResult("skipped", "работодатель просит ответить на вопросы — откликнитесь вручную")
        blocked = await self._blocked(page, sel)
        if blocked:
            return ApplyResult("blocked", blocked)

        relocation = page.locator(sel.relocation_confirm).first
        if await relocation.count():
            await relocation.click()
            await page.wait_for_timeout(1000)

        if req.site_resume_title:
            options = page.locator(sel.resume_option)
            count = await options.count()
            chosen = False
            for i in range(count):
                option = options.nth(i)
                if req.site_resume_title.lower() in (await option.inner_text()).lower():
                    await option.click()
                    chosen = True
                    break
            # No list at all = hh.ru has a single resume and doesn't ask; otherwise the title must match.
            if count and not chosen:
                return ApplyResult("failed", f"не нашёл на hh.ru резюме с названием «{req.site_resume_title}» — "
                                             "проверьте поле «Какое резюме отправлять» в настройках")

        letter_input = page.locator(sel.letter_input).first
        if req.letter.strip():
            # The letter field often exists but stays hidden until «Сопроводительное письмо» is pressed.
            if not (await letter_input.count() and await letter_input.is_visible()):
                toggle = page.locator(sel.letter_toggle).first
                if await toggle.count():
                    await toggle.click()
                    await page.wait_for_timeout(500)
            if not (await letter_input.count() and await letter_input.is_visible()):
                return ApplyResult("failed", "форма отклика на hh.ru выглядит не так, как ожидалось "
                                             "(нет поля для письма) — откликнитесь вручную")
            await letter_input.fill(req.letter.strip(), timeout=10_000)

        submit = page.locator(sel.submit).first
        if not await submit.count():
            return ApplyResult("failed", "форма отклика на hh.ru выглядит не так, как ожидалось "
                                         "(нет кнопки отправки) — откликнитесь вручную")
        await submit.click()
        try:
            await page.locator(sel.success).first.wait_for(timeout=self.success_timeout_ms)
        except Exception:  # noqa: BLE001 - playwright TimeoutError
            blocked = await self._blocked(page, sel)
            if blocked:
                return ApplyResult("blocked", blocked, maybe_sent=True)
            return ApplyResult("failed", "не удалось убедиться, что отклик ушёл — проверьте раздел «Отклики» "
                                         "на hh.ru", maybe_sent=True)
        return ApplyResult("applied", "отклик отправлен")

    async def _blocked(self, page: Any, sel: Any) -> str:
        url = page.url.lower()
        if "captcha" in url:
            return CAPTCHA_REASON
        if any(m in url for m in LOGIN_MARKERS) or await page.locator(sel.login_form).count():
            return LOGIN_REASON
        if await page.locator(CAPTCHA_ELEMENTS).count():
            return CAPTCHA_REASON
        return ""
