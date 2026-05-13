# Подключение AI Студия Че к Claude Desktop через MCP

**MCP (Model Context Protocol)** — открытый стандарт от Anthropic для подключения AI-клиентов к внешним сервисам. AI Студия Че реализует MCP-сервер на `/mcp`, позволяя в Claude Desktop / Cursor / любом MCP-клиенте работать с твоими проектами командами на естественном языке:

- «Покажи мои последние 5 КП»
- «Запусти SWOT-анализ компании Ромашка с ИНН 7707083893»
- «Сколько у меня лидов из чат-бота на этой неделе?»
- «Создай КП клиенту Иван Иванов на разработку лендинга»

## 1. Получить API-токен

1. Открой https://aiche.ru/ → войди в свой аккаунт
2. Кабинет → вкладка **🔌 API & интеграции** → **🔑 Токены** → **Создать**
3. Имя токена: `Claude Desktop MCP`
4. Scopes: ✅ `proposals`, ✅ `solutions`, ✅ `chatbots`
5. Скопируй **raw-токен** вида `ai_che_<prefix>_<secret>` — показывается **только один раз**

## 2. Настроить Claude Desktop

Открой конфиг-файл:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Если файла нет — создай. Содержимое:

```json
{
  "mcpServers": {
    "aiche": {
      "url": "https://aiche.ru/mcp",
      "headers": {
        "Authorization": "Bearer ai_che_<prefix>_<secret>"
      }
    }
  }
}
```

**Полностью перезапусти Claude Desktop** (через трей — Quit & Restart).

В новом чате появится иконка 🔌 — наведи курсор, увидишь «aiche» с tools.

## 3. Tools (что Claude может вызывать)

| Tool | Что делает |
|---|---|
| `get_balance` | Баланс юзера в копейках/рублях |
| `list_solutions` | Каталог 40 бизнес-решений с фильтром по категории |
| `run_solution` | Запустить orchestra-пилот (асинхронно) |
| `get_solution_status` | Узнать статус run'а + финальный output |
| `list_proposals` | Список последних КП |
| `get_proposal` | Карточка КП по ID |
| `create_proposal` | Создать КП-черновик (без AI-генерации) |
| `generate_proposal` | Полноценная AI-генерация КП (списывает 50 ₽) |
| `list_chatbots` | Список твоих чат-ботов |
| `recent_records` | Последние заявки/брони/заказы из ботов |

## 4. Resources (статичные данные)

Claude может прочитать как контекст:

| URI | Что внутри |
|---|---|
| `aiche://categories` | Все категории бизнес-решений с counts |
| `aiche://pricing` | Текущий прайс-лист (КП / сайты / маржа) |
| `aiche://models` | Каталог доступных LLM-моделей |

В Claude Desktop эти ресурсы видятся в UI как «attachable resources» — можно прикрепить к сообщению. Также Claude может прочитать их сам если решит что нужен контекст.

## 5. Примеры запросов в Claude Desktop

После подключения попробуй:

> **«Покажи мои бизнес-решения из категории финансы»**
> Claude вызовет `list_solutions` с `subcategory=finance`.

> **«Запусти проверку контрагента — ИНН 7707083893»**
> Claude найдёт нужное решение через list_solutions, потом `run_solution`, потом мониторит через `get_solution_status`.

> **«Сделай КП клиенту "ООО Ромашка" — нужен лендинг для продажи кофе, бюджет 100к»**
> Claude вызовет `generate_proposal` с этими параметрами → получит preview_url для отправки клиенту.

> **«Какие заявки пришли в чат-бот за последние сутки?»**
> Claude вызовет `recent_records` с `limit=20`, отфильтрует по дате.

## 6. Тестирование через curl

Если хочешь убедиться что всё работает до подключения Claude Desktop:

```bash
TOKEN="ai_che_PREFIX_SECRET"

# Info (без auth)
curl https://aiche.ru/mcp

# Initialize handshake
curl -X POST https://aiche.ru/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'

# Каталог tools
curl -X POST https://aiche.ru/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# Получить баланс
curl -X POST https://aiche.ru/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_balance","arguments":{}}}'

# Список ресурсов
curl -X POST https://aiche.ru/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"resources/list"}'

# Прочитать прайс
curl -X POST https://aiche.ru/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"aiche://pricing"}}'
```

## 7. Безопасность

- Токен передаётся только через `Authorization: Bearer`
- Все запросы HTTPS (TLS 1.2/1.3)
- Scope-проверка на каждом tool: токен без scope `proposals` не сможет вызвать `create_proposal`
- Rate-limit как в обычном Public API (10 req/min для tools, 30 для info)
- Tools могут списывать с баланса (например `generate_proposal` = 50 ₽); список цен в `aiche://pricing`

## 8. FAQ

**Q: Сколько стоит использование MCP?**
A: Сами вызовы — бесплатно. Платные действия (генерация КП, запуск orchestra-пилота) списываются с баланса по обычным ценам сервиса.

**Q: Можно ли отозвать токен?**
A: Да, в кабинете → 🔌 API & интеграции → Токены → корзинка рядом с токеном.

**Q: Что если Claude вызывает не тот tool?**
A: В системном промпте Claude Desktop опиши задачу детальнее. Например, вместо «расскажи про КП» скажи «вызови aiche.list_proposals и покажи 5 последних».

**Q: Работает ли с Cursor / другими MCP-клиентами?**
A: Да, любой MCP 2024-11-05 совместимый клиент. Конфигурация аналогичная (URL + Authorization header).

---

Адаптация модуля Dolibarr `htdocs/ai/server/mcp_server.php` на FastAPI/Python.
