# Модуль 14 — MCP Server

> **Что это:** JSON-RPC 2.0 endpoint для подключения AI-студии Че как инструмент к Claude Desktop / Cursor / любому MCP-клиенту. 10 tools + 3 resources. Auth через тот же ApiToken что и Public API ([13-public-api](13-public-api.md)).

## TL;DR

- **Route:** [server/routes/mcp.py](server/routes/mcp.py) — endpoint `/mcp` (POST JSON-RPC 2.0).
- **Гайд для юзеров:** [docs/mcp_setup.md](docs/mcp_setup.md).
- **Auth:** `Authorization: Bearer ai_che_***` — тот же ApiToken (без скоупов сейчас, мы доверяем владельцу токена).
- **Адаптация Dolibarr:** `htdocs/ai/server/mcp_server.php`.

## Tools (10 штук)

| Tool | Что делает |
|---|---|
| `get_balance` | Текущий баланс юзера в копейках + рублях |
| `list_solutions` | Каталог 40 пилотов с категориями |
| `run_solution` | Запустить пилот (legacy или v2 input_schema) |
| `get_solution_status` | Статус run + результат |
| `list_proposals` | Мои КП |
| `get_proposal` | Деталь КП |
| `create_proposal` | Создать пустое КП |
| **`generate_proposal`** | **Полная AI-генерация КП по brief'у** (одной командой из Claude Desktop) |
| `list_chatbots` | Список моих ботов |
| `recent_records` | Последние заявки/брони из ботов (стрим CRM-like) |

## Resources (3 штуки)

URI-based ресурсы, MCP-клиент может их подсасывать как контекст:

| URI | Что |
|---|---|
| `aiche://categories` | Категории решений с counts |
| `aiche://pricing` | Текущий pricing_config |
| `aiche://models` | MODEL_REGISTRY snapshot |

## Использование

Юзер в Claude Desktop / Cursor добавляет MCP-конфиг:
```json
{
  "mcpServers": {
    "aiche": {
      "url": "https://aiche.ru/mcp",
      "headers": {"Authorization": "Bearer ai_che_..."}
    }
  }
}
```

После этого Claude может:
- «Покажи мой баланс»
- «Запусти SWOT-анализ для моей ниши онлайн-школ»
- «Сгенерируй КП для клиента X на услуги Y, отправь на email»
- «Последние 10 заявок из ботов с расшифровкой»

## JSON-RPC 2.0 формат

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "get_balance", "arguments": {}}}
```

Response:
```json
{"jsonrpc": "2.0", "id": 1,
 "result": {"content": [{"type": "text", "text": "Баланс: 12 345.67 ₽"}]}}
```

## Гочча

- **Нет scopes сейчас** — токен с access к MCP может всё. Идея: добавить scope `mcp:full` отдельно от обычных read/write.
- **Стабильность:** MCP-клиенты делают **много мелких calls** — кэширование MODEL_REGISTRY/categories внутри endpoint желательно.

## Тесты

- `tests/test_mcp.py` — JSON-RPC, tools, resources (19 тестов)

## Зависимости

- [13-public-api](13-public-api.md) — auth (тот же ApiToken)
- [02-billing](02-billing-payments.md) — `get_balance`
- [06-solutions](06-solutions.md) — `run_solution` / `get_solution_status`
- [07-proposals](07-proposals.md) — `generate_proposal`
- [05-chatbots](05-chatbots.md) — `list_chatbots` / `recent_records`
