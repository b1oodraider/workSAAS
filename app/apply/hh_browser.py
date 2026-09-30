"""hh.ru applications through a real (headless) browser with the user's saved login session.

hh.ru's applicant API needs an OAuth app key, so the applier does what the user would do:
open the vacancy, press «Откликнуться», pick the resume, paste the cover letter, submit.
Selectors live in config ([apply.hh_selectors]) because hh.ru markup changes; they were
written from the public markup and not verified against the live site during development.

Safety: one application per call, the caller enforces rate limits; anything that looks
like a captcha, a login page or an account restriction returns "blocked", which pauses
auto-apply for the user until they look at it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.apply import sessions
from app.apply.base import Applier, ApplyRequest, ApplyResult
from app.core.config import get_settings

log = logging.getLogger(__name__)

SITE = "https://hh.ru"
# Vacancies with a mandatory questionnaire redirect here instead of opening the popup.
QUESTIONNAIRE_MARKERS = ("/applicant/vacancy_response", "vacancy_response?vacancyId")
LOGIN_MARKERS = ("/account/login", "/account/signup")


class HHBrowserApplier(Applier):
    source = "hh"
    title = "hh.ru"

    def __init__(self, base_url: str = SITE) -> None:
        self.base_url = base_url.rstrip("/")

    def is_ready(self, user_id: int) -> tuple[bool, str]:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False, 'нужен браузерный режим: pip install -e ".[browser]" && playwright install chromium'
        if not sessions.has_session("hh", user_id):
            return False, "войдите в hh.ru: worksaas hh-login <логин> или загрузите файл сессии на странице автооткликов"
        return True, ""

    async def apply(self, req: ApplyRequest) -> ApplyResult:
        ready, hint = self.is_ready(req.user_id)
        if not ready:
            return ApplyResult("blocked", hint)
        state = sessions.load_session("hh", req.user_id)
        if state is None:
            return ApplyResult("blocked", "файл сессии hh.ru повреждён — войдите заново")

        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        cfg = get_settings().apply
        url = f"{self.base_url}/vacancy/{req.external_id}"
        try:
            async with async_playwright() as pw:
                # No background calls to Google services from the automation browser.
                launch: dict[str, Any] = {"headless": cfg.headless, "args": [
                    "--disable-background-networking", "--disable-component-update", "--no-first-run",
                    "--disable-sync", "--metrics-recording-only", "--disable-default-apps"]}
                if cfg.browser_executable:
                    launch["executable_path"] = cfg.browser_executable
                browser = await pw.chromium.launch(**launch)
                try:
                    context = await browser.new_context(storage_state=state, locale="ru-RU",
                                                        viewport={"width": 1366, "height": 900})
                    page = await context.new_page()
                    result = await self._flow(page, url, req, cfg.hh_selectors)
                    # hh.ru refreshes cookies; keep the session alive for the next run.
                    if result.status in ("applied", "skipped"):
                        sessions.save_session("hh", req.user_id, await context.storage_state())
                    return result
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            log.warning("hh applier browser error: %s", exc)
            return ApplyResult("failed", f"ошибка браузера: {str(exc).splitlines()[0][:200]}")

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

        if any(m in page.url for m in QUESTIONNAIRE_MARKERS):
            return ApplyResult("skipped", "работодатель требует ответить на вопросы — откликнитесь вручную")
        blocked = await self._blocked(page, sel)
        if blocked:
            return ApplyResult("blocked", blocked)

        relocation = page.locator(sel.relocation_confirm).first
        if await relocation.count():
            await relocation.click()
            await page.wait_for_timeout(1000)

        if req.site_resume_title:
            options = page.locator(sel.resume_option)
            chosen = False
            for i in range(await options.count()):
                option = options.nth(i)
                if req.site_resume_title.lower() in (await option.inner_text()).lower():
                    await option.click()
                    chosen = True
                    break
            if not chosen and await options.count() > 1:
                return ApplyResult("failed", f"не нашёл на hh.ru резюме с названием «{req.site_resume_title}»")

        letter_input = page.locator(sel.letter_input).first
        if req.letter.strip():
            # The letter field often exists but stays hidden until «Сопроводительное письмо» is pressed.
            if not (await letter_input.count() and await letter_input.is_visible()):
                toggle = page.locator(sel.letter_toggle).first
                if await toggle.count():
                    await toggle.click()
                    await page.wait_for_timeout(500)
            if not (await letter_input.count() and await letter_input.is_visible()):
                return ApplyResult("failed", "не нашёл поле для сопроводительного письма")
            await letter_input.fill(req.letter.strip(), timeout=10_000)

        submit = page.locator(sel.submit).first
        if not await submit.count():
            return ApplyResult("failed", "не нашёл кнопку отправки отклика")
        await submit.click()
        try:
            await page.locator(sel.success).first.wait_for(timeout=15_000)
        except Exception:  # noqa: BLE001 - playwright TimeoutError
            blocked = await self._blocked(page, sel)
            if blocked:
                return ApplyResult("blocked", blocked)
            return ApplyResult("failed", "hh.ru не подтвердил отправку отклика")
        return ApplyResult("applied", "отклик отправлен")

    async def _blocked(self, page: Any, sel: Any) -> str:
        url = page.url.lower()
        if "captcha" in url:
            return "hh.ru показал капчу — откройте hh.ru в браузере, пройдите её и снимите паузу"
        if any(m in url for m in LOGIN_MARKERS) or await page.locator(sel.login_form).count():
            return "сессия hh.ru истекла — войдите заново (worksaas hh-login)"
        if await page.locator("iframe[src*='captcha'], [data-qa*='captcha'], form[action*='captcha']").count():
            return "hh.ru показал капчу — откройте hh.ru в браузере, пройдите её и снимите паузу"
        return ""
