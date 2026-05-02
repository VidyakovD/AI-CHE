# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-05-02 (после спринтов «Security audit», «Refresh single-use + sites public_token + RAG billing», «РФ Compliance»)_

---

## 🔴 БЛОКЕР юзеру: SMTP не работает

**Сейчас на проде нет SMTP** → юзеры регистрируются, но **не получают** код подтверждения email → не могут попасть в чат / КП / презентации.

**Решение** (выбери одного из российских провайдеров):
- **Unisender Go** ~0.30 ₽/письмо, в реестре РКН
- **SendPulse** бесплатно до 12k/мес
- **Yandex 360 для бизнеса** от 249 ₽/мес, на твоём `aiche.ru`

Действия:
1. Зарегистрируйся, возьми SMTP-кредeнтиалы.
2. На проде:
   ```bash
   ssh root@194.104.9.219
   cat >> /root/AI-CHE/.env <<'EOF'
   SMTP_HOST=smtp.unisender.com
   SMTP_PORT=587
   SMTP_USER=<логин>
   SMTP_PASS=<пароль>
   SMTP_FROM=AI Студия Че <noreply@aiche.ru>
   EOF
   systemctl restart ai-che
   ```
3. Проверь — зарегистрируй тестовый аккаунт, должен прийти код.

---

## ⚠️ 152-ФЗ / 54-ФЗ — действия юзера руками

Полный документ: [docs/compliance_ru.md](docs/compliance_ru.md).

| # | Что | Как сделать |
|---|---|---|
| 1 | **🔑 Сохранить ключ шифрования бэкапов в 1Password / Vault** | Ключ выведен в чате (или: `ssh root@... cat /root/AI-CHE/.backup_encryption_key`). Опционально: положить в `BACKUP_ENCRYPTION_KEY` env и удалить файл. |
| 2 | **Подключить ОФД в ЛК ЮKassa** | [https://yookassa.ru/my/](https://yookassa.ru/my/) → Настройки → Кассовый чек → подключи ОФД (Атол Онлайн / Контур.ОФД). Без этого `receipt`-объект, который мы передаём, никуда не пойдёт. |
| 3 | **Настроить env-переменные ЮKassa под налоговую систему** | В `.env`: `YOOKASSA_VAT_CODE=1` (Без НДС/УСН) или `4` (НДС 20%/ОСН); `YOOKASSA_TAX_SYSTEM_CODE=2` (УСН доходы) или `1` (ОСН). |
| 4 | **Регистрация оператора ПДн в РКН** | https://pd.rkn.gov.ru/ — обязательно для коммерческих сервисов. |
| 5 | **Обновить политику конфиденциальности** | Указать обработчиков: ЮKassa, Anthropic, OpenAI, SMTP-провайдер, Yandex (если будут бэкапы там). См. шаблон в [docs/compliance_ru.md](docs/compliance_ru.md). |
| 6 | **Бэкапы вне РФ-сервера / переезд primary в РФ** | Минимум: настроить ежедневную выгрузку зашифрованного backup'а в Yandex Object Storage. Лучше: миграция `aiche.ru` на VPS в РФ (Selectel/Yandex Cloud/Reg.ru). |

---

## 🟡 Действия юзера для готовых фич

| # | Что | Действия |
|---|---|---|
| 7 | **TG management-бот не запущен на проде** | Создать через @BotFather → `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME` в `.env` + `setWebhook`. Инструкция в HANDOVER.md. |
| 8 | **15 видео-туториалов в UI** | Снять MP4 → положить в `views/static/tutorials/<slug>.mp4` (список в `docs/gif_tutorials.md`). После этого я подключу lightbox с автоплеем. |
| 9 | **Прод ЮKassa** | Сейчас тестовый shop. Заменить `YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY` на live. |
| 10 | **OAuth keys (Google/VK)** | `GOOGLE_CLIENT_ID/SECRET` + `VK_CLIENT_ID` в `.env`. |
| 11 | **Ротировать засвеченный Google `AIza…` ключ** | Был в старых journalctl до фильтра `_SecretFilter`. |

---

## 🟢 Что я могу сделать в следующих сессиях (на твой выбор)

### Security (отложенные с security-аудита)
- **JWT `aud`/`iss` strict verification** — сейчас `verify_aud=False`, токен без claims проходит. Нужен grace-period для уже выданных токенов.
- **starlette upgrade** — CVE-2024-47874, CVE-2025-54121. Pinned в FastAPI 0.111 → апгрейд до 0.115+ с проверкой breaking changes.
- **2FA админки (TOTP через pyotp)** — отдельный flow при логине admin@.
- **Idempotency через Redis** — сейчас in-process, для multi-worker нужен общий стор.
- **`/admin/reencrypt-secrets` endpoint** — для ротации `JWT_SECRET` (сейчас задумано в комментариях, но не реализовано).
- **systemd `User=aiche`** — отвязать сервис от root.

### Большие фичи
- **WhatsApp канал через Wazzup24** — самый востребованный из «🔮 Скоро» каналов.
- **Web Push API через VAPID** — push в браузер без TG (sw.js уже умеет push, нужен subscription endpoint + ключи).
- **Native push в TG management-боте расширить** — голосовой ввод (Whisper), генерация КП в чате, low-balance/новые юзеры/платежи админу.
- **PPTX лучше оформление** — gradient overlays, анимации, иконки секций, hero-фоны.
- **Standalone .exe / .dmg / .AppImage (Electron)** — если PWA недостаточно.

### Operations
- **Cloudflare/CDN+WAF** — DDoS-защита + кэш статики (DNS-редирект на CF).
- **Web Push subscription endpoint** + VAPID-ключи — оживить push-handler в `sw.js`.
- **Архивация asset'ов** — прогон в проде, проверить cutoff.
- **Embeddings → pgvector / FAISS** при росте >500 позиций.
- **Убрать legacy `localStorage.obs_token`** — миграция на cookie уже завершена.

---

## ✅ Закрыто за последние 3 спринта

### Спринт «Security audit» (`cc5afa5`)
- VK webhook требует `vk_secret` + `compare_digest`
- SSRF в agent `tool_browse_url` + presentation `_add_remote_image` (DNS-rebinding защита + revalidate редиректов)
- `/knowledge/search` лимит длины `q` (1000 симв)
- RAG лимиты (50 → 20 файлов, 500 МБ/юзер) — потом подняты обратно с биллингом
- bleach-санитизация generated_html КП в legacy fallback + edit-section + save-html
- `/agent/{id}/ws` и `/stream` — owner-check
- `/auth/login` не возвращает `user_id`; `/resend-verify` не палит enumeration
- TG-link rate-limit (10/10мин на TG-user) + email-alert при привязке
- `/p/{token}` PDF — `relative_to(uploads_root)` (defense-in-depth)
- `is_verified` check в `brief-assist` и `/voice/parse`

### Спринт «Refresh single-use + sites public_token + RAG billing» (`d90e2f1`)
- Refresh-token rotation single-use: `User.refresh_jtis` (JSON list, до 10 multi-device)
  - Reuse-detection = revoke ALL sessions + audit-лог critical
  - `/reset-password` revoke ALL refresh-сессий
  - Grace-period для legacy токенов
- `/sites/hosted/{int_id}` → `/sites/hosted/{public_token}` (~160 bit unguessable)
  - Backfill миграция при старте: для уже опубликованных сайтов
  - Убран StaticFiles mount `/sites/hosted` (он обходил sandbox-обёртку)
- Storage-биллинг для RAG-файлов: `KnowledgeFile.last_billed_at` + миграция
  - Лимиты подняты обратно: 50 файлов × 50 МБ × 2 ГБ/юзер
  - Просрочка >7д → `enabled=False` (не в RAG, файл цел)
  - Просрочка >37д → hard-delete

### Спринт «РФ Compliance» (`a2bffc0`)
- AES-256-GCM шифрование бэкапов БД (152-ФЗ ПДн): `_db_backup_tick`
  + утилита `scripts/restore_backup.py`
- Чек 54-ФЗ в ЮKassa: `payment_subject="service"` + `payment_mode="full_payment"`
  + `tax_system_code` (env) + `vat_code` (env)
- `User.marketing_consent` + `marketing_consent_at` отдельно от оферты
  + endpoint GET/PUT `/user/marketing-consent`
  + UI чекбокс в форме регистрации + toggle в кабинете → Настройки
- Из payment-логов убраны суммы (только `payment_id`)
- `docs/compliance_ru.md` — что закрыто, что юзер делает руками

---

## 📋 Заметки для следующей сессии

- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing` без редеплоя. Список ключей и дефолтов — в `server/pricing.py:DEFAULTS`.
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
- **Прайс-листы для КП** — вкладка «📋 Прайсы» в `/proposals.html`
- **Прайс-лист бота** — кнопка `₽` в карточке бота в `/chatbots.html` (отдельно от КП)
- **Native dialogs запрещены** — везде использовать `aiAlert/aiConfirm/aiPrompt` из icons.js
- **WYSIWYG-редактор** — стандарт для всех редакторов (sites + proposals): `contenteditable=true` на body, медиа отключаются
- **Margin ×7 для презентаций** — внутри presentation_builder, в UI не показывается
- **JSON-first генерация для КП** — AI возвращает данные, не HTML. Шаблон/шапка/подвал стабильные.
- **Бэкапы шифруются AES-GCM** — ключ в `.backup_encryption_key` или env `BACKUP_ENCRYPTION_KEY`. Для restore — `scripts/restore_backup.py`.
- **Refresh-token rotation single-use** — после `register_refresh_jti` jti должен быть в наборе. Тесты с `create_refresh_token` напрямую — добавить `register_refresh_jti`.
- **Опубликованные сайты** — URL вида `/sites/hosted/{public_token}/`, не int_id (защита от enumeration).
