# Модели ИИ: три плана расходов (Cloud.ru Foundation Models)

Выбрано 2026-09-30 по ценам со скриншотов консоли Cloud.ru. **Качество моделей между собой
ещё не проверялось** — выбор сделан по цене и общему представлению о моделях; финальное
решение — после теста писем (см. `docs/HANDOFF.md`, раздел «Что делать дальше»).

**2026-10-07:** цены сверены с официальным тарифом, добавлена быстрая оценка (`match_quick`) и расчёт на
одного пользователя — разделы «Цены», «Быстрая оценка» и «Сколько уходит на одного пользователя» ниже
(они точнее старых планов, где каждая вакансия шла в полный `match`).

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

Эмбеддинги — только для отбора по смыслу (`BAAI/bge-m3`, 0,61 ₽ за 1 млн, раздел ниже). Не подходят: ререйнкеры/OCR/whisper; Qwen3-Coder-Next (для кода);
Max/«Thinking»-модели, GLM-4.7, MiniMax — дорого; GPT 4.1 Nano, Qwen3-30B-A3B, llama-3.3-70b — слабы
для русских писем.

## Цены (₽ за 1 млн токенов с НДС 22%, вход / выход)

Проверено 2026-10-07 по официальному тарифу Cloud.ru (Приложение №7.EVO.11.2, версия 261002,
https://cdn.cloud.ru/docs/legal/tariffs/evolution/current-version/foundation-models.pdf) и списку моделей
(https://cloud.ru/docs/foundation-models/ug/topics/overview__available__models). Цены со скриншотов совпали.
«Ext» — модель внешняя: запросы уходят стороннему API за пределы Cloud.ru, SLA на неё не действует.

| Модель | Имя в API | Вход | Выход | Контекст | «Размышления» |
|---|---|---|---|---|---|
| gpt-oss-20b (Ext) | `openai/gpt-oss-20b` | 5.888 | 27.50 | 131K | да |
| GigaChat 3 Lightning | `ai-sage/GigaChat3-10B-A1.8B` | 12.20 | 12.20 | 262K | нет |
| gpt-oss-120b | `openai/gpt-oss-120b` | 15.86 | 61.00 | 131K | да |
| Qwen3-30B-A3B (Ext) | `Qwen/Qwen3-30B-A3B` | 16.24 | 64.94 (thinking 162.33) | 41K | да |
| MiMo-V2.5 | `xiaomi/mimo-v2.5` | 24.00 | 48.00 | 1M | нет |
| GPT-4o mini | `openai/gpt-4o-mini` | 29.463 | 117.852 | 128K | нет |
| Gemini 3.1 Flash Lite | `google/gemini-3.1-flash-lite` | 42.70 | 256.20 | 1M | нет |
| DeepSeek-V4-Flash | `deepseek-ai/DeepSeek-V4-Flash` | 43.2978 | 86.5834 | 1M | да |
| DeepSeek-V4.1-Flash | **нет в списке — имя из консоли** | 64.94 | 194.81 | ? | ? |
| GigaChat-3-Pro | `GigaChat/GigaChat-3-Pro` (из консоли, 2026-10-08) | 73.03 | 176.39 | 262K | нет |
| GigaChat 3.5 Reasoning («Ultra») | `ai-sage/GigaChat3.5-432B-A28B-Reasoning` | 96.22 | 288.60 | 262K | да |
| GLM-5 | `z-ai/glm-5` | 170.80 | 546.56 | 203K | да |
| Kimi-K2.6 | `moonshotai/Kimi-K2.6` | 175.68 | 725.90 | 262K | да |
| GLM-5.1 | `zai-org/GLM-5.1` | 198.86 | 829.60 | 1M | да |
| DeepSeek-V4-Pro | `deepseek-ai/DeepSeek-V4-Pro` | 183.00 | 732.00 | 1M | да |

Бесплатной квоты сейчас нет (акция 2025 года закончилась). Прерванный запрос оплачивается полностью.
Поддержка `json_schema`/`json_object` и `reasoning_effort` на Cloud.ru не проверена — проверить `worksaas doctor --llm`.

## Быстрая оценка (`match_quick`) — какая модель

Подбор теперь двухступенчатый: каждая найденная вакансия (top-N после префильтра) получает дешёвую
`match_quick` (балл + одна строка), подробный `match` — только при балле ≥ `[matching] full_match_min` (60).
Это самая массовая операция, на неё нужна самая дешёвая модель без «размышлений».

1. **GigaChat3-10B-A1.8B (Lightning)** — рекомендуемая: работает внутри Cloud.ru (данные резюме не уходят
   стороннему API), без «размышлений» (стоимость предсказуема, нет риска обрезки), родной русский,
   ~0,044 ₽ за оценку. `max_tokens = 400`.
2. **gpt-oss-20b** — ещё дешевле на бумаге (~0,034 ₽ с учётом ~400 токенов «размышлений»), но внешняя модель
   и «думает»: держать `max_tokens = 2000`, иначе обрезанные (но оплаченные) ответы.
3. Qwen3-30B-A3B — нет: контекст 41K и дорогие «размышления».

Качество на задаче не проверено (бенчмарков под «оценку вакансии» нет). Перед выбором — прогнать 20–30 пар
резюме/вакансия: GigaChat3-10B и gpt-oss-20b против gpt-oss-120b; смотреть, совпадает ли сторона порога 60.

```toml
[llm.routes.match_quick]
provider = "cloudru"
model = "ai-sage/GigaChat3-10B-A1.8B"
max_tokens = 400

[llm.prices."ai-sage/GigaChat3-10B-A1.8B"]   # рубли, см. «Учёт бюджета»
input = 12.20
output = 12.20
```

## Сколько уходит на одного пользователя в месяц

Допущения (оценка, не замер): объёмы — как в таблице «Роли», делённые на 5 человек: ~1 200 быстрых
оценок, из них ~35% (~420) доходят до подробной; ~390 писем, ~320 проверок, ~50 операций с резюме.
Размер промптов замерен на коде: `match_quick` ≈ 3 500 токенов на вход (почти всё — резюме и вакансия),
`match` ≈ 3 900; «размышления» gpt-oss/V4-Pro приняты в 400–600 токенов на вызов (не замерено).
Кэш ответов (одна вакансия у двух друзей) не учтён — реальность будет чуть дешевле.

| План | Быстрая оценка | Подробная | Письма | Проверка | Резюме | **₽ / польз. / мес** | Было бы без быстрой оценки |
|---|---|---|---|---|---|---|---|
| Эконом | GigaChat3-10B | gpt-oss-120b | V4-Flash | gpt-oss-120b | V4-Flash | **~250** | ~305 |
| Баланс | GigaChat3-10B | V4-Flash | GigaChat-3-Pro | V4-Flash | V4.1-Flash | **~395** | ~520 |
| Качество | gpt-oss-20b | V4-Flash | V4-Pro | V4.1-Flash | V4-Pro | **~920** | ~1 060 |

Итого на 5 человек: ~1 250 / ~2 000 / ~4 600 ₽ в месяц. Главная статья в «Качестве» — письма на V4-Pro (~550 ₽).
Активный пользователь (вдвое больше поисков и писем) — примерно вдвое дороже; бюджет на человека —
`worksaas set-budget`.

## Планы по бюджету (2026-10-08, любые модели)

Подобраны перебором всех сочетаний 16 моделей по 5 задачам: максимум «индекса качества» при расходе ≤ 85%
бюджета (15% — запас на неточность оценки). Индекс — **экспертная оценка, не замер** (вес: письма 0,40,
подробная оценка 0,20, быстрая 0,15, резюме 0,15, проверка 0,10); проверить тестом писем (HANDOFF, шаг 2).
Каналы оплаты — только два: Cloud.ru (рубли, договор) и AITUNNEL (реселлер, рубли). Объёмы на человека:
1 200 быстрых оценок, 420 подробных, 390 писем, 320 проверок, 50 операций с резюме в месяц.

Цены, ₽ за 1 млн (вход / выход / кэш): DeepSeek-V4.1-Flash @AITUNNEL 30 / 120 / 1,2;
Gemini-3.8-Flash @AITUNNEL 150 / 750 / 15; gpt-6-luna @AITUNNEL 20 / 100 / 3; Claude Sonnet 5.5 @AITUNNEL
400 / 2 000 / 40; Claude Opus 5.5 @AITUNNEL 800 / 4 000; DeepSeek-V4-Pro @Cloud.ru 183 / 732; gpt-oss-20b
@Cloud.ru 5,9 / 27,5. Claude Haiku 5.5 ($0,10 / $0,50) реселлеры не продают.

Цена одного вызова, ₽: письмо — V4.1-Flash 0,23 · Gemini-3.8-Flash 1,3 · V4-Pro 1,6 · Sonnet 5.5 3,9 ·
Opus 5.5 8,2; подробная оценка — V4.1-Flash 0,19 · Gemini-3.8-Flash 1,1 · Sonnet 5.5 3,4.

### На одного пользователя

| Бюджет | Быстрая | Подробная | Письма | Проверка | Резюме | Расход | Индекс |
|---|---|---|---|---|---|---|---|
| 600 ₽ | V4.1-Flash | V4.1-Flash | V4.1-Flash | V4.1-Flash | V4-Pro | ~470 | 7,8 |
| 900 ₽ | gpt-6-luna | V4.1-Flash | **Gemini-3.8-Flash** | V4.1-Flash | V4.1-Flash | ~740 | 8,0 |
| 1 500 ₽ | V4.1-Flash | V4.1-Flash | Gemini-3.8-Flash | V4.1-Flash | **Sonnet 5.5** | ~1 080 | 8,35 |
| 2 000 ₽ | V4.1-Flash | Gemini-3.8-Flash | Gemini-3.8-Flash | V4.1-Flash | Sonnet 5.5 | ~1 470 | 8,45 |
| 2 000 ₽, без запаса | V4.1-Flash | V4.1-Flash | **Sonnet 5.5** | V4.1-Flash | V4.1-Flash | ~1 780 | 8,45 (лучше письма) |

### На команду 3–5 человек (план рассчитан на 5; при 3 — следующий уровень)

| Бюджет | На 5 человек (на одного) | Состав | Расход на 5 | При 3 людях можно |
|---|---|---|---|---|
| 1 500 ₽ | 300 ₽ | быстрая и проверка — gpt-oss-20b, остальное — V4.1-Flash | ~1 190 | всё на V4.1-Flash (~1 090) |
| 3 000 ₽ | 600 ₽ | план «600» | ~2 360 | план «900/1000» — письма на Gemini (~2 540) |
| 4 500 ₽ | 900 ₽ | план «900» — письма на Gemini-3.8-Flash | ~3 690 | план «1 500» — + резюме на Sonnet (~3 240) |
| 6 000 ₽ | 1 200 ₽ | быстрая/подробная/проверка — V4.1-Flash, письма — Gemini, резюме — V4-Pro | ~4 450 | письма на Sonnet 5.5 (~5 350) |

Выводы: рабочая лошадь — DeepSeek-V4.1-Flash (дёшево, хорошо держит JSON и русский); первые лишние деньги
выгоднее всего тратить на письма (Gemini-3.8-Flash, затем Claude Sonnet 5.5), потом на резюме; дорогие модели
на массовой оценке почти не добавляют качества. Opus 5.5, Gemini 3.1 Pro и gpt-6.1-sol ни в один бюджет до
2 000 ₽/чел. не проходят: одни письма на Opus — ~3 200 ₽/чел.

Настройка (провайдер AITUNNEL — OpenAI-совместимый вход; имена моделей сверить на aitunnel.ru):

```toml
[llm.providers.aitunnel_oai]
type = "openai_compat"
base_url = "https://api.aitunnel.ru/v1"
api_key_env = "AITUNNEL_API_KEY"
send_reasoning_effort = true

[llm.routes.default]
provider = "aitunnel_oai"
model = "deepseek-v4-1-flash"
effort = "low"

[llm.routes.cover_letter]          # план «900» и выше
provider = "aitunnel_oai"
model = "gemini-3.8-flash"
effort = "low"
```

## Тест писем вслепую (2026-10-08) — выбрана GLM-5.1

`worksaas compare-letters`, резюме владельца, 5 реальных вакансий (стажёр Java, Junior Go, Java 3–6 лет, QA, PHP —
нарочно разные по совпадению стека). Оценивали двое независимо: я и субагент вслепую (без ключа к моделям); выводы
совпали. Балл — из 25 (конкретика, нет выдумок, соответствие вакансии, язык, длина).

| Модель | Промпт v3: балл / писем без выдумок | Промпт v5 | ₽ за письмо (v5) |
|---|---|---|---|
| **zai-org/GLM-5.1** | **21,4 / 5 из 5** | 5 из 5, отвечает на вопросы работодателя | 2,18 |
| moonshotai/Kimi-K2.6 | 20,8 / 4 из 5 | — | 4,50 (v3), ~100 с на письмо |
| ai-sage/GigaChat3.5-432B-A28B-Reasoning | 18,8 / 0 из 5 | 18,6 / 0 из 5 | 1,29 |
| deepseek-ai/DeepSeek-V4-Pro | 18,2 / 2 из 5 | 15,8 / 2 из 5, одно письмо — мусорный текст | 0,96 |
| GigaChat/GigaChat-3-Pro | 5 из 5 упали (ответ в форме схемы) | 13 / 0 из 5 | 0,37 |

Типичные выдумки дешёвых моделей: «изучаю Go / знаю PHP на уровне pet-проектов», личный проект как коммерческий
опыт, «полгода» вместо ~4 месяцев, технологии, приписанные месту работы, «нагрузочные тесты», «200+ запросов».
Промпт v5 убрал их у GLM, но не у дешёвых моделей. Итог: письма, follow_up, recruiter_reply — GLM-5.1
(~850 ₽/мес на человека при 390 письмах), вариант 3 целиком — ~1 200 ₽/мес. Отчёты — `data/letter-tests/`.

## Письма и отбор по смыслу — всё через Cloud.ru (2026-10-08)

Расход на письма при ~390 письмах на человека в месяц (вход ~3,7k, выход ~0,7k токенов + «размышления»
у моделей с ними — на глаз ~0,5–1k; не замерено) и итог «варианта 3» (быстрая — GigaChat3-10B, подробная
и проверка — DeepSeek-V4-Flash, резюме — V4-Pro), ₽ на человека в месяц:

| Письма | Письма | Итого |
|---|---|---|
| `GigaChat/GigaChat-3-Pro` | ~150 | ~490 |
| `ai-sage/GigaChat3.5-432B-A28B-Reasoning` | ~330 | ~670 |
| `moonshotai/Kimi-K2.6` | ~590 | ~930 |
| `deepseek-ai/DeepSeek-V4-Pro` | ~620 | ~960 |
| `zai-org/GLM-5.1` | ~680 | ~1 020 |

Выбор — по тесту вслепую: `worksaas compare-letters <логин> --resume … --vacancy …` (HANDOFF, шаг 2).
Резюме на Gemini 3.1 Pro Preview (внешняя, 341,6 / 2 049,6 ₽) — ~415 ₽ вместо ~125 на V4-Pro.

**Отбор по смыслу**: `BAAI/bge-m3` (Cloud.ru, своя, 0,61 ₽ за 1 млн, 8K контекста, многоязычная — русское
резюме против английских вакансий). Вектор считается один раз на текст для всех пользователей: единицы
рублей в месяц. Альтернативы: `Giga-Embeddings-instruct-480M` (3,32 ₽, сильнее на русском — не проверено),
Qwen3-Embedding-0.6B (0,85 ₽). Внешние OpenAI/Gemini-эмбеддинги дороже и уходят за пределы Cloud.ru.

```toml
[llm.embeddings]
enabled = true
provider = "cloudru"
model = "BAAI/bge-m3"

[llm.prices."BAAI/bge-m3"]
input = 0.61
output = 0
```

Поддерживает ли Cloud.ru `/v1/embeddings` в OpenAI-формате — проверить `worksaas doctor --llm`
(покажет размер вектора и сходство «Python-разработчик» / «Python developer»).

## Модели OpenAI GPT-5 / GPT-6 (оценка 2026-10-07)

Цены за 1 млн токенов (вход / выход). OpenAI — с developers.openai.com/api/docs/pricing; Cloud.ru — тариф 261002
(с НДС); AITUNNEL — страницы моделей. **GPT-6 на Cloud.ru нет**; оплатить из РФ — через реселлеров (наценка
~2,3× у AITUNNEL, ~3× у ProxyAPI к цене OpenAI по курсу ЦБ 85,48). Все модели «размышляющие»: скрытые
рассуждения оплачиваются как выход, по умолчанию `medium` — задавать `effort` явно (и `send_reasoning_effort = true`).

| Модель | OpenAI, $ | Где в рублях | ₽ вход / выход |
|---|---|---|---|
| gpt-6-luna (есть `none`) | 0.10 / 0.50 | AITUNNEL | 20 / 100 (проверено) |
| gpt-6.1-sol | 2 / 10 | реселлер | ~393 / ~1 966 (оценка ×2,3) |
| gpt-6-astra | 10 / 50 | реселлер | ~1 966 / ~9 830 (оценка ×2,3) |
| gpt-5-nano | 0.05 / 0.40 | Cloud.ru | 9.821 / 78.568 |
| gpt-5-mini | 0.25 / 2.0 | Cloud.ru | 49.105 / 392.84 |
| gpt-5.4-mini | 0.75 / 4.5 | Cloud.ru | 147.315 / 883.89 |
| gpt-5 / 5.1 | 1.25 / 10 | Cloud.ru | 245.525 / 1 964.2 |

Расход на пользователя в месяц при тех же объёмах, что выше («размышления»: none — 0, low — ~300,
medium — ~1 000 токенов на вызов; не замерено):

| Вариант | Быстрая | Подробная | Письма | Проверка | Резюме | **₽ / польз. / мес** |
|---|---|---|---|---|---|---|
| Всё на gpt-6-luna (быстрая — none, прочее — low) | luna | luna | luna | luna | luna | **~300** |
| GPT-5 на Cloud.ru | 5-nano | 5-mini | 5-mini | 5-mini | 5-mini | **~790** |
| Письма и резюме на gpt-6.1-sol (medium) | luna | luna | 6.1-sol | luna | 6.1-sol | **~2 450** |
| gpt-6.1-sol везде, кроме быстрой | luna | 6.1-sol | 6.1-sol | 6.1-sol | 6.1-sol | **~4 800** |
| Письма и резюме на gpt-6-astra | luna | 6.1-sol | astra | 6.1-sol | astra | **~13 750** |
| gpt-6-astra на всё | astra | astra | astra | astra | astra | **~32 900** |

Выводы: gpt-6-luna по цене — уровень «Эконом» (~300 ₽), но для быстрой оценки GigaChat3-10B всё равно
дешевле (0,044 против 0,078 ₽ за вызов). Сильные GPT-6 стоят 5–90× больше DeepSeek/GigaChat; разумный
компромисс — сильная модель только на письма (одно письмо на gpt-6.1-sol ≈ 4,8 ₽, на gpt-6-astra ≈ 24 ₽).
Кэш промптов OpenAI (вход в 10 раз дешевле для повторяющегося начала промпта) может снизить вход
подробной/быстрой оценки примерно вдвое, если реселлер его пробрасывает, — не проверено.

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

- `effort` маршрута передаётся OpenAI-совместимому провайдеру как `reasoning_effort`, только если у провайдера
  `send_reasoning_effort = true` (по умолчанию выключено: часть серверов отвергает незнакомые параметры).
  Для gpt-oss на Cloud.ru — включить и проверить `worksaas doctor --llm`; `effort = "low"` на `match`/`match_quick`.
- Поддерживает ли Cloud.ru `response_format: json_object` — не проверено.
