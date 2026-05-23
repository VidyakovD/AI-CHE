/**
 * E2E: регистрация → верификация → логин → кабинет.
 *
 * Требования к окружению:
 *   - сервер запущен на BASE_URL (default localhost:8000)
 *   - DEV_MODE=true и APP_ENV=dev (иначе CORS/cookies сломают тест)
 *   - SMTP отключён (письма должны логироваться в консоль) — мы достаём
 *     verify-код прямо из БД через test-helper endpoint /admin/test/last-verify-code
 *     (этот endpoint должен быть guarded DEV_MODE-ом и НЕ существовать в проде).
 */
import { test, expect } from '@playwright/test';

const EMAIL = `e2e-${Date.now()}@test.local`;
const PASSWORD = 'TestPass1234!';

test.describe('Auth flow', () => {
  test('register → verify → login → cabinet', async ({ page, request }) => {
    // 1. Регистрация
    await page.goto('/');
    await page.click('button:has-text("Регистрация")');
    await page.fill('input[type="email"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.check('input[type="checkbox"]');   // соглашение с офертой
    await page.click('button:has-text("Зарегистрироваться")');
    await expect(page.locator('text=/код подтверждения|verify/i')).toBeVisible({ timeout: 10_000 });

    // 2. Достаём verify-код через test-helper endpoint
    // ВНИМАНИЕ: endpoint должен быть guarded DEV_MODE — не для прода.
    const codeResp = await request.get(`/admin/test/last-verify-code?email=${EMAIL}`);
    expect(codeResp.ok()).toBeTruthy();
    const { code } = await codeResp.json();
    expect(code).toMatch(/^\d{6}$/);

    // 3. Ввод кода
    await page.fill('input[name="verify_code"]', code);
    await page.click('button:has-text("Подтвердить")');
    await expect(page).toHaveURL(/\/$/);

    // 4. Проверяем что юзер залогинен и видит баланс
    const sidebarBalance = page.locator('#sidebarBalance');
    await expect(sidebarBalance).toBeVisible();
    await expect(sidebarBalance).toContainText('₽');

    // 5. Демо-данные созданы (commit e9a0ec8) — должна быть welcome-заметка
    await page.goto('/notes.html');
    await expect(page.locator('text=/Добро пожаловать в Че/i')).toBeVisible({ timeout: 5_000 });

    // 6. И демо-событие в календаре
    await page.goto('/calendar.html');
    await expect(page.locator('text=/Познакомиться с возможностями Че/i')).toBeVisible({ timeout: 5_000 });
  });

  test('login with wrong password shows error', async ({ page }) => {
    await page.goto('/');
    await page.click('button:has-text("Войти")');
    await page.fill('input[type="email"]', EMAIL);
    await page.fill('input[type="password"]', 'WrongPass1234!');
    await page.click('button:has-text("Войти")');
    await expect(page.locator('text=/неверн|invalid/i')).toBeVisible({ timeout: 5_000 });
  });
});
