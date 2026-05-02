# РФ-Compliance: чеклист действий юзера

Что я (Claude) **сделал автоматически**:

| # | Что | Файл / Status |
|---|---|---|
| 1 | AES-256-GCM шифрование бэкапов БД | [server/scheduler.py](../server/scheduler.py) — `_db_backup_tick` шифрует перед записью в `/backups/*.enc`. Ключ — в `.backup_encryption_key` (auto-generated, права 0o400) или env `BACKUP_ENCRYPTION_KEY`. Plaintext `.tmp` удаляется сразу после шифрования. |
| 2 | Утилита расшифровки | [scripts/restore_backup.py](../scripts/restore_backup.py) — `python restore_backup.py backup.enc out.db` |
| 3 | Чек 54-ФЗ в ЮKassa: tax_system_code + payment_subject + payment_mode | [server/routes/payments.py](../server/routes/payments.py) — настраивается через `YOOKASSA_VAT_CODE` (default 1=Без НДС) и `YOOKASSA_TAX_SYSTEM_CODE` (default 2=УСН доходы). |
| 4 | Чекбокс маркетинговой рассылки отдельно от оферты | [server/models.py](../server/models.py): `User.marketing_consent` + `User.marketing_consent_at`. Endpoint `PUT /user/marketing-consent` для toggle в кабинете. |
| 5 | Из payment-логов убраны суммы | [server/routes/payments.py](../server/routes/payments.py) — `payment.webhook` и `payment.confirm` логируют только `payment_id`, без `amount_kop`/`amount_rub`. Суммы остаются только в защищённой таблице `transactions`. |

---

## ❗ Что нужно сделать тебе руками (Claude не может)

### 1. КРИТИЧНО: Подключить SMTP-провайдер

**Сейчас:** на проде **SMTP вообще не настроен** (`SMTP_HOST` отсутствует в `.env`). Это значит:
- Юзеры **не получают** код подтверждения email при регистрации
- Login alerts при новом IP не уходят
- Reset-password не работает
- Email-уведомление при привязке TG management не уходит

**Что делать:** выбери одного из российских провайдеров (152-ФЗ — данные не должны уходить заграницу):

| Провайдер | Цена | Плюсы |
|---|---|---|
| **Unisender Go** | ~0.30 ₽/письмо | Российский, в реестре РКН, простая API + SMTP |
| **SendPulse** | бесплатно до 12k/мес | Российская инфра, SMTP relay |
| **Yandex 360 для бизнеса** | от 249 ₽/мес | Российский, твой домен `aiche.ru`, SMTP-relay |
| **Mailtrap** (НЕ для прод) | — | Только sandbox |

**Действия:**
1. Зарегистрируйся в выбранном провайдере, получи SMTP-кредeнтиалы.
2. Добавь в `/root/AI-CHE/.env`:
   ```
   SMTP_HOST=smtp.unisender.com
   SMTP_PORT=587
   SMTP_USER=<твой логин>
   SMTP_PASS=<пароль>
   SMTP_FROM=AI Студия Че <noreply@aiche.ru>
   ```
3. Рестарт: `systemctl restart ai-che`.
4. Проверь: попробуй зарегистрировать тестовый аккаунт — должен прийти код.

---

### 2. КРИТИЧНО: Бэкапы вне РФ-сервера + переезд primary в РФ (152-ФЗ)

**Сейчас:** прод в Нидерландах (Дронтен, Clouvider). Бэкапы лежат локально на том же сервере.

По **152-ФЗ ст. 18 ч. 5** ПДн граждан РФ должны храниться в БД, расположенной на территории РФ. Если у тебя есть юзеры с российскими паспортами/email на `.ru` — формально это нарушение.

**Варианты:**

#### Вариант A: Минимально — только бэкапы в РФ
- Завести **Yandex Object Storage** (S3-compatible) или **Yandex 360 Vault**
- Скрипт ниже автоматом загружает зашифрованный бэкап туда после создания:
  ```bash
  # /root/AI-CHE/scripts/upload_backup.sh
  #!/bin/bash
  TODAY=$(date +%Y-%m-%d)
  BACKUP="/root/AI-CHE/backups/chat.db.${TODAY}.enc"
  if [ -f "$BACKUP" ]; then
    aws s3 cp "$BACKUP" s3://aiche-backups/$(date +%Y/%m)/ \
      --endpoint-url=https://storage.yandexcloud.net \
      --profile yandex
  fi
  ```
- Cron: `0 4 * * * /root/AI-CHE/scripts/upload_backup.sh`

#### Вариант B: Перенос primary сервера в РФ
- VPS у Selectel / Yandex Cloud / Mail Cloud Solutions / Reg.ru
- Перенос chat.db через scp + rsync uploads
- DNS A-запись `aiche.ru` → новый IP

Я могу подготовить миграционный скрипт и инструкцию деплоя на новый сервер, если согласишься.

---

### 3. Подключить ОФД к ЮKassa (54-ФЗ)

Я добавил `receipt`-объект в `payment_data`, но **ЮKassa отправит его в ОФД только если у тебя в ЛК ЮKassa подключён ОФД-сервис**.

**Действия:**
1. Открой [ЛК ЮKassa](https://yookassa.ru/my/) → Настройки → Кассовый чек.
2. Подключи ОФД (Атол Онлайн / Контур.ОФД / Платформа ОФД — выбор зависит от того, какая у тебя касса; для облачной кассы ЮKassa предлагает Атол Онлайн).
3. Заполни в `.env` под свою налоговую систему:
   ```
   YOOKASSA_VAT_CODE=1               # 1=Без НДС, 4=НДС 20% (если ОСН)
   YOOKASSA_TAX_SYSTEM_CODE=2        # 2=УСН доходы, 3=УСН доходы-расходы, 1=ОСН
   ```
4. Тестовый платёж на тестовом кошельке → проверить что чек в ЛК ОФД появился.

---

### 4. Согласия и Политика конфиденциальности (152-ФЗ)

**Сейчас:** есть `Оферта.txt` и `terms.html`, но не выделены отдельно:
- Согласие на обработку ПДн (152-ФЗ)
- Согласие на маркетинговую рассылку (отдельный чекбокс)
- Перечень обработчиков (ЮKassa, OpenAI/Anthropic, SMTP-провайдер, Yandex)

**Действия:** обнови `terms.html` или сделай отдельную `/privacy.html` с такими разделами:

```
1. Оператор: ИП/ООО <имя>, ИНН <…>, адрес <…>
2. Цели обработки: регистрация, аутентификация, оплата, оказание услуг
3. Категории ПДн: email, имя, IP-адрес, история запросов, баланс
4. Обработчики (третьи лица), которым передаются ПДн:
   — ЮKassa (НКО «ЮMoney», ИНН 7750005725) — для приёма платежей
   — Anthropic PBC (США) — для AI-обработки текстов чата (по согласию)
   — OpenAI LLC (США) — для AI-обработки текстов чата (по согласию)
   — Unisender / SendPulse / Yandex 360 — для отправки email (по согласию)
5. Срок хранения: до удаления аккаунта по запросу
6. Права субъекта: доступ, изменение, удаление, отзыв согласия
7. Контакт оператора: dpo@aiche.ru
```

**Подача уведомления в РКН** (ст. 22 152-ФЗ): https://pd.rkn.gov.ru/ — обязательно для коммерческих сервисов с ПДн.

---

### 5. UI: добавить чекбокс рассылки в форму регистрации

Backend готов — `RegisterRequest.marketing_consent` поле принимает. Нужно дополнить форму на фронте (`views/index.html` модалка регистрации):

```html
<label class="flex items-start gap-2 mt-2">
  <input type="checkbox" id="marketingConsent" class="mt-1">
  <span class="text-xs text-zinc-400">
    Согласен получать информационную и рекламную рассылку (можно отключить
    в любой момент в кабинете)
  </span>
</label>
```

И в JS-обработчике `register()` отправлять `marketing_consent: document.getElementById('marketingConsent').checked`.

В кабинете — endpoint `PUT /user/marketing-consent {consent: true|false}` уже работает.

---

### 6. Скопировать ключ шифрования бэкапов в безопасное место

**Сейчас:** при первом backup-tick'е сгенерирован файл `/root/AI-CHE/.backup_encryption_key` (права 0o400). Если сервер компрометирован или удалён — все бэкапы превращаются в мусор без этого ключа.

**Действия (после первого деплоя):**
1. SSH на прод: `cat /root/AI-CHE/.backup_encryption_key` — это hex 64 символа.
2. Скопируй в **1Password / Bitwarden / Yandex Vault** (отдельно от сервера).
3. Опционально: положи в env `BACKUP_ENCRYPTION_KEY=<hex>` для явного управления.

Альтернатива (рекомендуется): сразу задать вручную:
```bash
# Сгенерировать новый ключ:
python -c "import secrets; print(secrets.token_hex(32))"
# Положить в .env:
echo "BACKUP_ENCRYPTION_KEY=<полученный_hex>" >> /root/AI-CHE/.env
# Удалить файл (env приоритет над файлом):
rm /root/AI-CHE/.backup_encryption_key
systemctl restart ai-che
```

---

## Краткое резюме рисков

| Риск | Severity | Кто закрывает |
|---|---|---|
| Юзеры не получают verification email | 🔴 Critical | **Ты** (SMTP) |
| ПДн в Нидерландах (152-ФЗ) | 🔴 Critical | **Ты** (миграция в РФ или Yandex backup) |
| Бэкапы plaintext | ✅ Закрыто | Я (AES-256-GCM) |
| Чеки в ОФД | 🟡 Half-done | Я (receipt в API) + **Ты** (подключить ОФД в ЛК) |
| Маркетинговое согласие отдельно | ✅ Backend готов | **Ты** (UI чекбокс) |
| Логи с суммами | ✅ Закрыто | Я |
| Нет регистрации в РКН | 🔴 Critical | **Ты** (юр.процесс) |
| Политика конфиденциальности | 🟡 Old | **Ты** (юрист) |
