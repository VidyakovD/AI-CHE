# HANDOVER — для нового AI-ассистента

Если ты впервые в этом проекте — после `CLAUDE.md` прочитай этот файл. Тут **состояние на 2026-04-26 после последнего большого спринта**.

## Кто юзер и что делаем
- Юзер — Денис, владелец `aiche.ru`. **B2B AI-платформа** для предпринимателей.
- Стек: FastAPI + SQLite + JS SPA. Прод в Нидерландах (Clouvider).
- Юзер — не программист. Общаемся по-русски, понятно, без терминов где можно. Делаешь — катаешь сразу на прод (`git push origin main` → `ssh ... git pull && systemctl restart ai-che`).

## Текущая «фаза» проекта
Платформа умеет:
1. **Чат с AI** (GPT-4o, Claude Sonnet/Opus, Perplexity, Grok, Imagen, Veo) — баланс в копейках, списание per-token + per-request
2. **Бизнес-решения** — 30 готовых промптов с фикс-ценой 50/100 ₽, выдача PDF
3. **Чат-боты для бизнеса** в TG/MAX/VK/Avito/widget — с **6 готовыми шаблонами** (запись/лиды/FAQ/продажи/квиз/контент)
4. **Конструктор сайтов** — фоновая генерация, два tier (Sonnet 1500₽ / Opus 1990₽)
5. **КП и презентации** — 50/100 ₽ с PDF
6. **AI-агенты** с очередью

Финансы: Welcome 50₽, реферал 10%. Платежи через ЮKassa (тестовый shop). Деплой ручной.

## Что было сделано в последних сессиях

### Спринт 1: «Аудит безопасности и багов»
Вышел отчёт с 1 CRIT, 4 HIGH, 7 MEDIUM. Все CRIT/HIGH закрыты:
- Welcome / referral бонусы — atomic gates через новые поля `User.welcome_bonus_claimed_at`, `User.referral_signup_bonus_paid_at` + UNIQUE-индекс на `transactions.yookassa_payment_id`
- Widget XSS — `bot.name` через `json.dumps` + `textContent` вместо innerHTML
- Worker_lock fail-CLOSED (был fail-OPEN — мог дублировать задачи)
- TG webhook требует `X-Telegram-Bot-Api-Secret-Token`
- HKDF вместо sha256 для Fernet-ключа в `secrets_crypto`
- SVG-аплоады — фильтр `<script>`/`onload=`
- Виджет injection в HTML — `rfind("</body>")` + fallback на `</html>`
- Виджет WebSocket — Origin-whitelist через `ChatBot.widget_allowed_origins`

### Спринт 2: «Конструктор бизнес-ботов»
- Новая таблица **`bot_records`** (lead/booking/order/quiz/ticket/subscriber)
- Новая нода **`save_record`** в workflow
- 5 новых нод для богатого UX: `request_contact`, `request_location`, `output_photo`, `edit_message`, `chat_action_typing`
- `output_max_buttons` (mirror `output_tg_buttons` для MAX)
- Авто-`setMyCommands` при деплое TG-бота (`/start /help /menu`)
- Conversation memory переехала из in-memory dict в **`bot_conversation_turns`** (переживает рестарт, multi-worker safe)

### Спринт 3: «6 шаблонов ботов + UX-обвязка»
- `server/bot_templates.py` — 6 шаблонов (lead_capture, sales_warmup, faq_support, booking, quiz_funnel, content_broadcast)
- Каждый шаблон имеет `customizable` поля (название компании, услуги, и т.д.) → подставляется в workflow через `{{key}}`
- Endpoint `POST /chatbots/from-template/{slug}` + публичный `GET /chatbots/templates`
- На `/chatbots.html`: трёхпутный chooser «Шаблон / AI-конструктор / С нуля»
- В карточке бота 4 новых кнопки: 👁 Превью · 📊 Аналитика · 📋 Записи · ✨ Доработать через AI
  - **Превью** — мини-чат с ботом в песочнице (не списывает, не сохраняет records)
  - **Аналитика** — диалоги/входящие/конверсия + SVG bar chart + топ-вопросы
  - **Записи** — таблица заявок с фильтрами + CSV-экспорт
  - **AI-доработка** — юзер пишет «сделай тон строже» → workflow_builder правит граф

### Спринт 4: «Imagen 4 + Veo 3 через прокси»
- Google AI Studio блочит наш ASN — добавлен `GOOGLE_HTTPS_PROXY` env
- Imagen 4 (fast/std/ultra) с правильными именами моделей. `negativePrompt` deprecated → пихаем в основной prompt
- Veo 2/3.0/3.0-fast/3.1 с асинхронным polling (predictLongRunning)
- Per-model capabilities: audio только Veo3, i2v только Veo3.x, neg только Veo2 — иначе 400
- На `/index.html`: Nano как отдельный таб, Veo с селектом «Качество модели» + «Картинка→видео» режим
- Видео в чате как `<video controls>` + кнопка «Скачать»
- Авто-детект `.mp4`/`.webm` URL → addVideoMsg
- Click-outside сворачивает панель Veo/Nano параметров
- **Auto-refund** в `/message` если ai-видео/картинка вернулись с ошибкой

### Спринт 5: «Сайты — фоновая генерация + 2 tier»
- Synchronous → **async** через `asyncio.create_task` + polling-эндпоинт `/generation-status`
- Pre-process ТЗ через **GPT-4o-mini** (расширяет до 800-1500 слов с цветами/тоном/секциями)
- Anthropic SDK timeout 60→**600 сек**
- Auto-continue до 3 (Sonnet) или **6** (Opus) turns с промежуточным сохранением
- Frontend polling 4 сек × до 10 мин с живым прогрессом «Готово 47 KB, дописываю (4/6)…»
- Два tier: **🟢 Стандарт (Sonnet) 1500₽** или **🟣 Премиум (Opus) 1990₽**
- Радио-кнопки с описанием, динамическая цена на «Создать сайт»

### Спринт 6: «Audit log для AI-ассистента» (текущий)
- Таблица `action_logs` + `server.audit_log.log_action()` helper
- Логирование в register/verify/payment/AI-вызовах/sites/bot CRUD
- Эндпоинты `/admin/actions`, `/admin/actions.txt`, `/admin/actions.jsonl`
- Cleanup в scheduler (info > 30 дней, error > 90)
- Этот HANDOVER.md и обновлённый CLAUDE.md

## Что НЕ сделано (но понятно как)
1. **OAuth Google/VK** — код готов, ждёт `GOOGLE_CLIENT_ID`/`VK_CLIENT_ID` в env
2. **Прод ЮKassa** — сейчас тестовый shop, нужен live shop_id+secret
3. **Bot constructor live в TG/MAX** — endpoint и шаблон workflow есть (`bot_constructor` нода), но не задеплоено как самостоятельный бот в `@aiche_bot_builder`
4. **OAuth-привязка клиента-салона** к платформе — сейчас бот-конструктор создаёт ботов под аккаунтом своего владельца, не под клиента
5. **Mobile responsiveness** — Tailwind есть, но реальную проверку на 375px не делали
6. **DRY рефактор `server/ai.py`** — частично сделан (`try_with_keys`, `_openai_compatible_response`), но 14 *_response функций ещё дублируются

## Что обычно ломается
1. **Google AI Studio 429 «prepayment depleted»** — у юзера закончились кредиты в Google Cloud билле. Решение: пополнить на ai.studio. Auto-refund в `/message` уже работает — деньги клиента не списываются.
2. **Veo 3.0 fast 503 «Deadline expired»** — квота. Backend делает fallback на 3.1 → 3.0 → 2.0 автоматически.
3. **Anthropic 60 сек timeout** — было до спринта 5, сейчас 600.
4. **Backup в git** — `backups/` теперь в `.gitignore`, но если кто-то добавит файлы — `git add -A` зацепит. Использовать `git add -A ':!backups/'`.

## Как разобраться в новой задаче от юзера
1. **Прочитай эти файлы.** Серьёзно — здесь ВСЁ актуально.
2. **Запроси audit log за нужный период:**
   ```
   curl https://aiche.ru/admin/actions.txt?since_hours=72&only_errors=true \
        -H "Authorization: Bearer <admin token>"
   ```
   Юзер тебе пришлёт.
3. **`git log --oneline -20`** — что трогали недавно.
4. **`grep -rn` или Grep tool** — где живёт фича.
5. Если фича большая — сначала **отвечай планом**, потом делай. Юзер любит «расскажи как сделаешь, потом катай».
6. После деплоя — **подтверди работу** живым curl или скриншотом из preview.

## Стиль работы юзера
- «Делай по порядку», «делай на свое усмотрение» = можешь катать сразу на прод без согласования каждого шага
- «Точечно поправим» = не бойся ошибиться, лучше быстрее
- Любит чёткие списки с эмодзи 🟢🟣 (но не везде — для статусов и tier'ов)
- Не любит вопросы из серии «А что предпочитаете?» — лучше прими решение сам и обоснуй коротко
- При проблеме — пришлёт скриншот, по нему ориентируйся

## Канал коммуникации с юзером в этом чате
- Все правки — сразу `git push origin claude/sleepy-johnson-115434:main` → `ssh ... git pull && systemctl restart ai-che`
- Отчёт после каждого блока — короткий список что сделано + ссылки на файлы (file:line)
- Если что-то новое — сразу обновляй `CLAUDE.md` и (для крупного спринта) `HANDOVER.md`

## Полезные команды
```bash
# Запуск local dev
DEV_MODE=true JWT_SECRET=dev-secret python -m uvicorn main:app --reload --port 8001

# Тесты
python -m pytest tests/

# SSH прод
ssh -i 'C:\Users\Денис\.ssh\id_ed25519' root@194.104.9.219

# Logs прод
journalctl -u ai-che -n 100 --no-pager

# Audit log dump (admin)
curl -H "Authorization: Bearer <admin token>" \
     "https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000"

# Apply migrations + sanity import (local)
DEV_MODE=true python -c "import sys;sys.path.insert(0,'.');from server.db import Base,engine,apply_lightweight_migrations;from server import models;Base.metadata.create_all(bind=engine);apply_lightweight_migrations();import main;print('routes:',len(main.app.routes))"
```
