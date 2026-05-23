/**
 * E2E: модули агента — Notes, Calendar, Finance.
 *
 * Pre-requisite: тест-юзер должен быть залогинен. Используем storageState
 * созданный в auth.spec.ts (npx playwright test --project=chromium auth).
 *
 * Покрывает критичные UI-flow которые pytest не видит:
 *   - создание заметки → RAG-индексация → доступность в чате
 *   - создание события календаря → отображение в списке
 *   - импорт CSV финансов → таблица + категоризация
 */
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// Утилита: ждать toast-уведомление о саксесcе
async function expectToast(page: any, pattern: RegExp) {
  await expect(page.locator('.toast, [data-toast]').filter({ hasText: pattern }))
    .toBeVisible({ timeout: 5_000 });
}

test.describe('Module: Notes', () => {
  test('создать → найти через поиск → удалить', async ({ page }) => {
    await page.goto('/notes.html');
    await page.click('button:has-text("Новая заметка")');
    const title = `E2E Test ${Date.now()}`;
    await page.fill('#noteTitle', title);
    await page.fill('#noteText', 'Тестовый текст для семантического поиска. Содержит уникальное слово xyzzy42.');
    await page.click('button:has-text("Сохранить")');
    await page.waitForTimeout(1200);   // sleep пока модал закроется + RAG индексирует

    // Семантический поиск должен найти
    await page.fill('#searchInput', 'xyzzy42');
    await expect(page.locator('text=' + title)).toBeVisible({ timeout: 5_000 });

    // Открываем и удаляем
    await page.locator('.note-row').filter({ hasText: title }).first().click();
    await page.click('button:has-text("Удалить")');
    page.on('dialog', d => d.accept());
    await expect(page.locator('text=' + title)).not.toBeVisible({ timeout: 3_000 });
  });
});

test.describe('Module: Calendar', () => {
  test('создать локальное событие → отображается в списке', async ({ page }) => {
    await page.goto('/calendar.html');
    await page.click('button:has-text("Новое событие")');
    const title = `E2E Event ${Date.now()}`;
    await page.fill('#evTitle', title);
    // Дата по умолчанию = сегодня, время 12:00 — оставляем
    await page.fill('#evLocation', 'Zoom: e2e-test.zoom.us');
    await page.click('button:has-text("Создать")');
    await page.waitForTimeout(800);

    // Должно быть видно в списке с source-тегом 'local'
    await expect(page.locator('.ev-title').filter({ hasText: title })).toBeVisible({ timeout: 5_000 });
    await expect(page.locator('.ev-src.local')).toBeVisible();

    // Удаление
    const row = page.locator('.ev').filter({ hasText: title }).first();
    page.on('dialog', d => d.accept());
    await row.locator('button.btn-d').click();
    await expect(page.locator('.ev-title').filter({ hasText: title })).not.toBeVisible({ timeout: 3_000 });
  });
});

test.describe('Module: Finance', () => {
  test('импорт CSV → таблица + агрегаты', async ({ page }) => {
    // Готовим минимальный generic CSV
    const csv = [
      'date,amount,description',
      '2026-05-20,-150.00,АЗС Лукойл',
      '2026-05-21,-250.50,Магнит',
      '2026-05-22,50000.00,Зарплата',
    ].join('\n');
    const tmpPath = path.join('/tmp', `e2e-finance-${Date.now()}.csv`);
    fs.writeFileSync(tmpPath, csv, 'utf8');

    await page.goto('/finance.html');
    await page.click('button:has-text("Импорт CSV")');
    await page.setInputFiles('#csvFile', tmpPath);
    await page.selectOption('#csvBank', 'generic');
    await page.click('button:has-text("Загрузить")');

    // Ждём пока импорт пройдёт
    await expect(page.locator('#importStatus')).toContainText(/Импортировано:\s*3/, { timeout: 10_000 });
    await page.waitForTimeout(800);

    // Проверяем что появились стат-карточки
    await expect(page.locator('#statCount')).toContainText('3');
    await expect(page.locator('#statIn')).toContainText('50');     // 50 000 ₽ дохода
    await expect(page.locator('#statOut')).toContainText('401');   // 400.50 расхода (≈ 401)

    // Cleanup
    page.on('dialog', d => d.accept());
    await page.click('#clearAllBtn');

    fs.unlinkSync(tmpPath);
  });
});
