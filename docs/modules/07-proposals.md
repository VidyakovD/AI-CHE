# Модуль 07 — Proposals (КП)

> **Что это:** Коммерческие предложения. 4 пресета дизайна + 4 шапки + бренды юзера + прайсы + JSON-first генерация + WYSIWYG + AI-правка секций + версии + **электронная подпись клиентом (canvas + audit-trail + auto-CRM `won`)** + email-orchestrator. Open когда: чинишь HTML/PDF КП, добавляешь пресет, работаешь с подписью.

## TL;DR

- **Builder:** [server/proposal_builder.py](server/proposal_builder.py) (1235 строк) — `parse_client_site`, JSON-first prompt v3, `_render_proposal_json` → HTML, 4 `_PRESET_CSS`, 4 header_layout, `edit_section`, bleach-санитизация.
- **Routes:** [server/routes/proposals.py](server/routes/proposals.py) — brands CRUD, projects CRUD, generate, public-link, send-email, stage, price-lists.
- **UI:** [views/proposals.html](views/proposals.html) (1995 строк) + блок «Подписано клиентом» + auto-save черновика + cost-hint + drag-n-drop CSV прайса.
- **Public-страница:** [main.py](main.py) `/p/{token}` (inline HTML+JS, 150 строк — _TODO: вынести в Jinja-template_).
- **Подпись:** canvas → POST `/p/{token}/sign` → SHA-256 hash от `proposal_id + name + email + ts + sig + ip` → `ProposalSignature` + audit-log.

## Цена

- **50 ₽** первая генерация (ключ `proposal.create=5_000`) — ⚠ хардкод `PROPOSAL_COST_KOP=5000` в route, не читает pricing_config (_TODO: fix_).
- **5 ₽** перегенерация (ключ `proposal.edit=500`).
- **real × 5** AI-правка секции (ключ `ai.improve_margin_pct=500`).

## Модели

| Таблица | Поля важные |
|---|---|
| `proposal_brands` | user_id, name, logo_url, colors_json, fonts_json |
| `proposal_projects` | user_id, brand_id, client_name, client_email, client_site, generated_html, public_token, header_layout, status (draft/sent/opened/signed/won/lost), opened_at, sent_at, signed_at |
| `proposal_versions` | snapshot для отката (retention top-10) |
| `proposal_signatures` | proposal_id, name, email, signature_data_url (canvas PNG), ip, hash_sha256 — анти-подделка |
| `proposal_price_lists` | юзерские прайсы для подстановки |

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/brands` | Список брендов юзера |
| POST/PUT/DELETE | `/brands` | CRUD |
| GET | `/projects` | Список КП |
| POST | `/projects` | Создание + генерация (50 ₽) |
| PUT | `/projects/{id}/regenerate` | Перегенерация (5 ₽) |
| POST | `/projects/{id}/edit-section` | AI-правка секции (real × 5) |
| POST | `/projects/{id}/public-link` | Создать `public_token` |
| POST | `/projects/{id}/send-email` | Отправить клиенту (тут email-валидация **слабая**: только `"@" in to` — _TODO: использовать EMAIL_RE_) |
| PUT | `/projects/{id}/stage` | sent / opened / won / lost (CRM-стадии) |
| GET | `/projects/{id}/versions` | Версии |
| POST | `/projects/{id}/restore/{version_id}` | Откатить |
| GET | `/price-lists` | Список |
| POST | `/price-lists` | Upload CSV |

**Public:**
| Метод | Endpoint | Что |
|---|---|---|
| GET | `/p/{token}` | HTML-страница КП (для клиента) |
| GET | `/p/{token}/pdf` | PDF |
| POST | `/p/{token}/sign` | Подпись |

## JSON-first prompt v3

`prompt_template` в [server/proposal_builder.py](server/proposal_builder.py) просит Claude вернуть **JSON-структуру** (sections, items, prices), а не HTML. Backend рендерит HTML через `_render_proposal_json` с шаблонами для 4 пресетов + 4 шапок. Преимущество: можно менять CSS не дёргая LLM.

⚠ **`max_tokens=6000` хардкод** ([server/proposal_builder.py:1155](server/proposal_builder.py)) — для длинных прайсов >50 позиций JSON обрезается. _TODO: `6000 + len(price_text)//4`._

## Подпись клиентом (e-signature)

Поток:
1. Клиент открывает `/p/{token}` → видит КП → кнопка «Подписать».
2. Canvas-блок → клиент рисует подпись → имя + email.
3. POST `/p/{token}/sign` с `signature_data_url` + name + email.
4. Backend: проверяет анти-fraud (rate-limit, размер, image MIME), генерирует **SHA-256 hash** от `proposal_id + name + email + ts + sig + ip` → невозможно подделать (`0a8bdf7`).
5. ProposalSignature row + audit-log `proposal.signed` + auto-stage `signed`/`won` + webhook event `proposal.signed`.

## Бренды

- Логотип хранится как public URL (`/uploads/...`), **whitelist на схемы**: только http/https/data:image/ (`a057a20`).
- Цвета: primary, accent (для пресетов).
- Шрифты: дропдаун с локальными вариантами (Golos Text по умолчанию).

## Email-orchestrator

[server/email_service.py](server/email_service.py) — SMTP Yandex 360 (`smtp.yandex.ru:465 SSL`). Кириллица в From/Subject **обязательно** через `_encode_address_header()` (RFC2047), иначе Yandex 550 sender rejected (`5de8aab`).

`send_with_attachment` → отправляет КП клиенту с PDF-прикреплением.

## Безопасность

- ✅ **bleach-санитизация generated_html** перед сохранением в БД (`dc7eecf`)
- ✅ **WYSIWYG iframe `allow-scripts` — РИСК для старых записей до bleach-фикса** → migration script `scripts/sanitize_legacy_proposal_html.py` (`65fd341`)
- ✅ **SHA-256 подпись** — невозможно подделать
- ✅ **Image URL whitelist** logo/cover/signature
- ✅ **Public page rate-limit** (`d13cb7e`)

## Гочча / TODO

См. [TODO_NEXT.md](TODO_NEXT.md) → раздел КП:
- Email-валидация формальная (только `"@" in to`)
- Кириллица в filename PDF (`filename*=UTF-8''…`)
- `PROPOSAL_COST_KOP=5000` хардкод
- Public proposal page как inline f-string → вынести в Jinja-template
- Счётчик повторных открытий (`open_count`)
- `max_tokens=6000` хардкод
- Snapshot-version race на retention
- **«Напомнить клиенту» cron** — sent_at > 3 дня и opened_at IS NULL → auto-followup (идея)

## Тесты

- `tests/test_critical_paths.py` — proposal flow
- `tests/test_new_features.py` — signature

## Зависимости

- [02-billing](02-billing-payments.md) — списания
- [03-ai-core](03-ai-core.md) — Claude генерация
- [13-public-api](13-public-api.md) — webhook events `proposal.opened/sent/signed`
- [15-crm](15-crm.md) — auto-stage won
- [14-mcp-server](14-mcp-server.md) — `generate_proposal` tool через MCP
- [16-storage](16-storage.md) — логотипы брендов
