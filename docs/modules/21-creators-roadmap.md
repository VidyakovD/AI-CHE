# Модуль 21 — Creators (📅 В РАЗРАБОТКЕ — MVP)

> **Статус:** 🟡 **Согласован концепт, начинается реализация.** Не «продукт для блогеров», а **рабочая зона для бизнеса/SMM** — компания заполняет профиль, AI строит контент-план в виде календаря, готовит каждый пост (с учётом актуальности), публикует автоматически или по одобрению.

## Концепция (со слов юзера, 2026-05-13)

**Главный экран — календарь.** Сетка дней (месяц). На днях с постами — цветные плашки (видео=цвет А, текст=цвет Б, reels=цвет В). Сверху чекбоксы платформ: **Instagram / TG / VK / YouTube / MAX** — что включено, то отображается на календаре.

**Flow юзера:**
1. Заходит в `/creators.html` → выбирает/создаёт **компанию** (профиль)
2. Заполняет профиль (ниша, продукт, аудитория, tone-of-voice, темы, стоп-слова)
3. Жмёт «Сгенерировать контент-план на месяц» → AI разбрасывает посты по дням и платформам
4. Видит календарь, разноцветный → клик по дню → карточка с подготовленным материалом
5. **В день постинга** AI готовит «свежие» посты (новости/тренды через Perplexity); evergreen-посты готовы заранее
6. Юзер одобряет или редактирует → пост публикуется (автопостинг где доступно) или копирует руками

**Дополнительно — анализ соцсетей:** парсинг своих/чужих профилей через Perplexity → рекомендации.

## Архитектурное решение

**Отдельная страница `/creators.html`** + собственный домен моделей. Переиспользуем:
- [03-ai-core](03-ai-core.md) — Perplexity (свежак) + Sonnet (тексты) + DALL-E/Imagen (картинки)
- [06-solutions](06-solutions.md) — orchestra-pipeline для подготовки поста
- [10-agents-workflows](10-agents-workflows.md) — `tool_send_tg_message`, `tool_send_vk_post` для автопостинга
- [11-knowledge-rag](11-knowledge-rag.md) — RAG по архиву контента (опционально v2)

Не используем:
- Solution-каталог (Креаторы — отдельный flow, не пилоты)
- proposals (другая логика)
- chatbots (это для общения с клиентами, не публикации)

## MVP scope (предложения по умолчанию, корректируется до старта)

### Профиль компании (8 полей для MVP)

1. `name` — название компании / бренда
2. `niche` — ниша (select: e-commerce / услуги / IT / производство / общепит / медицина / красота / образование / другое)
3. `product` — что продаёте (textarea)
4. `audience` — кто целевая аудитория (textarea: возраст / пол / география / интересы)
5. `tone` — tone-of-voice (select: дружеский / экспертный / премиум / провокативный / нейтральный)
6. `topics` — 3-5 ключевых тем (chips/tags input)
7. `stopwords` — стоп-слова (textarea: что не писать)
8. `logo` — лого (опц, file upload в Storage)

V2 добавим: брендбук цвета/шрифты, конкуренты, конкретные UTM, история постов.

### Типы контента (для цветов в календаре)

- 📝 Текстовый пост (короткий)
- 🖼 Картинка + текст
- 🎥 Reels/Shorts сценарий
- 🎬 YouTube видео (описание + тайтл + теги)
- 📊 Опрос/вопрос
- 📰 Новостной (актуальный)

### Pipeline генерации

```
Шаг A (один раз при «Сгенерировать план»):
  Профиль → Sonnet → 30 ContentItem'ов (date, platform, type, brief)

Шаг B (за день до публикации, scheduler):
  ContentItem с типом «новостной» → Perplexity research → Sonnet текст → DALL-E картинка (если нужно)
  ContentItem evergreen → Sonnet текст → DALL-E картинка (если нужно)
  Результат → ContentItem.prepared_content + status='ready'

Шаг C (в момент публикации, scheduler):
  ContentItem.status='ready' и schedule_at <= now:
    - TG/VK подключены → автопубликация
    - Нет подключения → push «пост готов, скопируй и опубликуй»
```

### Тариф — **Freemium** (согласовано 2026-05-13)

**Бесплатно каждому юзеру:**
- 3 подготовленных поста / месяц (Шаг B)
- Сама генерация контент-плана (Шаг A — лишь Sonnet с профилем) — бесплатно всегда (раз в неделю не чаще)

**Платно (когда лимит исчерпан):**
- **Подготовить пост (evergreen)** — 15 ₽ (Sonnet + опц DALL-E)
- **Подготовить пост (с актуальным research'ем)** — 25 ₽ (Perplexity + Sonnet + опц DALL-E)
- **Картинка DALL-E** — отдельно ~5 ₽
- **Анализ соцсети (свой профиль)** — 150 ₽ (Perplexity-пилот) — **не freemium**
- **Анализ конкурента** — 200 ₽ (Perplexity reasoning-pro) — **не freemium**

Лимит счётчик в `creator_profiles.free_posts_used_this_month` + сброс через scheduler 1 числа.

Подписка (например 990 ₽/мес = до 30 постов) — v2 после набора юзеров.

### Платформы — **все 4 включаем сразу** (согласовано 2026-05-13)

| Платформа | Стратегия MVP |
|---|---|
| **Telegram** | Полный автопостинг (Bot API, юзер подключает бота как админа канала) |
| **VK** | Полный автопостинг (wall.post API, community-token через подключение приложения) |
| **YouTube** | OAuth + Data API v3 для генерации описаний/тайтлов/тегов/таймкодов. Видео заливает юзер. **Итерация 5+** (OAuth-flow добавит работы). |
| **Instagram** | Только генерация контента (текст + картинка). Автопостинг **не делаем** в MVP (Meta-API через посредников нестабильно). Юзер копирует руками. |

## Модели (новые таблицы)

| Таблица | Поля важные |
|---|---|
| `creator_profiles` | user_id, name, niche, product, audience, tone, topics_json, stopwords, logo_url, created_at |
| `content_calendars` | profile_id, period_start, period_end, status (draft/active), generated_at |
| `content_items` | calendar_id, date, schedule_at, platform (tg/vk/yt/ig), type (text/image/reels/youtube/poll/news), brief, prepared_content_md, prepared_media_url, status (planned/preparing/ready/published/skipped), published_at, manual_override_text |
| `channel_connections` | profile_id, platform, channel_id, token (EncryptedString), is_active, fail_count |
| `creator_analysis_runs` | profile_id, type (own/competitor), target_url, result_md, cost_kop, created_at |

## Endpoints (черновик)

| Метод | Endpoint | Что |
|---|---|---|
| GET/POST/PUT/DELETE | `/creators/profiles` | CRUD профилей компании |
| POST | `/creators/profiles/{id}/calendar/generate` | Сгенерировать план на месяц (200 ₽) |
| GET | `/creators/profiles/{id}/calendar?from=&to=` | Получить календарь |
| PUT | `/creators/items/{id}` | Изменить пост (date, type, content) |
| POST | `/creators/items/{id}/prepare` | Принудительно подготовить сейчас |
| POST | `/creators/items/{id}/approve` | Одобрить (status=ready) |
| POST | `/creators/items/{id}/publish` | Опубликовать вручную сейчас |
| DELETE | `/creators/items/{id}` | Пропустить |
| GET/POST/DELETE | `/creators/profiles/{id}/channels` | Подключения TG/VK |
| POST | `/creators/profiles/{id}/analyze-own` | Анализ своих соцсетей (150 ₽) |
| POST | `/creators/profiles/{id}/analyze-competitor` | Анализ конкурента (200 ₽) |

## Scheduler (новые cron'ы)

В [server/scheduler.py](server/scheduler.py) добавить:
- `creators_prepare_loop` — раз в час: items с `schedule_at - now < 24h` и status=planned → Шаг B
- `creators_publish_loop` — раз в 5 минут: items со status=ready и schedule_at <= now → Шаг C

## Безопасность

- TG bot tokens, VK community tokens — EncryptedString в `channel_connections.token`
- Rate-limit на `/calendar/generate` (1 запрос/мин/юзер) — Sonnet дорогой
- SSRF в analyze-competitor URL — DNS + CIDR block + scheme whitelist (как везде)

## План итераций

1. **Итерация 1 (сейчас):** Модели + миграция + `/creators.html` страница (без календаря — список карточек) + endpoints профилей + endpoint «сгенерировать план» + первый pipeline Sonnet → 30 items + один анализ-пилот «свой профиль».
2. **Итерация 2:** Календарь UI (CSS-grid, цвета по типу, фильтр платформ).
3. **Итерация 3:** Scheduler `prepare_loop` (Perplexity для актуальных + DALL-E для картинок).
4. **Итерация 4:** TG-подключение + автопостинг.
5. **Итерация 5:** VK-подключение + автопостинг.
6. **Итерация 6:** Анализ конкурента + drag-n-drop карточек.

## Что нужно решить с юзером до старта

См. ниже — задаю вопросы.
