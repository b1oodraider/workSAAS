# Модели ИИ: три плана расходов (Cloud.ru Foundation Models)

Выбрано 2026-09-30 по ценам со скриншотов консоли Cloud.ru. **Качество моделей между собой
ещё не проверялось** — выбор сделан по цене и общему представлению о моделях; финальное
решение — после теста писем (см. `docs/HANDOFF.md`, раздел «Что делать дальше»).

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
| DeepSeek-V4.1-Flash | `deepseek-ai/DeepSeek-V4.1-Flash` | **догадка по образцу — проверить** в консоли |

## Готовые фрагменты `config.toml`

Общая часть для всех планов:

```toml
[llm.providers.cloudru]
type = "openai_compat"
base_url = "https://foundation-models.api.cloud.ru/v1"
api_key_env = "CLOUDRU_API_KEY"
json_mode = "json_object"   # если сервер не поддерживает — "none" (схема и так вставляется в промпт)
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

- `effort` в маршрутах сейчас **не передаётся** OpenAI-совместимым провайдерам — глубину «размышлений»
  gpt-oss/V4-Pro ограничить нельзя (предложено добавить `reasoning_effort`, ждёт согласия).
- Поддерживает ли Cloud.ru `response_format: json_object` — не проверено.
