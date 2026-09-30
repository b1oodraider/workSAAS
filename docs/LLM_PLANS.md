# Модели ИИ: три плана расходов (Cloud.ru Foundation Models)

Выбрано 2026-09-30 по ценам со скриншотов консоли Cloud.ru. **Качество моделей между собой
ещё не проверялось** — выбор сделан по цене и общему представлению о моделях; финальное
решение — после теста писем (см. `docs/HANDOFF.md`, раздел «Что делать дальше»).

## Результаты теста (2026-09-30) — действующий выбор

**Сверка с консолью (30.09, 37,10 ₽ с НДС):** цены таблицы для V4.1-Flash, GigaChat-3-Pro, GigaChat3.5 (в счёте — «GigaChat Ultra») и gpt-oss совпадают с реальными с точностью ~5 %. Лишнее дали: Qwen3.6-35B-A3B (в таблице не было; реально ~217 ₽ вход / ~320 ₽ выход за 1M — дороже V4.1-Flash!) и неудачные вызовы GigaChat-3-Pro, которые приложение не записывает.

**Бонусные баллы Cloud.ru работают не для всех моделей.** С нулевым денежным балансом отвечают:
`deepseek-ai/DeepSeek-V4.1-Flash`, `GigaChat/GigaChat-3-Pro`, `ai-sage/GigaChat3.5-432B-A28B`,
`Qwen/Qwen3.6-35B-A3B`. Остальные — в том числе V4-Flash, V4-Pro, gpt-oss-120b, GLM, Kimi, gpt-4o-mini —
отвечают `402 Not enough money`. Поэтому тестировались модели, доступные на баллах. Планы ниже, с V4-Flash
и gpt-oss, возможны только после пополнения счёта деньгами.

| Роль | Модель | effort | ₽ за вызов (замер) |
|---|---|---|---|
| Письма (`cover_letter`, промпт v5) | DeepSeek-V4.1-Flash | low | ~0,45–0,6 |
| Отбор (`match`) | DeepSeek-V4.1-Flash | none | ~0,35–0,45 |
| Проверка, резюме | DeepSeek-V4.1-Flash | none / low | не замерялось |
| Резерв | GigaChat3.5-432B-A28B | — | ~0,35–0,45 |

При объёмах из таблицы ролей (5 человек): отбор ~6 000 × 0,4 ≈ 2 400 ₽, письма ~1 950 × 0,5 ≈ 1 000 ₽,
остальное ~300–500 ₽. Итого **~3 700–3 900 ₽/мес**. Этого хватит примерно на месяц баллов.
Сэкономить можно так: `[matching] top_n = 10`, это на треть меньше оценок.

**Все ИИ-функции (01.10, после правки промптов, V4.1-Flash, ₽ за вызов по замеру):**

| Функция | ₽ | Функция | ₽ |
|---|---|---|---|
| resume_profile | 0,55 | letter_critic | 0,45 |
| resume_review | 1,0–1,2 | tailor_resume | 1,0–1,3 |
| match | 0,47 | interview_prep | 1,0 |
| vacancy_review | 0,45 | follow_up | 0,35 |
| cover_letter | 0,5 | recruiter_reply | 0,4 |
| | | offer_negotiation | 0,55 |

Промпты стали длиннее (правила против выдумок, дата, тип работы) — вход ~3,5–4k токенов.
Месяц на 5 человек при объёмах из таблицы ролей: отбор ~2 800 ₽, письма ~1 000, проверка ~700,
резюме ~250 — **~4 700 ₽**; с `[matching] top_n = 10` — **~3 800 ₽**.
Полный прогон всех функций на двух резюме (33 вызова) стоит ~19 ₽.

**Письма.** Вход — резюме владельца и 5 реальных вакансий hh.ru: две junior, стажёр, 3+ лет и 5+ лет.
Субагент оценивал письма вслепую, в случайном порядке, по 5 критериям, максимум 25 баллов.
- **GigaChat-3-Pro** — 11,6. С `json_object` возвращает саму JSON-схему; работает только с `json_schema`.
  При `max_tokens=8000` один ответ дважды уходил в бесконечный цикл. Главное — грубо выдумывает:
  «1,8 года коммерческой разработки», пет-проект подаёт как работу, «репозиторий под свадьбы»
  (на самом деле сервис знакомств), «профильное образование».
- **GigaChat3.5-432B** — 14,4. Длинные списки, кальки. Требования вакансии выдаёт за опыт кандидата:
  «Kafka — часть продакшена», «consumer groups, DLQ», «EXPLAIN».
- **DeepSeek-V4.1-Flash** — 21,0 при effort=low и 19,8 при none. Живой язык, честно признаёт разрыв
  в стаже, полезные warnings. Мои выводы и выводы рецензента совпали.
- У всех моделей повторялись одни и те же выдумки: сложенный стаж «1 год 8 месяцев»,
  «учусь в МАИ» при неоконченном, английский «для переписки», «Hibernate ежедневно».
  Поэтому промпт письма дописан (v4 → v5, после ревью prompt-critic): правила о стаже, статусе
  образования, словах-усилителях, чистке warnings. Результат на V4.1-Flash: **22,2/25**, грубых выдумок нет.
  Осталась натяжка «коммерческого стажа на Java меньше требуемого», хотя язык стажировки
  в резюме не указан.

**Отбор (`match`).** Те же 5 вакансий, по 2 прогона.
- **V4.1-Flash, effort none** — баллы стабильны: 82/82, 78/78, 42/42, 82/78; один разброс 55/42 у вакансии 5+ лет.
  Gaps и risks точные. Около 1 000 выходных токенов, 100 % валидный JSON.
- **V4.1-Flash, effort low** — 2–3,4 тыс. выходных токенов уходят на «размышления», вызов стоит вдвое дороже.
  Баллы чуть выше, из 10 ответов 1 — невалидный JSON.
- **Qwen3.6-35B-A3B, effort none** — дёшево и стабильно, но выдумывает: «профильное образование»,
  «уверенное владение Spring Boot 3.x». В одном ответе проскочили китайские иероглифы. Цена в таблице не записана.
- **gpt-oss-120b, effort low** (проверено после пополнения счёта) — ~0,08 ₽ за оценку по ценам из таблицы,
  в 5 раз дешевле. Но баллы скачут (12/35, 88/78, 78/88), а в фактах о кандидате больше всего ошибок:
  «высшее техническое образование (бакалавр)», «опыт 1–2 года», «настроил GitHub Actions для деплоя»,
  «опыт работы в agile». effort=medium не лучше. Слепая оценка субагента: 9,4/20.
- **DeepSeek-V4-Flash, effort low** — ~0,3 ₽ за оценку, 30–40 с на ответ из-за долгих «размышлений».
  Выносит резкие баллы (90/35/30), и дважды strong 90 держится на неверных фактах:
  «опыт более года», «образование соответствует». Слепая оценка: 14,2/20.
- **V4.1-Flash, effort none** — слепая оценка 16,4/20: меньше всего выдумок, ловит реальные блокеры
  (образование для Сбера, JavaScript, стаж). Мягкая калибровка для старших грейдов:
  55 при требовании 5+ лет. Решение лучше принимать по score с порогом около 60, а не по полю recommendation.
- Общая ошибка всех моделей в `match`: «1 год 8 месяцев» из шапки резюме считают стажем разработки,
  а в нём 11 месяцев репетиторства. Промпт `match` пока не правился.
- `reasoning_effort="none"` принимают V4.1-Flash и Qwen3.6. gpt-oss (low|medium|high) и V4-Flash
  (low..max) на `none` отвечают 400.
- Без `effort` Qwen3.6 тратит весь лимит на размышления и возвращает пустой ответ.
  `reasoning_effort` Cloud.ru принимает, в том числе `none`.

## Роли (какие функции какой моделью)

| Роль | Функции (`[llm.routes.<имя>]`) | Объём/мес на 5 человек | Токены вход / выход, млн |
|---|---|---|---|
| **Отбор** | `match` | ~6 000 | 24 / 4,2 |
| **Письма** | `cover_letter`, `follow_up`, `recruiter_reply` | ~1 950 | 7,3 / 1,35 |
| **Проверка** | `vacancy_review`, `letter_critic` | ~1 600 | 4,3 / 1,3 |
| **Резюме** («прожарка» = критика + правка) | `resume_profile`, `resume_review`, `tailor_resume`, `interview_prep`, `offer_negotiation` | ~250 | 1,1 / 0,45 |

Допущения (оценка, не замер): резюме ~5 000 знаков, вакансия ~3 500, ~3 знака русского текста
на токен, 5 человек. Размеры промптов замерены на коде: `match` ≈ 4k токенов на вход (с JSON-схемой),
`cover_letter` ≈ 3,8k. Одна оценка вакансии или одно письмо на DeepSeek-V4-Flash ≈ 0,23 ₽.

## План «1 500 ₽»

| Роль | Модель | ₽/мес |
|---|---|---|
| Отбор | gpt-oss-120b | ~640 |
| Письма | DeepSeek-V4-Flash | ~430 |
| Проверка | gpt-oss-120b | ~150 |
| Резюме | DeepSeek-V4-Flash | ~90 |
| **Итого** | | **~1 300** (до ~1 600, если gpt-oss много «думает») |

Если не влезает — `[matching] top_n = 10` (вместо 15): оценок станет на треть меньше.

## План «3 000 ₽»

| Роль | Модель | ₽/мес |
|---|---|---|
| Отбор | DeepSeek-V4-Flash | ~1 400 |
| Письма | GigaChat-3-Pro | ~770 |
| Проверка | DeepSeek-V4-Flash | ~300 |
| Резюме | DeepSeek-V4.1-Flash | ~160 |
| **Итого** | | **~2 630** |

Для писем взаимозаменяемы: GigaChat-3-Pro (~770), Gemini 3.1 Flash Lite (~660), DeepSeek-V4.1-Flash (~740) —
выбрать по тесту.

## План «6 000 ₽»

| Роль | Модель | ₽/мес |
|---|---|---|
| Отбор | DeepSeek-V4-Flash | ~1 400 |
| Письма | DeepSeek-V4-Pro | ~2 320 |
| Проверка | DeepSeek-V4.1-Flash | ~530 |
| Резюме | DeepSeek-V4-Pro | ~530 |
| **Итого** | | **~4 800** |

~1 200 ₽ оставлено в запас: если V4-Pro «думает», выходных токенов больше. Если запас не нужен —
Отбор на DeepSeek-V4.1-Flash (≈5 800). Альтернативы для писем: GLM-5 (~2 000), Kimi-K2.6 (~2 300).

## Общий топ моделей для проекта

1. **DeepSeek-V4-Flash** — универсальная, лучшее соотношение (на всё ~2 200 ₽).
2. **gpt-oss-120b** — самая дешёвая для массовой оценки (на всё ~1 000–1 450 ₽).
3. **DeepSeek-V4-Pro** — письма/резюме при бюджете.
4. GLM-5 — альтернатива V4-Pro.
5. GigaChat-3-Pro — русский деловой стиль писем.
6. Gemini 3.1 Flash Lite — письма, средний сегмент.
7. DeepSeek-V4.1-Flash — шаг вверх от V4-Flash.
8. GPT 4o Mini — резервная модель (надёжный JSON).
9. MiMo-V2.5 — очень дёшево, качество неизвестно.
10. Kimi-K2.6 — сильная, но дорогая.

Не подходят: эмбеддинги/ререйнкеры/OCR/whisper (не генерируют текст); Qwen3-Coder-Next (для кода);
Max/«Thinking»-модели, GLM-4.7, MiniMax — дорого; GPT 4.1 Nano, Qwen3-30B-A3B, llama-3.3-70b — слабы
для русских писем.

## Цены (₽ за 1 млн токенов, вход / выход; со скриншотов, с НДС или без — неизвестно)

| Модель | Вход | Выход | Контекст |
|---|---|---|---|
| gpt-oss-120b | 15.86 | 61 | 131K |
| MiMo-V2.5 | 24.00 | 48.00 | 1048K |
| GPT 4o Mini | 29.463 | 117.852 | 128K |
| DeepSeek-V4-Flash | 43.2978 | 86.5834 | 1048K |
| Gemini 3.1 Flash Lite | 42.7 | 256.2 | 1048K |
| DeepSeek-V4.1-Flash | 64.94 | 194.81 | 1048K |
| GigaChat-3-Pro | 73.03 | 176.39 | 262K |
| GigaChat3.5-432B-A28B | 96.22 | 288.6 | 262K |
| GLM-5 | 170.8 | 546.56 | 202K |
| DeepSeek-V4-Pro | 183 | 732 | 1048K |
| Kimi-K2.6 | 175.68 | 725.9 | 262K |

## Имена моделей в API Cloud.ru

Адрес: `https://foundation-models.api.cloud.ru/v1` (OpenAI-совместимый). Ключ — переменная окружения
`CLOUDRU_API_KEY` (в `.env` или в настройках облачного окружения). **Никогда не коммитить и не печатать.**

| Модель | Имя в API | Источник |
|---|---|---|
| DeepSeek-V4-Flash | `deepseek-ai/DeepSeek-V4-Flash` | пример кода с сайта (подтверждено) |
| DeepSeek-V4-Pro | `deepseek-ai/DeepSeek-V4-Pro` | пример кода с сайта (подтверждено) |
| gpt-oss-120b | `openai/gpt-oss-120b` | пример кода с сайта (подтверждено) |
| GigaChat-3-Pro | `GigaChat/GigaChat-3-Pro` | пример кода с сайта (подтверждено) |
| DeepSeek-V4.1-Flash | `deepseek-ai/DeepSeek-V4.1-Flash` | подтверждено запросом 2026-09-30 |
| GigaChat3.5-432B | `ai-sage/GigaChat3.5-432B-A28B` | подтверждено запросом (цена 96,22 / 288,6) |

## Готовые фрагменты `config.toml`

Общая часть для всех планов:

```toml
[llm.providers.cloudru]
type = "openai_compat"
base_url = "https://foundation-models.api.cloud.ru/v1"
api_key_env = "CLOUDRU_API_KEY"
json_mode = "json_object"   # если сервер не поддерживает — "none" (схема и так вставляется в промпт)
reasoning_effort = true     # effort маршрута -> reasoning_effort (проверено)
```

### План «1 500 ₽»

```toml
[llm.routes.default]         # всё, что не перечислено ниже
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4-Flash"

[llm.routes.match]
provider = "cloudru"
model = "openai/gpt-oss-120b"
[llm.routes.vacancy_review]
provider = "cloudru"
model = "openai/gpt-oss-120b"
[llm.routes.letter_critic]
provider = "cloudru"
model = "openai/gpt-oss-120b"
# cover_letter, follow_up, recruiter_reply, resume_* , tailor_resume, interview_prep,
# offer_negotiation — через default (DeepSeek-V4-Flash)
```

### План «3 000 ₽»

```toml
[llm.routes.default]         # Отбор, Проверка
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4-Flash"

[llm.routes.cover_letter]
provider = "cloudru"
model = "GigaChat/GigaChat-3-Pro"
[llm.routes.follow_up]
provider = "cloudru"
model = "GigaChat/GigaChat-3-Pro"
[llm.routes.recruiter_reply]
provider = "cloudru"
model = "GigaChat/GigaChat-3-Pro"

# Резюме — DeepSeek-V4.1-Flash (имя модели проверить!)
[llm.routes.resume_profile]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
[llm.routes.resume_review]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
[llm.routes.tailor_resume]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
[llm.routes.interview_prep]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
[llm.routes.offer_negotiation]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
```

### План «6 000 ₽»

```toml
[llm.routes.default]         # Письма и Резюме
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4-Pro"

[llm.routes.match]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4-Flash"

# Проверка — DeepSeek-V4.1-Flash (имя модели проверить!)
[llm.routes.vacancy_review]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
[llm.routes.letter_critic]
provider = "cloudru"
model = "deepseek-ai/DeepSeek-V4.1-Flash"
```

Резервная модель (на любой маршрут): `fallbacks = [{ provider = "cloudru", model = "<имя GPT 4o Mini в API — узнать в консоли>" }]`.

## Учёт бюджета в приложении

Лимиты и цены в приложении хранятся «в долларах» (`[llm.prices."<модель>"]`, $ за 1M токенов;
без цены модель считается бесплатной, и лимит не сработает). Варианты:

- перевести рубли по курсу и вписать цены в $;
- или вписать цены **в рублях** и лимиты пользователей тоже в рублях — считаться будет верно, но
  в интерфейсе останется значок «$» (косметика; можно добавить настройку валюты — не сделано).

```toml
# цены в рублях (вариант 2)
[llm.prices."deepseek-ai/DeepSeek-V4-Flash"]
input = 43.2978
output = 86.5834
[llm.prices."deepseek-ai/DeepSeek-V4-Pro"]
input = 183
output = 732
[llm.prices."openai/gpt-oss-120b"]
input = 15.86
output = 61
[llm.prices."GigaChat/GigaChat-3-Pro"]
input = 73.03
output = 176.39
[llm.prices."deepseek-ai/DeepSeek-V4.1-Flash"]
input = 64.94
output = 194.81
```

## Известные ограничения

- `effort` маршрута передаётся OpenAI-совместимым провайдерам как `reasoning_effort` (`none` — без размышлений);
  включается у провайдера строкой `reasoning_effort = true` (по умолчанию выключено: не все серверы принимают параметр). `xhigh`/`max` сводятся к `high`.
- `json_object` у Cloud.ru работает (DeepSeek, GigaChat3.5); GigaChat-3-Pro нужен `json_schema`.
- GigaChat-3-Pro к каждому запросу добавляет ~1 200 скрытых входных токенов (видно в `usage`).
- Неудачные вызовы (обрезанный ответ, невалидный JSON) провайдер оплачивает, но приложение записывает их с нулевой стоимостью.
