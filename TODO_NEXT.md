# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-04-28 (после спринтов «КП-конструктор», «Презентации v2», «Три приложения»)_

## ✅ Закрыто за последние спринты (см. HANDOVER.md)

### КП-конструктор (большой спринт)
- ✅ Разделили КП и Презентации на два модуля (`/proposals.html`, `/presentations.html`)
- ✅ Бренды (`ProposalBrand`) — лого, цвета, шрифт, реквизиты, tagline, usp_list, guarantees, tone, intro/cta phrases
- ✅ Прайс-листы (`ProposalPriceList` + `ProposalPriceItem`) — отдельный модуль, не из бота, с CSV-импортом
- ✅ JSON-first генерация (Claude → JSON слотов → backend рендерит в HTML по фиксированному шаблону)
- ✅ 4 пресета оформления (minimal/classic/bold/compact)
- ✅ Парсинг сайта клиента (SSRF-safe) для контекста
- ✅ Генерация PDF через xhtml2pdf + DejaVu Sans (кириллица)
- ✅ 5 семейств шрифтов в PDF (Liberation Sans/Serif, Noto Sans/Serif установлены apt'ом)
- ✅ 8 готовых палитр B2B
- ✅ WYSIWYG-правка (contenteditable=true на body, медиа отключены)
- ✅ AI-правка одной секции (real × 5)
- ✅ Версионирование (до 10 версий, restore)
- ✅ Дублирование КП
- ✅ CRM-стадии (new/sent/opened/replied/won/lost) + воронка-индикатор + фильтры
- ✅ Email threading (IMAP-watcher парсит In-Reply-To → ProposalProject.outbox_message_id)
- ✅ Публичная ссылка `/p/{token}` без auth
- ✅ Ручная отправка по email + auto_proposal нода
- ✅ Pre-approval mode для auto-режима (TG-уведомление вместо отправки)
- ✅ Whitelist в auto: keywords + email-домены
- ✅ Подпись (signature_url) в подвале PDF
- ✅ Pre-validation до списания

### Презентации v2 (полная переработка)
- ✅ Новые поля PresentationProject: topic/audience/slide_count(3-40)/extra_info/bg_color/text_color/accent_color/title_color/client_site_url/custom_charts/slides_json/pptx_path/html_preview/pdf_path
- ✅ Claude prompt v3 → JSON со слайдами 7 типов (title/section/content/two_column/chart/quote/cta)
- ✅ Backend рендерит HTML preview (карусель + SVG-графики bar/line/pie)
- ✅ PPTX через python-pptx (нативные графики, speaker notes, картинки скачиваются)
- ✅ PDF через xhtml2pdf (landscape A4)
- ✅ Color picker × 4 (фон/акцент/заголовки/текст) + 4 быстрых пресета
- ✅ Vision-описания загруженных фото через Claude Haiku
- ✅ URL клиентского сайта → парсинг → стиль/тон в prompt
- ✅ Графики с явными данными (kind/labels/values) с страховкой добавления
- ✅ ТЗ-визард (`/brief-assist`) — Claude Haiku из «грубой идеи» делает структурированный JSON
- ✅ Margin ×7 внутри (presentation.margin_pct=700), но в UI не показывается
- ✅ Динамическая цена в UI (debounced)

### Три приложения (PWA + Desktop + TG-бот)
- ✅ PWA: manifest.json + service worker + icon.svg
- ✅ Install-prompt API (window.aiShowInstall) с кросс-платформенной инструкцией
- ✅ Кабинет → вкладка «📲 Приложение»
- ✅ Desktop standalone-режим: draggable titlebar в `display:standalone`
- ✅ TG management-бот (`server/tg_management.py`)
- ✅ Привязка через 6-знач код или deep-link
- ✅ Команды: /start /link /unlink /me /stats /menu + inline-кнопки
- ✅ Push-уведомления при отправке КП / новой заявке / ошибках
- ✅ Toggle подписок (proposals/records/errors)
- ✅ Webhook с двойной защитой (path_secret + X-Telegram-Bot-Api-Secret-Token)

### Security audit
- ✅ uvicorn → 127.0.0.1, UFW + fail2ban + nginx server_tokens off
- ✅ Password policy 10+ симв с чёрным списком
- ✅ Login alert email при новом IP
- ✅ Path traversal в ZIP-экспорте
- ✅ CSV-injection (records.csv + transactions.csv)
- ✅ CSV-import: верхняя граница 1 млрд ₽
- ✅ `_SecretFilter` на root-handler (был только на httpx/openai/anthropic)
- ✅ Storage billing race fix
- ✅ P0 регрессия `/transactions.csv` (декоратор)
- ✅ pip-audit: 11/12 CVE закрыто

### UX правки
- ✅ Cyrillic в PDF через DejaVu Sans
- ✅ Custom modals (aiAlert/aiConfirm/aiPrompt) во всех 8 views
- ✅ WYSIWYG-режим в редакторе сайтов и КП (body-level contenteditable)
- ✅ Замена картинок и иконок (SVG/FA/Lucide) в редакторе сайтов
- ✅ Analytics N+1 fix (9 SQL → 4 SQL)
- ✅ LRU embedding cache
- ✅ Sites polling final fetch (защита от tab-suspend)
- ✅ TestUserApiKeys + TestBotPriceList (89/89 тестов)

---

## 🟡 На очереди (в порядке убывания пользы)

### 1. Юзер должен зарегистрировать TG management-бот
**TG-бот реализован, но не запущен на проде** — нужны действия юзера:
1. Создать бот через @BotFather → `/newbot` → получить токен
2. Добавить в `.env`:
   ```
   TG_MGMT_BOT_TOKEN=1234567890:AAH...
   TG_MGMT_BOT_USERNAME=aiche_mgmt_bot
   ```
3. Установить webhook (одна команда):
   ```bash
   # SECRET = tg_webhook_secret(TOKEN) — берём из python:
   /root/AI-CHE/venv/bin/python -c "
     import os; os.environ['JWT_SECRET']='<реальный>'
     from server.security import tg_webhook_secret
     print(tg_webhook_secret('<TOKEN>'))
   "
   curl -F url=https://aiche.ru/webhook/tg-mgmt/SECRET \
        -F secret_token=SECRET \
        https://api.telegram.org/botTOKEN/setWebhook
   ```
4. Рестарт сервиса и попробовать привязку через кабинет → Настройки

### 2. Видео-туториалы в UI (после получения GIF от юзера)
Список из 15 GIF-туториалов в `docs/gif_tutorials.md`. Юзер сам снимает MP4. Когда положит в `views/static/tutorials/<slug>.mp4` — встроить lightbox с автоплеем в empty-state'ы и при кнопке «📺 Как это работает».

### 3. Голоса РФ-каналов → реальная разработка
Юзер видит блок «🔮 Скоро» в модалке настроек бота. Раз в неделю смотреть статистику голосов:
```bash
ssh root@194.104.9.219 'cd /root/AI-CHE && /root/AI-CHE/venv/bin/python -c "
from server.db import db_session
from server.models import ActionLog
from sqlalchemy import func
with db_session() as db:
  rows = db.query(ActionLog.target_id, func.count(ActionLog.id)).filter(ActionLog.action==\"user.feature_vote\").group_by(ActionLog.target_id).all()
  for r in sorted(rows, key=lambda x: -x[1]): print(f\"  {r[0]}: {r[1]}\")"'
```

Кандидаты по простоте интеграции:
- **WhatsApp** через Wazzup24 (российская инфра, без VPN)
- **Одноклассники** — OK Bot API
- **Email-канал** — IMAP уже частично есть как trigger
- **JivoSite** — webhook + REST

### 4. starlette upgrade (security)
Pinned в FastAPI 0.111. CVE-2024-47874, 2025-54121. Нужен апгрейд FastAPI до 0.115+ → нужно проверить breaking changes (Depends-сигнатура, lifespan, middleware-API). Отдельным аккуратным спринтом.

### 5. 2FA для админки
TOTP через `pyotp`. Дополнительный шаг при логине admin@. Отдельным спринтом.

### 6. Cloudflare/CDN+WAF
Сейчас прямой запрос в Дронтен NL. Cloudflare даст:
- Базовый DDoS-протекшн
- WAF rules (можно купить free plan)
- Кэш статики (всё что не /api/*)

Нужен лишь DNS-редирект на CF и origin server pull настройка.

### 7. Web Push API (через VAPID)
Push-уведомления в браузер без TG. Нужно:
- Сгенерить VAPID-ключи
- В sw.js уже есть push-handler — добавить subscription в БД
- Endpoint `/user/push/subscribe` для получения PushSubscription от браузера
- При событии — `pywebpush` шлёт уведомление

### 8. Архивация asset'ов при просрочке оплаты
Логика в scheduler.py уже работает (7д grace + 37д до удаления). Нужен прогон в production-режиме чтобы убедиться что cutoff правильный.

### 9. PPTX/PDF: ещё лучше оформление
Сейчас слайды простые. Можно добавить:
- Декоративные элементы (gradient overlays, фигуры)
- Анимации в PPTX (через python-pptx slide transitions)
- Иконки секций (📋/⏱/🛡/✅) в зависимости от preset'а
- Hero с фоновой картинкой/паттерном

### 10. Standalone .exe / .dmg / .AppImage (Electron)
Если PWA-режима мало — обернуть в Electron. Будет полноценная нативная сборка с auto-update через GitHub releases. Defer.

### 11. Native push в TG management-боте
Сейчас push при отправке КП/новой заявке. Можно расширить:
- Голосовой ввод задач (TG voice → Whisper → команда)
- Генерация КП по описанию прямо в чате с ботом
- Уведомления о низком балансе
- Уведомления о новых регистрациях/платежах для админа

### 12. Прочие мелочи
- **MAX inline-кнопки** реально протестировать с живым ботом
- **systemd `User=aiche`** — миграция на отдельного юзера от root
- **localStorage.obs_token** окончательно убрать (после периода миграции на cookie)
- **Голос Google API ключ** засветился в старых journalctl до фильтра `_SecretFilter` — ротировать `AIza...` при удобном случае
- **Embeddings перевод на pgvector / FAISS** при росте прайсов >500 позиций (сейчас linear scan по cosine достаточно)

---

## 📋 Заметки для следующей сессии

- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing` без редеплоя. Список ключей и дефолтов — в `server/pricing.py:DEFAULTS`.
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
- **Прайс-листы для КП** — вкладка «📋 Прайсы» в `/proposals.html`
- **Прайс-лист бота** — кнопка `₽` в карточке бота в `/chatbots.html` (отдельно от КП)
- **No OAuth keys** — `GOOGLE_CLIENT_ID/VK_CLIENT_ID` в env пока пусто
- **YooKassa тестовый** — для прода нужен live shop_id
- **Видео Kling/Veo/Nano** — модели работают, но скрыты из UI MODELS array
- **Native dialogs запрещены** — везде использовать `aiAlert/aiConfirm/aiPrompt` из icons.js
- **WYSIWYG-редактор** — стандарт для всех редакторов (sites + proposals): `contenteditable=true` на body, медиа отключаются
- **Margin ×7 для презентаций** — внутри presentation_builder, в UI не показывается
- **JSON-first генерация для КП** — AI возвращает данные, не HTML. Шаблон/шапка/подвал стабильные.
