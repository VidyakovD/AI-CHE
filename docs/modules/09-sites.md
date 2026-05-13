# Модуль 09 — Sites (сайты под ключ)

> **Что это:** AI-генерация сайтов в sandbox-iframe + ZIP-выгрузка + public_token + WYSIWYG edit-режим + patch-based `/iterate`. Open когда: чинишь генерацию, дебажишь sandbox, работаешь с edit-toolbar.

## TL;DR

- **Routes:** [server/routes/sites.py](server/routes/sites.py) (1605 строк) — все endpoints.
- **UI:** [views/sites.html](views/sites.html) (2520 строк) — генератор + редактор + preview.
- **Цена:** Sonnet 1500 ₽ / Opus 1990 ₽ / `/iterate` через pricing_config (`site.iter`).
- **Public preview:** `main.py:/site/{public_token}` — клиент видит без auth, ~160bit token против enumeration.
- **Edit-режим (после длинной саги):** srcdoc + `sandbox="allow-same-origin"` БЕЗ inline-script (parent управляет contentDocument).

## Длинная сага сайта-редактора (2026-05-12/13)

Серия из ~20 коммитов, понять как сейчас работает можно только в контексте:

1. **PWA SW кэшировал старые версии** → юзеры залипали → kill-switch SW (`7f27ee0`).
2. **Code-fence strip** (` ```html ... ``` `) ДО проверки `<` — закрыло ложные refund'ы 1990 ₽ (`3676540`).
3. **`/iterate` переписан → patch-based** (Claude возвращает JSON-patches `{find, replace}` вместо переписывания всего HTML) — 5-10× дешевле (`01b0f2d`).
4. **Edit-режим: srcdoc + allow-same-origin БЕЗ inline-script** — победили CSP (`1991f6e`).
5. **Edit-toolbar:** B/I/U/S + размер 12-64px + цвет текста + цвет фона + align + list + link + undo/redo (`563d972`, `211fae6`).
6. **Scroll position сохраняется** при toggleEditMode.
7. **Edit-режим только по кнопке** — сброс editMode при openProject/closeDetail/switchDetailTab(preview) (`4f09d4a`, `f7c7bc5`).
8. **Autosave не сохраняет edit-артефакты** (`contenteditable`, `__editmode_css`, `data-edit-id`) в БД + migration script (`8665710`).
9. **selectedImg = el.src** (не DOM-объект) (`b66ec46`).
10. **AI-правка блока:** clone DOM без `data-edit-id` перед `outerHTML`.

⚠ Если меняешь edit-режим — изучи всю серию, иначе сломаешь.

## Endpoints (главные)

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/sites/projects` | Список |
| POST | `/sites/projects` | Создание + AI-генерация |
| GET | `/sites/projects/{id}` | Детали + код |
| PUT | `/sites/projects/{id}` | Autosave |
| POST | `/sites/projects/{id}/iterate` | **Patch-based** правка |
| POST | `/sites/projects/{id}/edit-block` | AI-правка конкретного блока (`data-edit-id`) |
| POST | `/sites/projects/{id}/public-link` | Создать public_token |
| GET | `/sites/projects/{id}/zip` | Скачать ZIP |
| GET | `/sites/projects/{id}/chat` | История чата правок |
| GET | `/site/{public_token}` | Public preview (sandbox-iframe в `main.py`) |

## Модели

| Таблица | Поля |
|---|---|
| `site_projects` | user_id, name, code_html, public_token, gen_status (running/done/failed), phase (spec_approved/generating_code/done), model |
| `site_templates` | зарезервировано (нет UI-фичи one-click шаблонов) |

## Безопасность

- ✅ **public_token** ~160bit — нельзя enumerate `project_id`
- ✅ **sandbox-iframe** для public preview (no allow-scripts на чужой код)
- ✅ **bleach** на исходящий HTML (но в edit-режиме srcdoc → allow-same-origin)
- ⚠ **WYSIWYG iframe allow-scripts в режиме правки** — только для **своего юзерского** HTML, изолировано от других юзеров

## Гочча

- **`asyncio.create_task` без ссылки → GC может убить** → теперь сохраняем в `_pending_site_tasks` (`36c6af7`).
- **`_strip_markdown_code_fence` дублируется** в `/iterate` и `/edit-block` (_TODO: вызывать функцию_).
- **Closure-bug в lambda** в [server/routes/sites.py:619, :666](server/routes/sites.py) — `prompt`/`model_id` захватываются по ссылке. Сейчас работает, но при рефакторе легко словить.
- **`/sites/code` мёртвый endpoint** — `site_decode_code` нигде не используется из фронта (можно удалить).
- **Phase `generating_code` после reload вкладки** — openProject уходит в showDone с пустым codeEditor. _TODO: если `gen_status='running'` — перезапустить polling_.

## TODO (см. [TODO_NEXT.md](TODO_NEXT.md))

- Custom-домен через CNAME
- SEO-preview stage (OG-теги, robots, sitemap) за +50 ₽
- Шаблоны сайтов one-click (5-10 готовых ТЗ)
- Auto-flag failed-generation (если >30% за час — email админу)
- Кнопка «Регенерировать» с тем же ТЗ другой моделью
- ETA в loader-е («обычно 2-4 минуты»)
- a11y на radio quality-option
- Sequential `project.id` в физпути → переехать на `public_token`

## Зависимости

- [02-billing](02-billing-payments.md) — 1500/1990 ₽ + iterate
- [03-ai-core](03-ai-core.md) — Sonnet/Opus
- [13-public-api](13-public-api.md) — webhook `site.done` / `site.failed`
- [17-push](17-push.md) — push при done
