# Модуль 08 — Presentations

> **Что это:** генератор презентаций. PPTX / HTML / PDF, color picker, vision-анализ фото, графики, ТЗ-визард. Open когда: чинишь PPTX-вывод, меняешь шаблоны, добавляешь chart-тип.

## TL;DR

- **Builder:** [server/presentation_builder.py](server/presentation_builder.py) (1088 строк)
- **Routes:** [server/routes/presentations.py](server/routes/presentations.py) — generate, estimate-cost, pptx, pdf, brief-assist
- **UI:** [views/presentations.html](views/presentations.html) (724 строки)
- **Цена:** `real × 7` (margin внутри, в UI не показываем) — ключ `presentation.margin_pct=700`

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/presentations/brief-assist` | AI помогает заполнить ТЗ |
| POST | `/presentations/estimate-cost` | Предварительная цена |
| POST | `/presentations/generate` | Запуск (бронирует баланс) |
| GET | `/presentations/{id}/pptx` | PPTX (python-pptx) |
| GET | `/presentations/{id}/pdf` | PDF (xhtml2pdf) |
| GET | `/presentations/{id}/html` | HTML preview |

## Модели

| Таблица | Поля |
|---|---|
| `presentation_projects` | user_id, brief_json, slides_json, status, generated_pptx_url |
| `presentation_templates` | (зарезервировано) |

## Возможности

- **Vision-анализ фото:** загружаешь фото → Claude Haiku vision описывает → используется в слайде
- **Графики:** matplotlib → PNG → встройка в слайд
- **Color picker:** brand colors переносятся в шаблон PPTX
- **ТЗ-визард:** через `/brief-assist` AI задаёт уточняющие вопросы (как для агента-консультанта)

## Гочча

- **`python-pptx` не установлен на dev (Python 3.14)** — smoke-тесты skipped. На проде Python 3.10 — ок.
- **SSRF в presentation_builder** при загрузке внешних картинок — закрыто DNS + CIDR-блоком (`d13cb7e`).
- **Маржа внутри** — в UI юзеру говорим итоговую цену, без разбиения. Не «случайно» вывести real_cost.

## Тесты

- `tests/test_smoke_builders.py` — smoke что генератор не падает

## Зависимости

- [02-billing](02-billing-payments.md) — списание
- [03-ai-core](03-ai-core.md) — Claude + vision Haiku
- [16-storage](16-storage.md) — итоговый PPTX как StoredAsset
