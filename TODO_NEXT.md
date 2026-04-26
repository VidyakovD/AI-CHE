# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-04-26 (после security audit + harden спринта)_

## 🔴 Security debt после P0/P1 спринта

### JWT в localStorage → httpOnly cookie + CSRF (defer)
**Зачем:** сейчас XSS = моментальный угон сессии (token читается JS). httpOnly cookie + CSRF-токен в заголовке = XSS получает только короткое окно.

**Где трогать:**
- `server/auth.py` — добавить `set_access_cookie(response, token)` хелпер.
- `server/routes/auth.py` — `/login`, `/register/verify-email`, `/oauth/exchange` — устанавливать `Set-Cookie: access_token=...; HttpOnly; Secure; SameSite=Lax; Path=/`.
- `server/routes/deps.py:current_user` — читать токен сначала из cookie, потом из Authorization (back-compat).
- Новый middleware: для всех `POST/PUT/DELETE/PATCH` требовать `X-CSRF-Token` совпадающий с cookie `csrf_token` (double-submit pattern).
- Все views — убрать `localStorage.setItem('obs_token')`, читать csrf-token из cookie через JS, добавлять в `fetch` header.
- `/auth/logout` — очистка cookie через `Set-Cookie: access_token=; Max-Age=0`.

**Why deferred:** инвазивная миграция, требует тестов на back-compat (старые залогиненные клиенты должны продолжать работать), отдельные тесты на CSRF middleware.

### Прочие из аудита (низкий приоритет)
- `tg_webhook_secret()[:32]` оставлено (128 бит — приемлемо, увеличение сломает выставленные webhook'и).
- Tailwind CDN → локальный bundle (300ms latency + supply-chain risk).
- Hardcoded цены `SITE_QUALITY_TIERS`/`CODE_GEN_PREMIUM_COST` — мигрировать в `model_pricing` БД с UI в админке.
- ARIA / a11y — пройтись по основным views.

## 🟡 В разработке / на очереди

### 1. Чат-бот «Конструктор ботов» в TG и MAX 🆕

**Что делаем:** AI-бот, живущий в TG и MAX, который через диалог создаёт
**другие** боты для конкретных бизнес-задач — например запись в салон
красоты, мастерскую, на консультацию. Конструктор сам:

- Спрашивает у клиента (владельца салона) что нужно: тематика, услуги,
  расписание, как принимать оплату/контакты.
- Генерирует workflow для нового бота (текстовое меню, кнопки, переходы).
- Деплоит этого нового бота под вторым TG/MAX токеном клиента.
- Конечный бот умеет:
  - Inline-кнопки (мы уже поддерживаем `output_tg_buttons` в воркфлоу-движке).
  - Сценарные переходы (switch / condition по callback_data).
  - Сохранение записи в storage (имя, телефон, услуга, время).
  - Подтверждение / отмену / напоминание (`trigger_schedule`).
  - Опционально: уведомление владельца о новой записи (`output_tg`).

**Где трогать:**
- `server/agent_runner.py` — добавить агент `bot_builder` с системным
  промптом «ты собираешь TG/MAX-бота под бизнес-задачу».
- `server/workflow_builder.py` — у нас уже есть AI-сборка графа
  по задаче. Может стать ядром.
- `server/chatbot_engine.py` — поддержка inline-кнопок MAX
  (`output_max_buttons`, аналог output_tg_buttons).
- `views/chatbots.html` или новая страница — UX «создать бота через AI».

**Why:** клиент-салон не должен сам собирать workflow в Canvas-конструкторе
— это слишком сложно. Через диалог в TG/MAX — бизнес-юзеру комфортно.

---

### 2. Сайты — ручное редактирование + точечная AI-правка 🆕

**Что делаем:** в редакторе сайта (`views/sites.html` → детальная модалка)
после генерации HTML добавить:

- **Ручное редактирование текста и картинок прямо в превью** (частично
  уже работает через `contenteditable` + `editMode`). Доделать:
  - Стабильное сохранение изменений в исходный `code_html` без срыва
    разметки (сейчас `site:htmlChange` пуляет в textarea, но не всегда
    синхронизируется при `click outside`).
  - Замена картинок через клик: модальное окно «загрузить новую» или
    выбрать из ранее загруженных в проект.
- **Точечная AI-правка по блокам:**
  - Юзер выделяет блок (`section`, `div`, `header` …) → клик правой
    кнопкой / иконка «✨ Изменить через AI».
  - Открывается мини-промпт «что хочешь изменить в этом блоке?».
  - Backend получает ID блока + текущий HTML блока + промпт →
    Claude отдаёт обновлённый блок → подменяем в `code_html`.
  - **Цена:** 5 ₽ за точечную правку (как сейчас iterate).
- **Сохранение перед скачиванием:**
  - Перед `Download HTML` / `ZIP` / `Опубликовать` — финальный коммит
    всех ручных правок в `code_html` через POST `/save-code`.
  - В `code_html` идёт уже отредактированная финальная версия
    (включая заменённые картинки и AI-правки блоков).

**Где трогать:**
- `server/routes/sites.py` — новый endpoint
  `POST /sites/projects/{id}/edit-block` (cost 500 коп = 5 ₽):
  принимает `block_id`, `block_html`, `instruction` → возвращает обновлённый блок.
- `views/sites.html` — JS:
  - `data-edit-id` атрибуты на каждый top-level блок при рендере iframe.
  - Hover-toolbar над блоком: «✨ AI-правка», «🖼 Заменить фон/картинку».
  - Перед download/publish — `finalizeEdits()` собирает HTML из iframe
    и POSTит в `/save-code`.
- `server/chatbot_engine.py` уже не нужен — это про сайты.

**Why:** клиент хочет менять отдельные секции точечно, а не
гонять весь HTML через Claude (5 ₽ за блок гораздо дешевле, чем 1500 ₽
за полную регенерацию).

---

### 3. Доделки по картинкам (минор)

- Размер 1024×1536 / 1536×1024 — проверить что реально применяется
  при `images.edit` (gpt-image-1). Если игнорится — открыть issue в OpenAI
  cookbook или добавить fallback на images.generate с перерисовкой по описанию.
- Lightbox / hover-кнопки могут не работать если у юзера старый кэш —
  всегда инструктировать `Ctrl+Shift+R`.

---

### 4. PDF бизнес-решений — мелкие косметика

- xhtml2pdf варнинг `getSize: Not a float '0.4em'` — поправить CSS,
  заменить em на pt в `_BRAND_CSS` (server/pdf_builder.py).
- Добавить логотип на обложку (если поднимем `/static/logo.png`).
- Пагинация: проверить разрыв страниц перед `<h1>`/`<h2>`.

---

## ✅ Сделано (хвост за апрель 2026)

### Большой рефакторинг 2026-04-25
- ❌ Подписки убраны полностью (PLANS, /payment/create, _sub_dict, subscription tab)
- 💰 CH (внутренняя валюта) → копейки. Миграция × 10 на проде применена.
- 📋 Оферта.txt + terms.html переписаны (раздел 4 «Оплата», 5 «Баланс»)
- 🟥 Интеграция MAX (botapi.max.ru) — TG/VK/Avito/Widget + MAX
- ✨ AI-сборка воркфлоу по описанию через Claude
- 🔧 Фиксы безопасности из аудита: чат-leak, шифрование токенов,
  YooKassa HMAC, /message требует auth, condition word-boundary,
  orchestrator JSON parsing, env-token fallback убран
- 🎨 Картинки: gpt-image-1 + DALL-E, edit-by-reference (до 10 фото),
  выбор формата, lightbox + hover-кнопки, цена 15 ₽ (маржа ~3-4×)
- 🌐 Сайты: фикс 1500 ₽, чат по ТЗ через GPT-4o, генерация HTML через
  Claude с auto-continue (max_tokens=16000), viewport-resizer,
  привязка чат-бота, ресайз модалки
- 📄 КП/Презентации: фикс 50/100 ₽ + 5 ₽ правки, картинки upload,
  привязка чат-бота
- 💼 Бизнес-решения: фикс 50/100 ₽, Markdown→PDF (xhtml2pdf) с
  фирменными стилями + обложка + footer
- 🔑 OpenAI/Anthropic/Grok ключи — прямые, прокси awstore удалён
- 💸 Пакеты пополнения: 500 ₽ / 1000+50 ₽ / 3000+250 ₽
- ⚡ nginx proxy_read_timeout 600s для долгих Claude-запросов
- 🐛 Фиксы: картинки сохранялись в server/uploads вместо /uploads,
  fetchMe возвращал {user:...} вместо user, баланс не обновлялся
  на главной (auto-refresh каждые 30 сек + focus + visibilitychange)

---

## 📋 Заметки для следующей сессии

- **Нет ключей для OAuth Google/VK** — код готов, ждёт `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `VK_CLIENT_ID`, `VK_CLIENT_SECRET`.
- **YooKassa тестовый shop** — для прод-платежей нужен live shop_id и secret.
- **Видео-модели (Kling, Veo, Nano)** — временно скрыты из UI, но код живёт.
  При желании вернуть — добавить в `MODELS` массив в index.html.
