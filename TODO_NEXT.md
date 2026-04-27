# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-04-27 (после Bot pricing rework + Price-list spринта)_

## ✅ Закрыто за последние 2 спринта (см. HANDOVER.md)

- ✅ Bot pricing rework: с-нуля бесплатно, AI-create ≥1000 ₽, AI-improve real ×5
- ✅ Реальные диалоги с маржой ×3, edit-block переписан с фикс на real ×5
- ✅ Свои API-ключи юзеров (OpenAI/Claude/Gemini/Grok) с fallback и скидкой 80%
- ✅ Прайс-лист бота с semantic vector search (text-embedding-3-small)
- ✅ MAX полный фикс: Authorization БЕЗ Bearer, secrets_crypto fallback, JWT decode со всеми ключами
- ✅ Mini-browser в превью сайта (back/forward/reload/home через postMessage)
- ✅ Брендовые иконки каналов и AI (canonical из simple-icons.org CC0)
- ✅ Кнопка «🔄 Обновить» на active-боте (redeployBot)
- ✅ Кастомный scrollbar + help-инструкции к токенам каналов
- ✅ Roadmap каналов с голосованием (POST /user/feature-vote)
- ✅ Storage UI в кабинете и в карточке бота
- ✅ Self-hosting export workflow (ZIP + README)
- ✅ Sites: auto-save + узкое превью fix + GPT enhance улучшен
- ✅ Bot constructor: GPT→Claude pipeline, library блоков, френдли entry-point
- ✅ Security: 11 P0 + 25+ P1 закрыты в двух аудит-спринтах
- ✅ JWT в httpOnly cookie + CSRF (double-submit) с back-compat
- ✅ Pricing config в БД (заменил hardcoded), FK CASCADE, pagination, ARIA проход

---

## 🟡 На очереди (в порядке убывания пользы)

### 1. Видео-туториалы в UI (после получения GIF от юзера)

Список из 15 GIF-туториалов сделан в `docs/gif_tutorials.md`. Юзер
самостоятельно снимет MP4 (приоритет 1: bot-from-template, bot-via-ai,
connect-tg-token, lead-magnet-upload, site-generate). Когда положит в
`views/static/tutorials/<slug>.mp4` — встроить lightbox с автоплеем
в empty-state'ы и при кнопке «📺 Как это работает».

### 2. Голоса РФ-каналов → реальная разработка

Юзер видит блок «🔮 Скоро» в модалке настроек бота. Раз в неделю
смотреть статистику голосов:
```bash
ssh root@194.104.9.219 'cd /root/AI-CHE && /root/AI-CHE/venv/bin/python -c "
from server.db import db_session
from server.models import ActionLog
from sqlalchemy import func
with db_session() as db:
  rows = db.query(ActionLog.target_id, func.count(ActionLog.id)) \
           .filter(ActionLog.action==\"user.feature_vote\") \
           .group_by(ActionLog.target_id).all()
  for r in sorted(rows, key=lambda x: -x[1]): print(f\"  {r[0]}: {r[1]}\")
"'
```

Кандидаты по простоте интеграции:
- **WhatsApp** через Wazzup24 (российская инфра, без VPN, простой REST API)
- **Одноклассники** — OK Bot API (от VK Group, похож на VK)
- **Email-канал** — IMAP уже частично есть как trigger (`server/email_imap.py`)
- **JivoSite** — webhook + REST (если их API живой)

### 3. Архивация asset'ов при просрочке оплаты

Сейчас если баланс кончился — `_storage_billing_tick` пропускает списание.
Нужно: после N дней (например 7) без оплаты → `is_active=False`, файлы
остаются на диске но недоступны через public URL. Через 30 дней —
физическое удаление. Базовая логика готова в scheduler, но не активирована
в production-режиме (cutoff даты сейчас грейс-период).

### 4. Pure self-hosted standalone-runtime

**Сделано**: экспорт workflow в ZIP (для импорта в другую AI Студию Че).
**НЕ сделано**: standalone-движок отдельно от платформы.
**Why deferred**: `chatbot_engine.py` = 110KB с многими зависимостями
(БД, шифрование, AI-провайдеры). Реальный self-host = форк всего репо.
**Решение**: договариваться с enterprise-клиентами индивидуально.

### 5. Tailwind CDN → локальный bundle

Сейчас pinned `cdn.tailwindcss.com/3.4.0` (защита от breaking changes,
но supply-chain риск остаётся). Полный self-host требует node на dev:
- `npx tailwindcss -i src.css -o views/static/tailwind.min.css --minify`
- Конфиг с custom-colors (primary #ff8c42 и т.д.) уже задокументирован
  в `scripts/build_tailwind.md`
- Bundle с tree-shaking ~30-50 KB вместо 50 KB JIT runtime

Defer пока на dev-машине нет node.

### 6. Прочие мелочи

- **MAX inline-кнопки** реально протестировать с живым ботом (код есть,
  юзер ещё не подключил продакшн-бот)
- **systemd `User=aiche`** — миграция на отдельного юзера от root
  (команды в комментариях `ai-che.service`)
- **localStorage.obs_token** окончательно убрать после периода миграции
  (когда все access-токены истекут — 1+ день после деплоя cookie)
- **Голос Google API ключ** засветился в старых journalctl до фильтра
  `_SecretFilter` — ротировать `AIza...` при удобном случае
- **Embeddings перевод на pgvector / FAISS** при росте прайсов >500 позиций
  (сейчас linear scan по cosine similarity достаточно)

---

## 📋 Заметки для следующей сессии

- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing`
  без редеплоя. Список ключей и дефолтов — в `server/pricing.py:DEFAULTS`.
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
  (`/index.html` → аватар → cabTab('apikeys'))
- **Прайс-лист бота** — кнопка `₽` в карточке бота в `/chatbots.html`,
  CSV import + reembed
- **No OAuth keys** — `GOOGLE_CLIENT_ID/VK_CLIENT_ID` в env пока пусто
  (код готов, ждёт регистрации в Google/VK)
- **YooKassa тестовый** — для прода нужен live shop_id
- **Видео Kling/Veo/Nano** — модели работают, но скрыты из UI MODELS array
