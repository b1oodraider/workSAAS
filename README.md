# workSAAS

Self-hosted помощник по поиску работы для себя и нескольких друзей:

- **Оценка резюме** — разбор глазами рекрутера: оценка, проблемы с конкретными исправлениями,
  недостающие ключевые слова, переписанный блок «О себе».
- **Оценка вакансий** — красные/зелёные флаги с цитатами, реальные must-have, вопросы работодателю.
- **Подбор вакансий по резюме** — ИИ составляет профиль и поисковые запросы → вакансии качаются
  с hh.ru / RSS → бесплатный префильтр → ИИ оценивает только лучшие. Можно запускать по расписанию.
- **Сопроводительные письма** — под конкретную вакансию, с учётом анализа соответствия, без выдуманных фактов.
- Учёт расходов на LLM и месячный лимит на каждого пользователя, кэш ответов.

Архитектура и правила расширения: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Требования

- Python 3.11+ (или Docker)
- API-ключ Anthropic **или** любой OpenAI-совместимый провайдер (OpenRouter, DeepSeek, локальная Ollama…)
- ~150 МБ RAM. Ноутбука на Ryzen/16 ГБ хватает с большим запасом.

## Быстрый старт (ноутбук)

```bash
git clone <repo> worksaas && cd worksaas
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env                 # впишите WS_SECRET_KEY и ANTHROPIC_API_KEY
cp config.example.toml config.toml   # при необходимости поправьте

worksaas create-user me --admin      # спросит пароль
worksaas create-user friend1 --budget 5
worksaas run                         # http://127.0.0.1:8000
```

Друзьям в той же сети: `worksaas run --host 0.0.0.0`. Через интернет — только за HTTPS (ниже).

Без API-ключа интерфейс можно пощупать, направив маршрут на заглушку:
```toml
[llm.routes.default]
provider = "fake"
model = "fake"
```

## VPS (Docker + HTTPS)

```bash
cp .env.example .env && cp config.example.toml config.toml   # заполнить
mkdir -p data && sudo chown 1000:1000 data                    # контейнер работает не от root
docker compose up -d --build
docker compose exec app worksaas create-user me --admin
```

Контейнер слушает только `127.0.0.1:8000`. HTTPS проще всего через [Caddy](https://caddyserver.com/)
(`/etc/caddy/Caddyfile`):
```
jobs.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Хватает самого дешёвого VPS (1 vCPU / 1 ГБ RAM).

## Прокси

Одна переменная `WS_PROXY_URL` в `.env` + флаг `use_proxy = true` у нужного провайдера или источника
в `config.toml`. Так можно пустить через прокси только Anthropic, а hh.ru — напрямую.
Поддержка HTTP(S)-прокси написана, но на реальном прокси не проверялась. Для SOCKS5 у hh.ru и
OpenAI-совместимых провайдеров нужен `pip install "httpx[socks]"`; работает ли SOCKS с клиентом
Anthropic SDK — не проверял.

## Сколько это стоит

Грубая оценка (не измерена на реальном API — посмотрите фактические цифры на странице «Расходы»):
один анализ ≈ 3–5 тыс. входных токенов + 1–3 тыс. выходных (включая размышления модели).
На `claude-opus-5-5` ($4 / $20 за 1M) это порядка **$0.03–0.08 за анализ**; подбор с `top_n = 15`
≈ $0.5–1.2 за запуск. Повторные запросы с теми же данными берутся из кэша бесплатно.

Рычаги (всё в `config.toml`, без правки кода):
- `matching.top_n` / `prefilter_min` — сколько вакансий доходит до ИИ;
- `effort` в маршрутах — глубина размышлений (низкий = дешевле);
- отдельный маршрут `[llm.routes.match]` на более дешёвую модель (например `claude-sonnet-5-5`
  или `claude-haiku-4-5`; для Haiku 4.5 уберите `effort` — эта модель его не поддерживает);
- `default_monthly_budget_usd` и `worksaas set-budget <user> <usd>`.

## Источники вакансий

| Источник | Статус |
|---|---|
| `hh` — публичный API hh.ru | Реализован по документации API; **из облака разработки hh.ru был недоступен, на реальном API не проверен**. Если hh.ru начнёт требовать авторизацию — укажите `access_token` в `[sources.hh.options]`. |
| `rss` — любые RSS/Atom-ленты | Реализован, проверен на тестовых лентах. Ленты задаются в `[sources.rss.options].feeds`. |
| `manual` / импорт по ссылке | hh.ru-ссылки грузятся через API, остальные — как текст страницы. |

Новый источник = один файл в `app/sources/` (см. ARCHITECTURE.md, раздел 4.3).

## Команды

```
worksaas run [--host H] [--port P]   веб + фоновые задачи в одном процессе
worksaas worker                      фоновые задачи отдельно (jobs.run_in_web_process = false)
worksaas init-db                     применить миграции
worksaas create-user NAME [--admin] [--budget USD]
worksaas set-password NAME
worksaas set-budget NAME USD         0 = без лимита
```

## Разработка

```bash
pip install -e ".[dev]"
pytest                    # тесты используют fake-LLM, сеть и деньги не нужны
alembic revision --autogenerate -m "..."   # после изменения моделей в app/models
```
