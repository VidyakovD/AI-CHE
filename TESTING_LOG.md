# Журнал тестирования и багов

> Юзер тестирует функции в проде/локально, пишет сюда что работает и где сломалось.
> Claude (в следующих сессиях) обрабатывает Open-баги по мере появления.
>
> **Новые записи — СВЕРХУ.** Статусы: 🔴 Open → 🔧 In progress → ✅ Fixed → 🟡 Won't fix.
>
> Где смотреть глубже:
> - Серверные ошибки на проде: `journalctl -u ai-che --since '1 hour ago' | grep -iE 'error|trace|exception'`
> - Аудит действий в БД: таблица `action_logs` (или `/admin/actions.txt?since_hours=24`)
> - Frontend: DevTools → Console + Network → отправь Claude скрин/копию

---

## 🔴 Open

_(пусто — записывай сюда то что нашёл при тестировании)_

---

## 🔧 In progress

_(пусто)_

---

## ✅ Fixed

_(пусто)_

---

## 🟡 Won't fix / known limitations

_(пусто)_

---

## Шаблон записи

```
### YYYY-MM-DD HH:MM — <модуль/раздел>
- **Что делал**: открыл /finance.html, нажал «Добавить транзакцию», ввёл сумма 500 ₽
- **Ожидал**: транзакция появится в списке, баланс обновится
- **Результат**: 500 ошибка, в Console: TypeError: Cannot read properties of null
- **Воспроизводимость**: всегда / раз через раз / 1 из 10
- **Скрин/лог**: (приложи в чат)
- **Статус**: 🔴 Open
- **Контекст**: браузер, юзер ID, прод/dev
```

## Что просить у Claude когда придёшь с багом

1. **Один баг — один блок** в формате выше.
2. Если есть Network-payload — копируй request URL + body + response.
3. Если регрессия (раньше работало) — укажи когда сломалось.
4. Тяжёлые сценарии (баги в биллинге, потеря данных) — помечай 🔥 в начале блока.

## Чекист функционала для прохода

> Используй как стартовый набор. Отмечай ✅ если работает, 🔴 если сломалось.

### Auth & профиль
- [ ] Регистрация нового юзера
- [ ] Подтверждение email (письмо приходит, link работает)
- [ ] Вход + JWT cookie
- [ ] 2FA setup → enable → login через 2FA
- [ ] VK OAuth login
- [ ] QR-логин (мобилка → десктоп)
- [ ] Refresh-token rotation
- [ ] Logout

### Чат с AI (5 провайдеров)
- [ ] Чат с GPT
- [ ] Чат с Claude
- [ ] Чат с Grok
- [ ] Чат с Perplexity (с цитатами)
- [ ] Чат с Imagen/Veo (генерация картинок/видео)
- [ ] Голос → транскрипция → ответ
- [ ] Маски (системные промпты): персонажи, переключение

### ИИ Агенты (модульный оркестратор Че)
- [ ] Подключить модуль из каталога (любой)
- [ ] Изменить настройки модуля (PATCH)
- [ ] Запустить модуль вручную (invoke)
- [ ] Отключить модуль (DELETE)
- [ ] Cron-расписание модуля (подождать tick)
- [ ] Прокачка L0 → L1 (после N взаимодействий)

### Модули с UI-страницами
- [ ] /creators.html — добавить бренд, контент-план, prepare, autopost TG
- [ ] /finance.html — добавить транзакцию, CSV-импорт
- [ ] /calendar.html — добавить событие, Google OAuth, Yandex CalDAV
- [ ] /notes.html — добавить заметку, поиск (RAG)

### Биллинг
- [ ] Топап через ЮKassa (тестовый платёж)
- [ ] Списания за AI-вызовы (баланс уменьшается)
- [ ] Списания за модули (cron-tick → транзакция)
- [ ] /storage биллинг (knowledge base, файлы)
- [ ] История транзакций

### Чат-боты (6 каналов)
- [ ] Создать бота (любой шаблон)
- [ ] TG webhook (привязать токен, /start работает)
- [ ] VK longpoll
- [ ] Avito, Max, Wazzup — хотя бы попытка подключения
- [ ] Web-виджет на сайте

### Сайты, КП, Презентации
- [ ] Сгенерировать сайт (sandbox-iframe edit mode)
- [ ] Опубликовать сайт (hosted_path)
- [ ] КП с e-подписью (proposal_public.html)
- [ ] Презентация (HTML preview + PDF + PPTX)

### Public API + MCP
- [ ] Создать API-токен (POST /apikeys, scope)
- [ ] Bearer-вызов к public endpoint
- [ ] Webhook receiver (HMAC signature валидный)
- [ ] MCP JSON-RPC от Claude Desktop

### Админка
- [ ] /admin/stats
- [ ] /admin/users (list, ban/unban)
- [ ] /admin/adjust-balance (через TOTP)
- [ ] /admin/reencrypt-secrets (после ротации JWT_SECRET)
- [ ] /admin/actions.txt (live audit log)

### Push, Email, Notifications
- [ ] Web Push подписка (VAPID)
- [ ] Email уведомление (низкий баланс, новая заявка)
- [ ] In-app notifications (/user/notifications/recent)
