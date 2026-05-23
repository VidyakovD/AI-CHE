# E2E-тесты на Playwright

Покрывают UI-flow которые pytest не видит. **Stub-готовы** — ждут установки
Playwright и поднятия сервера. Когда инфра поднята — `npx playwright test`
прогонит всё.

## Локальный запуск

```bash
# 1. Установить Playwright (один раз)
npm install -D @playwright/test
npx playwright install chromium

# 2. Запустить сервер в отдельном терминале
DEV_MODE=true APP_ENV=dev JWT_SECRET="dev-secret-32-chars-long" \
  python main.py

# 3. Прогнать тесты
BASE_URL=http://localhost:8000 npx playwright test --config=tests/e2e/playwright.config.ts

# 4. UI-mode для отладки (показывает каждый шаг визуально)
BASE_URL=http://localhost:8000 npx playwright test --ui
```

## Что покрыто

| Файл | Сценарии |
|---|---|
| `auth.spec.ts` | Регистрация → verify → cabinet, неверный пароль |
| `modules.spec.ts` | Notes (создать/найти/удалить), Calendar (создать/удалить), Finance (импорт CSV) |

## Что ещё нужно добавить (TODO)

- Chat с агентом: «внеси в календарь на 12 мая в 12:00» → событие появилось
- Креаторы: создать бренд → сгенерировать контент-план
- КП: создать с e-подписью
- 2FA admin: setup → enable → adjust-balance с TOTP
- PrivacyGuard: чат с PII → проверить что в логах маскировано

## Endpoint-helper для тестов

Тестам нужен endpoint `GET /admin/test/last-verify-code?email=...` чтобы достать
6-значный код без реального SMTP. **Этот endpoint должен быть guarded DEV_MODE**
и НЕ существовать в проде.

Реализация (добавить в server/routes/admin.py):

```python
@router.get("/test/last-verify-code")
def admin_test_last_verify_code(email: str, db: Session = Depends(get_db)):
    """ТОЛЬКО для E2E-тестов. Guard: DEV_MODE."""
    if os.getenv("DEV_MODE", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(404)
    from server.models import VerifyToken
    u = db.query(User).filter_by(email=email).first()
    if not u:
        raise HTTPException(404)
    vt = (db.query(VerifyToken)
            .filter_by(user_id=u.id, purpose="verify_email", used=False)
            .order_by(VerifyToken.id.desc()).first())
    if not vt:
        raise HTTPException(404)
    return {"code": vt.token}
```

Сейчас этот endpoint **не добавлен** — добавить когда будут запускать E2E.

## CI-интеграция

В `.github/workflows/ci.yml` добавить job (после pytest):

```yaml
e2e:
  needs: lint-and-test
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: '3.12' }
    - run: pip install -r requirements.txt
    - run: |
        DEV_MODE=true APP_ENV=dev JWT_SECRET=ci-secret-32-chars-long \
          python main.py &
        sleep 5
    - uses: actions/setup-node@v4
      with: { node-version: '20' }
    - run: npm install -D @playwright/test
    - run: npx playwright install --with-deps chromium
    - run: BASE_URL=http://localhost:8000 npx playwright test
    - uses: actions/upload-artifact@v4
      if: failure()
      with:
        name: playwright-report
        path: tests/e2e/playwright-report
```
