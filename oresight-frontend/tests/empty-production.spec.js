import { test, expect } from '@playwright/test';

// A demo database ages out of every "last N days" window: GET /production?days=30 then legitimately
// returns []. The views used to render "NaN%" and an empty grey chart box. /production is mocked here so the
// test does not depend on what the live database currently holds; every other call goes to the real API.
const NO_DATA = 'No data in the last 30 days';

test.describe('empty production window', () => {
  test.beforeEach(async ({ page }) => {
    await page.route(/\/production\?/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { 'access-control-allow-origin': 'http://localhost:5173', 'access-control-allow-credentials': 'true' },
        body: '[]',
      }),
    );
  });

  test('site Output variance KPI says "no data", never NaN', async ({ page }) => {
    await page.goto('/site/1');
    const card = page.getByTestId('stat-output-variance');
    // Generous: the first page load after the dev server starts waits on Vite's dependency optimisation.
    await expect(card).toBeVisible({ timeout: 30_000 });
    await expect(card).toContainText(NO_DATA);
    await expect(card).not.toContainText('NaN');
  });

  test('site Production tab shows the empty state for the chart and the shift ledger', async ({ page }) => {
    await page.goto('/site/1?tab=production');
    const empty = page.getByTestId('production-empty');
    await expect(empty).toBeVisible();
    await expect(empty.getByText(NO_DATA)).toHaveCount(2); // chart card + shift ledger card
    await expect(empty.locator('table')).toHaveCount(0);
    await expect(empty.locator('.recharts-responsive-container')).toHaveCount(0);
  });

  test('dashboard Production pulse shows the empty state and the page has no NaN', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByText('Production pulse')).toBeVisible();
    await expect(page.getByText(NO_DATA).first()).toBeVisible();
    await expect(page.locator('body')).not.toContainText('NaN');
  });
});
