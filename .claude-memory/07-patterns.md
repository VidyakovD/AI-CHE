# 07. Коддинг-паттерны проекта

Если пишешь новый код — следуй этим паттернам, они уже приняты.

## Биллинг (ОБЯЗАТЕЛЬНО)

**Запрет**: `user.tokens_balance += X` или `-= X` где угодно. Всегда через `server/billing.py`:

```python
from server.billing import deduct_atomic, deduct_strict, credit_atomic

# Вариант 1: списать сколько есть, не уходя в минус (после дорогого действия)
charged = deduct_atomic(db, user_id, cost)  # возвращает min(balance, cost)

# Вариант 2: списать полную сумму или отказать (предоплата)
if not deduct_strict(db, user_id, cost):
    raise HTTPException(402, "Недостаточно токенов")

# Зачислить
credit_atomic(db, user_id, amount)

# После — свой db.commit()
db.commit()
```

Writes `Transaction(type="usage", tokens_delta=-cost, description=...)` нужно добавлять вручную после deduct.

## Сессии БД вне FastAPI Depends

**Запрет**: голый `db = SessionLocal()` в фоновых задачах/helpers. Использовать:

```python
from server.db import db_session

with db_session() as db:
    # Автоматический rollback на exception + close() в finally
    obj = db.query(Model).first()
    obj.field = "new"
    db.commit()
```

## Миграции

Добавить колонку:

```python
# server/db.py
LIGHTWEIGHT_MIGRATIONS.append(
    ("table_name", "new_col", "VARCHAR DEFAULT 'value'"),
)
```

Применяется на старте автоматически. Идемпотентно.

Добавить index:

```python
LIGHTWEIGHT_INDEXES.append(
    ("index_name", "CREATE INDEX IF NOT EXISTS ... ON ..."),
)
```

## Рейт-лимит нового эндпоинта

```python
# server/security.py::RULES
RULES["/my/new/endpoint"] = (60, 60)   # 60 запросов/мин на IP
```

## Secret в БД (шифрование)

```python
from server.secrets_crypto import encrypt, decrypt

# При записи
cred.password = encrypt(plaintext_password)

# При чтении
plain = decrypt(cred.password)
```

Формат: `enc:v1:<fernet_token>`. Обратная совместимость с plaintext и старым `enc:<token>` без версии.

## Advisory lock для фоновой задачи

Если задача запускается из `startup()` и работает в каждом worker uvicorn:

```python
from server.worker_lock import worker_lock

async def my_loop():
    while True:
        with worker_lock("my_task", ttl_sec=25) as acquired:
            if acquired:
                await do_tick()
        await asyncio.sleep(30)
```

Без этого задача выполнится дважды в минуту (по одному разу в каждом worker).

## Admin audit log

Для любого админ-действия, меняющего критичные данные (балансы, цены, ключи, баны):

```python
from server.admin_audit import log_admin_action

log_admin_action(
    db, admin_user,
    action="adjust_balance",
    target_type="user", target_id=user_id,
    details={"delta": delta, "reason": reason},
    request=request,   # для IP
)
```

## IDOR защита

Для любого эндпоинта `/resource/{id}` с `optional_user` или `current_user`:

```python
item = db.query(Resource).filter_by(id=item_id).first()
if not item:
    raise HTTPException(404)
# Проверка владения
if item.user_id != (user.id if user else None):
    raise HTTPException(403, "Нет доступа")
```

Особенно актуально для: `/agent/{id}/*`, `/solutions/runs/{id}/*`, `/sites/projects/{id}/*`, `/chatbots/{id}/*`, `/presentations/projects/{id}/*`.

## Сообщения юзеру (русский, понятный)

Плохо:
```python
raise HTTPException(500, str(e))  # утечка internal stacktrace
```

Хорошо:
```python
try:
    ...
except Exception as e:
    log.error(f"Payment create error: {e}")
    raise HTTPException(500, "Не удалось создать платёж. Попробуйте через минуту или напишите в поддержку.")
```

## Async vs sync

- FastAPI endpoint handlers — async (если вызывают `await`)
- AI-вызовы (OpenAI/Anthropic SDK) — синхронные → через `loop.run_in_executor()` если внутри async
- Пример: `grok_search_response` в chatbot_engine обёрнут в executor

## Frontend JS (чистый, без фреймворка)

Паттерны из существующего кода:

```javascript
// API base
const API = localStorage.getItem('obs_api') || window.location.origin;

// Auth header
function hdrs() {
    return {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + localStorage.getItem('obs_token'),
    };
}

// Escape HTML (defined in каждом файле)
function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Price formatting (если уже подключён pricing-helper)
window.formatCH(50);   // "50 CH (~5 ₽)"
window.chToRub(50);    // "~5 ₽"
```

**ЗАПРЕТ** в innerHTML: вставлять user-input без `esc()`:

```javascript
// ПЛОХО
el.innerHTML = `<p>${bot.name}</p>`;
// ХОРОШО
el.innerHTML = `<p>${esc(bot.name)}</p>`;
```

## Commit messages

Формат: `<type>(<scope>): <краткое описание>`

Примеры:
- `feat(agents-ux): кнопки действий на карточке + 2-CTA wizard`
- `fix(orchestrator): бот молчал — single-downstream отключал ветки`
- `security(comprehensive): P0/P1/P2 фиксы из аудита`
- `chore: stop tracking .env and server/.jwt_secret`

Тело коммита — подробное, **на русском**, с пояснением «почему» а не только «что». 

Finisher:
```
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

## Диагностика на проде

При «бот не отвечает» / «ошибка» / «не работает X»:

1. `journalctl -u ai-che --since "10 minutes ago" --no-pager` — свежие ошибки
2. Проверить БД: что в `chatbots`, `agent_configs`, `api_keys`
3. Если AI — запустить `generate_response()` напрямую через python3 в venv (пример в 06-deploy.md)
4. Если проблема в workflow — patch `_execute_node` через monkeypatch и логировать каждый шаг (пример в диагностике `762c5c4`)

## Тесты

Текущий state: `tests/test_api.py` почти пустой. При изменении критичной логики (биллинг, auth, webhook):

1. Напиши репро-скрипт с `python3 -c "..."` в PR-описании
2. Пригони реальные данные из прод-БД и протестируй локально (DEV_MODE=true, JWT_SECRET=test)
3. Desk-check: `node -e "..."` для парсинга JS (выявляет SyntaxError в HTML)
