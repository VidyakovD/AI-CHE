/**
 * Playwright config для AI Студия Че E2E-тестов.
 *
 * Запуск:
 *   npm i -D @playwright/test
 *   npx playwright install chromium
 *   BASE_URL=http://localhost:8000 npx playwright test
 *
 * В CI: добавить браузеры в .github/workflows/ci.yml через
 *   - uses: microsoft/playwright-github-action@v1
 *   - run: npx playwright test
 *
 * Цель: 5 главных flow которые pytest не покрывает (UI-уровень):
 *   - login + buy-tokens
 *   - чат с агентом (Че отвечает)
 *   - создание заметки на /notes.html
 *   - импорт CSV на /finance.html
 *   - создание события через UI на /calendar.html
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,                   // tests share one user/session
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    locale: 'ru-RU',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
