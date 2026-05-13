# Модуль 21 — Creators

> **Что это:** рабочая зона контент-планирования для бизнеса/SMM. Профиль бренда → AI-контент-план в виде календаря → AI-подготовка каждого поста (текст + опц. картинка) → автопостинг в TG/VK или копирование вручную. Плюс анализ соцсетей (свой + конкурент). Не путать с чат-ботами ([05-chatbots](05-chatbots.md)) — те для общения с КЛИЕНТАМИ.

## TL;DR

- **Главные файлы:** [server/routes/creators.py](server/routes/creators.py) (роутер) + [server/creators_planner.py](server/creators_planner.py) (план) + [server/creators_prepare.py](server/creators_prepare.py) (текст+картинка) + [server/creators_publisher.py](server/creators_publisher.py) (TG/VK send) + [server/creators_vk.py](server/creators_vk.py) (VK API) + [server/creators_analyzer.py](server/creators_analyzer.py) (анализ).
- **UI:** [views/creators.html](views/creators.html), ссылка на главной index.html (sidebar с бейджем NEW).
- **Schedulers** в [server/scheduler.py](server/scheduler.py): `creators_prepare_loop` (5 мин), `creators_publish_loop` (1 мин).
- **Тариф:** Freemium — 3 поста/бренд/мес бесплатно. Дальше: 15-30 ₽ за пост. Анализ соцсетей: 150 ₽ (свой) / 200 ₽ (конкурент). Генерация плана бесплатно.

## Архитектура

### Flow юзера

```
1. Создал бренд (Brand): 8 полей профиля
2. ✨ «Сгенерировать на месяц» → Sonnet раскладывает ~30-50 постов по дням и платформам (bесплатно)
3. Календарная сетка показывает план — цвета по типу, оранжевая обводка для is_news
4. За 24h до публикации scheduler автоматически готовит контент (или юзер жмёт «✨ Подготовить»)
5. Когда status=ready → можно «📤 Опубликовать сейчас» (TG/VK), или scheduler опубликует в schedule_at автоматически
6. Параллельно: «🔍 Анализ своего профиля» / «🕵 Анализ конкурента» — Perplexity+Sonnet отчёт
```

### Шаги pipeline

**Шаг A — Sonnet строит план** ([creators_planner.py](server/creators_planner.py)):
- `_propose_slots` раскидывает слоты по дням и платформам (TG 4/нед, VK 3/нед, YT 1/нед, IG 4/нед, типы с весами)
- Время постинга по МСК с конвертацией в UTC
- Sonnet получает профиль + список слотов → возвращает JSON-array с brief к каждому
- **Бесплатно.** Cooldown 60 мин против абуза.

**Шаг B — Подготовка одного поста** ([creators_prepare.py](server/creators_prepare.py)):
- `is_news=True` → Perplexity `sonar-pro` research → Sonnet writer под платформу
- `is_news=False` → Sonnet writer сразу (учёт tone / stopwords / max_chars платформы)
- `type ∈ {image, reels}` → DALL-E 3 опционально (по флагу `with_image` или auto для image/reels)
- IMAGE_PROMPT-блок отделяется от тела автоматически (regexp)
- **Freemium:** 3 поста / бренд / месяц бесплатно, потом 15-30 ₽

**Шаг C — Публикация** ([creators_publisher.py](server/creators_publisher.py)):
- **TG:** `send_telegram` / `send_telegram_photo` из [server/messaging/senders.py](server/messaging/senders.py)
- **VK:** [server/creators_vk.py](server/creators_vk.py) — `publish_to_vk_wall` с 3-шаговым upload фото (`getWallUploadServer` → POST → `saveWallPhoto`)
- **YouTube / Instagram:** автопостинг **не делаем** в MVP (только генерация, юзер копирует руками)
- При ошибке `fail_count++`; после 10 ошибок → `is_active=false`

**Анализ соцсетей** ([creators_analyzer.py](server/creators_analyzer.py)):
- Perplexity research (`sonar-reasoning-pro` для competitor / `sonar-pro` для own) по URL
- Sonnet writer → markdown-отчёт: главное · сильные · слабые · 5 действий · идеи
- Платформа определяется по URL (`detect_platform`)

## Модели

| Таблица | Поля |
|---|---|
| `creator_brands` | user_id, name, niche, product, audience, tone, topics_json, stopwords, logo_url, **free_posts_used_this_month**, **free_posts_reset_at** |
| `content_calendars` | brand_id, period_start, period_end, status (active/archived), generated_at |
| `content_items` | calendar_id, schedule_at, platform (tg/vk/yt/ig), type (text/image/reels/youtube/poll), is_news, brief, prepared_content_md, prepared_media_url, status (planned/preparing/ready/published/skipped), cost_kop, published_at, manual_override |
| `creator_channel_connections` | brand_id, platform, channel_id, title, **token (EncryptedString 2048)**, is_active, fail_count, last_error_at. UniqueConstraint(brand_id, platform, channel_id) |
| `creator_analysis_runs` | brand_id, target_type (own/competitor), target_url, platform, status (running/done/failed), result_md, cost_kop, error |

Каскадное удаление: `User.delete` → `CreatorBrand.delete` → cascading на calendars/items/channels/analysis.

## Endpoints (всё под `/creators/`)

### Brands

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/brands` | список (лимит 10/юзер) |
| POST | `/brands` | создать |
| GET / PUT / DELETE | `/brands/{id}` | CRUD |

### Calendar / items

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/brands/{id}/calendar` | активный календарь + items |
| POST | `/brands/{id}/calendar/generate` | Sonnet строит план (cooldown 60 мин). **Бесплатно** |
| PUT | `/items/{id}` | сменить дату/brief/тип/news/status |
| DELETE | `/items/{id}` | удалить |
| POST | `/items/{id}/prepare` | подготовка (freemium 3/мес → потом 15-30 ₽). Sync |
| POST | `/items/{id}/publish` | публикация вручную (TG/VK) |

### Freemium

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/brands/{id}/freemium` | `{used, remaining, limit, current_month}` |

### Channels

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/brands/{id}/channels` | список подключений |
| POST | `/brands/{id}/channels` | подключить (TG `verify_tg_channel` / VK `verify_vk_community`) |
| PUT | `/channels/{id}/toggle` | вкл/выкл |
| DELETE | `/channels/{id}` | — |
| POST | `/channels/{id}/test` | тестовое сообщение (TG/VK) |

### Analysis

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/brands/{id}/analyze` | запуск (own 150 ₽ / competitor 200 ₽). Auto-refund при fail |
| GET | `/brands/{id}/analysis` | список запусков |
| GET / DELETE | `/analysis/{id}` | детально/удалить |

## Тарифы (фактические)

| Что | Стоимость | Где |
|---|---|---|
| Генерация плана | бесплатно | Sonnet ~$0.05 reach |
| Подготовка text evergreen | 15 ₽ (после freemium) | Sonnet |
| Подготовка image/reels | 30 ₽ | Sonnet + DALL-E |
| Подготовка news | 25 ₽ | sonar-pro + Sonnet |
| Freemium лимит | 3 поста/бренд/мес | `creator_brands.free_posts_*` |
| Анализ своего профиля | 150 ₽ | sonar-pro + Sonnet |
| Анализ конкурента | 200 ₽ | sonar-reasoning-pro + Sonnet |

⚠ **Пока цены захардкожены** в [creators_prepare.py](server/creators_prepare.py) и [creators_analyzer.py](server/creators_analyzer.py). _TODO: вынести в pricing_config._

## Schedulers

- **`creators_prepare_loop`** ([scheduler.py](server/scheduler.py)) — каждые 5 мин:
  - берёт planned-items с `schedule_at <= now + 24h`, лимит 5 за тик
  - применяет ту же freemium-логику что и ручной prepare
  - если баланс < cost и freemium исчерпан → пропускает (item остаётся planned)
  - worker_lock для multi-worker
- **`creators_publish_loop`** — каждую минуту:
  - берёт ready-items с `schedule_at <= now`, лимит 10
  - только tg/vk (yt/ig — ручной copy/paste, item остаётся ready)
  - worker_lock

## Безопасность

- ✅ **Token EncryptedString(2048)** для TG bot tokens и VK community access_tokens
- ✅ **TG webhook secret** не нужен (мы только постим, не получаем)
- ✅ **VK groups.getById** валидация при подключении (бот должен видеть сообщество)
- ✅ **TG getMe + getChat** валидация при подключении
- ✅ **Rate-limit `/calendar/generate`**: cooldown 60 мин между генерациями
- ✅ **Image URL whitelist** наследуется от storage (`/uploads/` или https)
- ✅ **fail_count auto-disable**: 10 ошибок → канал отключается

## Гочча

- **Цены захардкожены** — стоит вынести в `pricing_config` (как `creators.post_text=1500`, `creators.post_image=3000`, и т.д.).
- **Подготовка sync** в `/items/{id}/prepare` — может занять 15-40 сек. UI показывает loader. Если будет таймаут — переписать на async-task с polling.
- **DALL-E картинка** добавляется только если `with_image=true` или `type in {image, reels}`. Для text-постов картинки нет, даже если хочется визуала.
- **VK 3-шаговый upload** хрупкий: если падает один из шагов, постим без картинки (не падаем целиком).
- **Instagram, YouTube** в MVP — **только генерация контента**, копирование руками. Автопостинг через Meta API из РФ — риск блокировки. YT upload требует OAuth + большой quota.
- **Платформа MAX** не подключена.
- **Один user-account → до 10 брендов**. Для агентств с >10 клиентов — увеличить `MAX_BRANDS_PER_USER` или перейти на воркспейсы.

## TODO (для следующих сессий)

- [ ] Цены в `pricing_config` (key prefix `creators.*`)
- [ ] Drag-n-drop постов по календарю (переносить дату мышкой)
- [ ] Push-уведомление когда пост готов (если канала нет — «скопируй и опубликуй»)
- [ ] Перегенерировать только текст без картинки (отдельная кнопка)
- [ ] Bulk-режим: запустить prepare для всех planned за раз (1 кнопка)
- [ ] Tariff подписка 990 ₽/мес = до 30 постов (v2)
- [ ] YouTube OAuth + upload видео (большой объём, отдельная итерация)
- [ ] Instagram через Meta Business API (риск — отложено)
- [ ] Аналитика опубликованных постов (просмотры/реакции через TG / VK APIs)

## Тесты

В MVP не написаны unit-тесты для creators — нужны (тестировали вручную через UI). _TODO: pytest для `creators_planner` (mock generate_response), `creators_prepare` (freemium counter), `creators_analyzer` (price + URL validation)._

## Зависимости

- [02-billing](02-billing-payments.md) — списания через `deduct_strict` + `credit_atomic` refunds + Transaction log
- [03-ai-core](03-ai-core.md) — `generate_response` для Sonnet + sonar-pro + sonar-reasoning-pro + DALL-E 3
- [05-chatbots](05-chatbots.md) — переиспользуем `messaging/senders.py` для TG send/photo
- [16-storage](16-storage.md) — DALL-E картинки сохраняются как StoredAsset
- [18-privacy](18-privacy-compliance.md) — action_logs для creator.* events
- [20-infra](20-infra-deploy.md) — два scheduler-loop'а в общем worker
