# 🧠 Память Claude по проекту AI Студия Че

**Для следующей сессии Claude:** прочитай эти файлы в начале, чтобы войти в контекст
за 2 минуты вместо 2 часов исследования.

Репо: https://github.com/VidyakovD/AI-CHE
Прод: https://aiche.ru
Ветка с этой памятью: `claude/memory` (не мёржить в main, чисто документация).

## 📑 Читай по порядку

| Файл | О чём | Когда нужно |
|---|---|---|
| [01-overview.md](01-overview.md) | Что за проект, кто юзер, бизнес-модель | Всегда — первым |
| [02-architecture.md](02-architecture.md) | Структура файлов, роуты, модули, модели БД | Если работаешь с кодом |
| [03-agents-tab.md](03-agents-tab.md) | Как устроена вкладка ИИ Агенты (три сущности, flow) | Если правишь agents.html или agent_runner.py |
| [04-billing.md](04-billing.md) | Тарификация CH, как считаются цены, атомарные списания | Если трогаешь платежи/биллинг |
| [05-security.md](05-security.md) | Все закрытые CVE, паттерны защиты, чего нельзя делать | Всегда перед security-фиксами |
| [06-deploy.md](06-deploy.md) | SSH на прод, systemd, backup, миграции, env | Когда нужно задеплоить |
| [07-patterns.md](07-patterns.md) | Кодинг-паттерны проекта (deduct_atomic, db_session, …) | Когда пишешь новый код |
| [08-pending-bugs.md](08-pending-bugs.md) | Что не доделано, известные баги, TODO | Когда решаешь «что дальше» |

## 🚀 TL;DR для очень спешащего

- **Стек:** FastAPI + SQLAlchemy + SQLite (`chat.db`), чистый HTML/JS, темная тема.
- **AI:** OpenAI/Claude/Gemini/Perplexity/Grok + DALL-E/Kling/Veo. Anthropic — через прокси awstore.cloud (часто отваливается, TLS).
- **Деньги:** внутренняя валюта CH, 1 CH ≈ 0.10 ₽. Подписки / пакеты / разовые запуски.
- **Ключевые боли:**
  - Anthropic proxy TLSV1_ALERT_INTERNAL_ERROR (периодически)
  - Google/VK OAuth — пользователь не прислал ключи
  - Боевая ЮKassa — пользователь не прислал
- **Язык пользователя:** русский. Отвечать по-русски. Не использовать эмодзи в коде если не просят.
- **Деплой:** `git push main → ssh root@194.104.9.219 → git pull → systemctl restart ai-che`. См. `06-deploy.md`.

## ⚠️ Что НЕ делать

1. Не писать `user.tokens_balance += X` напрямую — только через `server.billing.deduct_*/credit_atomic` (race condition).
2. Не открывать SessionLocal() без `with db_session():` вне FastAPI Depends.
3. Не хардкодить пути с обратными слэшами (Windows-only) — использовать `os.path.join` / `pathlib.Path`.
4. Не трогать `.env` на проде без backup (там live-ключи: OpenAI/Anthropic/ЮKassa).
5. Не коммитить `.env` и `server/.jwt_secret` — они в .gitignore.
6. Не мёржить ветку `claude/memory` в main (чисто документация).

## 💬 Стиль общения с этим пользователем

- Пишет по-русски, часто кратко/неформально.
- Любит чтобы задачи делались **одной большой пачкой** а не по кусочкам.
- Просит «делай всё по очереди / делай все» = делай по списку без уточнений.
- Ценит конкретность: что именно сделано + где посмотреть.
- Не очень технарь — UX-проблемы («а где нажать?») бывают частые.
- При ошибке — даёт лог/скрин → ты диагностируешь и чинишь.

## 📝 Последние коммиты на момент записи

```
762c5c4 fix(orchestrator): бот молчал — single-downstream отключал ветки
556c9b9 feat(agents-ux): убран клик по карточке, тест-модалка, @username
186c0e2 feat(agents-ux): кнопки действий на карточке + 2-CTA wizard
79157e9 fix(orchestrator): keyword-match по границам слов (fix "фер"→оферте)
5c224db fix(workflow): убраны white-квадраты, popover ? теперь виден, нода растёт вниз
f1739cf feat(workflow): undo/redo + шаблоны + импорт/экспорт JSON
6a00ce1 feat(workflow-agents): копирование блоков + auto-resize + подсказки
d1083db fix(agents): восстановлена кликабельность — JS-синтакс был разломан
```

Всего я сделал ~25 коммитов в последнюю сессию — security, UX, pricing,
workflow-фичи, исправления багов. Полная история в git log.
