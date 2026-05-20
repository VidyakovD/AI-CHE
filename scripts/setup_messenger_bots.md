# Подключение TG и MAX ботов для общения с Че

Этот документ — пошаговая инструкция чтобы активировать кнопки «📱 Подключить
Telegram» и «📱 Подключить MAX» в `/agents-modular.html` для всех юзеров
платформы.

После выполнения юзеры смогут:
- Привязать свой TG/MAX-аккаунт к платформе через одноразовый код
- Писать Че прямо в мессенджере — те же ответы и память что и на сайте
- Команды: `/me` (баланс), `/unlink` (отвязать)

---

## Шаг 1. Создать ботов в мессенджерах

### Telegram

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Команда: `/newbot`
3. **Имя бота**: например «Че — AI Студия» (то что увидит юзер)
4. **Username**: например `aiche_che_bot` (должен оканчиваться на `_bot`)
5. BotFather пришлёт токен — длинная строка вида `1234567890:AABBCCdd...`
6. **Скопируй токен** и username (без `@`)

Дополнительно (опционально):
- `/setdescription` → «Личный AI-помощник Че: поручаю задачи, делегирую модулям, прокачка L0-L4»
- `/setuserpic` → загрузить иконку
- `/setcommands` → отправить список:
  ```
  start - Начать
  me - Мой профиль и баланс
  link - Привязать аккаунт по коду
  unlink - Отвязать аккаунт
  help - Справка
  ```

### MAX

1. Открой [MAX-бот для разработчиков](https://max.ru/masterbot) (или эквивалент — нужно сверить на dev.max.ru)
2. Создай нового бота, по аналогии с BotFather
3. Получи токен
4. Скопируй токен и username

> ⚠ MAX API относительно молодой. Если интеграция не работает — проверить
> актуальную документацию: https://dev.max.ru/docs-api

---

## Шаг 2. Записать токены на прод

Подключаемся к прод-серверу и добавляем в `.env`:

```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\Users\Денис\.ssh\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 << 'EOF'
cd /root/AI-CHE
# Backup .env перед изменением
cp .env .env.backup-bots-$(date +%Y%m%d-%H%M%S)

# Telegram
echo "" >> .env
echo "# Telegram management-бот (создан в @BotFather $(date +%Y-%m-%d))" >> .env
echo "TG_MGMT_BOT_TOKEN=<ВСТАВЬ_ТОКЕН_ТЕЛЕГРАМ>" >> .env
echo "TG_MGMT_BOT_USERNAME=<ВСТАВЬ_USERNAME_БЕЗ_СОБАКИ>" >> .env

# MAX
echo "" >> .env
echo "# MAX management-бот (создан $(date +%Y-%m-%d))" >> .env
echo "MAX_MGMT_BOT_TOKEN=<ВСТАВЬ_ТОКЕН_MAX>" >> .env
echo "MAX_MGMT_BOT_USERNAME=<ВСТАВЬ_USERNAME_БЕЗ_СОБАКИ>" >> .env

systemctl restart ai-che
sleep 3
systemctl is-active ai-che
EOF
```

⚠ **Замени плейсхолдеры** `<ВСТАВЬ_...>` на реальные значения перед запуском.

---

## Шаг 3. Установить webhook'и

После добавления токенов и рестарта — нужно сказать TG и MAX «куда слать
сообщения юзеров».

### Telegram webhook

Сервер автоматически вычисляет secret из токена (см. `server/security.py:tg_webhook_secret`).
Получи secret и установи webhook одной командой:

```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\Users\Денис\.ssh\id_ed25519' \
  root@193.187.92.147 << 'EOF'
cd /root/AI-CHE
TG_TOKEN=$(grep '^TG_MGMT_BOT_TOKEN=' .env | cut -d= -f2-)
SECRET=$(/root/AI-CHE/venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from server.security import tg_webhook_secret
import os
print(tg_webhook_secret(os.getenv('TG_MGMT_BOT_TOKEN', '')))
")
echo "Computed secret: \$SECRET"
curl -s -X POST "https://api.telegram.org/bot\${TG_TOKEN}/setWebhook" \
  -F "url=https://aiche.ru/webhook/tg-mgmt" \
  -F "secret_token=\${SECRET}" \
  -F "allowed_updates=[\"message\",\"callback_query\"]"
echo ""
EOF
```

Должно прийти `{"ok":true,"result":true,"description":"Webhook was set"}`.

### MAX webhook

MAX не поддерживает header-style secret, используем path-secret (derived из токена):

```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\Users\Денис\.ssh\id_ed25519' \
  root@193.187.92.147 << 'EOF'
cd /root/AI-CHE
MAX_TOKEN=$(grep '^MAX_MGMT_BOT_TOKEN=' .env | cut -d= -f2-)
SECRET=$(/root/AI-CHE/venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from server.security import tg_webhook_secret  # та же функция используется и для MAX
import os
print(tg_webhook_secret(os.getenv('MAX_MGMT_BOT_TOKEN', '')))
")
echo "Computed secret: \$SECRET"
curl -s -X POST "https://botapi.max.ru/subscriptions" \
  -H "Authorization: \${MAX_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://aiche.ru/webhook/max-mgmt/\${SECRET}\",\"update_types\":[\"message_created\"]}"
echo ""
EOF
```

Должно вернуться что-то вида `{"ok":true}` или сообщение об успешной подписке.

---

## Шаг 4. Проверка

1. Открой [aiche.ru/agents-modular.html](https://aiche.ru/agents-modular.html)
2. Нажми **📲** в шапке
3. На карточках «Telegram-бот» и «MAX-бот» бейдж должен быть **«доступно»** (а не «скоро»)
4. Кнопка **«📱 Подключить Telegram»** активна (не disabled)

Если бейдж всё ещё «скоро»:
- `journalctl -u ai-che --since '5 min ago' | grep -i bot_token` — проверь что токены прочитались
- Открой DevTools → Network → `GET /user/tg-link/status` → должно `bot_configured: true`

---

## Шаг 5. Self-test (привязать свой аккаунт первым)

1. На сайте: 📲 → «📱 Подключить Telegram» → получи 6-символьный код
2. Открой бота в TG (по deep-link из инструкции под кодом)
3. Бот пришлёт приветствие → пришли ему `/link XXXXXX`
4. Ответ: `✅ Привязано! Аккаунт: vidyakovd@gmail.com`
5. Напиши боту обычным текстом: `привет, ты тут?`
6. Бот должен ответить от лица Че

Аналогично для MAX.

---

## Troubleshooting

**TG `setWebhook` возвращает 401**
- `TG_MGMT_BOT_TOKEN` пустой или неверный в `.env`
- Перезапусти сервис: `systemctl restart ai-che`

**Бот не отвечает в TG**
- Проверь логи: `journalctl -u ai-che --since '2 min ago' | grep tg-mgmt`
- Возможно webhook вернулся к старому URL — переустанови

**На сайте кнопка disabled с подсказкой «бот не подключен админом»**
- Frontend кэширует ответ `/user/tg-link/status` — Ctrl+Shift+R
- Или сервер не перечитал `.env` — `systemctl restart ai-che`

**Привязка прошла, но MAX-бот не отвечает на текст**
- Проверь подписку: `curl https://botapi.max.ru/subscriptions -H "Authorization: $MAX_TOKEN"`
- В ответе должен быть наш URL `aiche.ru/webhook/max-mgmt/<secret>`

---

## Что хранится где (для отладки)

- **Токены**: `/root/AI-CHE/.env` (TG_MGMT_BOT_TOKEN, MAX_MGMT_BOT_TOKEN)
- **Username бота**: тот же `.env` (TG_MGMT_BOT_USERNAME, MAX_MGMT_BOT_USERNAME) — для deep-link'ов
- **Привязка юзера ↔ TG/MAX user_id**: таблица `users` колонки `tg_user_id` / `max_user_id`
- **Одноразовые коды привязки**: те же колонки `tg_link_code` / `max_link_code` (TTL 10 мин)
- **Webhook secret**: НЕ хранится, derived в runtime через `tg_webhook_secret(token)` из `server/security.py`
