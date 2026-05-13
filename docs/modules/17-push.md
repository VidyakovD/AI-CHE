# Модуль 17 — Push & Notifications

> **Что это:** Web Push (VAPID) для уведомлений о завершении asynchronous-задач (orchestra-run / сайт сгенерирован / новая заявка). Open когда: добавляешь push-событие, дебажишь VAPID.

## TL;DR

- **Код:** [server/push.py](server/push.py) (110 строк) — pywebpush 2.0.
- **Routes:** в [server/routes/user.py](server/routes/user.py) — `/notifications/recent`, `/push/subscribe`, `/push/unsubscribe`.
- **UI:** в [views/icons.js](views/icons.js) — `aiNotifRefresh` (колокольчик) + permissions request.
- **Транспорт:** Web Push API (VAPID) — браузер юзера получает уведомление даже если вкладка закрыта.

## Модели

| Таблица | Поля |
|---|---|
| `push_subscriptions` | user_id, endpoint, p256dh, auth, created_at |
| `users.notifications_last_seen_at` | для подсчёта «непрочитанных» в колокольчике |

## События (когда фаирится push)

- `orchestra_run.done` — пилот завершился
- `site.done` / `site.failed` — генерация сайта
- `record.created` — бот собрал заявку
- `proposal.opened` — клиент открыл КП
- `low_balance_alert` — баланс упал ниже порога

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/notifications/recent` | Список последних событий (для колокольчика) |
| POST | `/push/subscribe` | Сохранить VAPID-subscription |
| DELETE | `/push/unsubscribe` | Удалить |

## Гочча

- **VAPID ключи** — в env, **публичный отдаёт фронту через `/auth/me`**.
- **Если push fails** (например 410 Gone — юзер отписался) — удаляем subscription автоматически.
- **iOS Safari требует PWA** — но мы PWA отключили (см. [09-sites.md](09-sites.md) сагу). Push на iOS не работает.

## Зависимости

- [05-chatbots](05-chatbots.md) — record.created
- [06-solutions](06-solutions.md) — orchestra_run.done
- [09-sites](09-sites.md) — site.done/failed
- [07-proposals](07-proposals.md) — proposal.opened
