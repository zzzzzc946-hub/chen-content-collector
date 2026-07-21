import { expect, test, type Page } from '@playwright/test';

const longUnbrokenCaption = `https://example.com/${'unbrokencaptiontoken'.repeat(320)}`;

const report = {
  accessRole: 'collaborator',
  dailyDate: '2026-07-15',
  id: 'report-1',
  items: [
    {
      caption: longUnbrokenCaption,
      id: 'item-1',
      localRecordId: 'local-1',
      maxDailyCard: longUnbrokenCaption,
      maxFeedback: 'MAX 反馈',
      mediaId: null,
      reviewStatus: 'pending',
      sourceUrl: 'https://example.com/source',
      title: '响应式测试素材',
      version: 1,
    },
  ],
  mediaMode: 'current_day',
  publishedVersion: 1,
  status: 'published',
} as const;

const ownerReport = {
  ...report,
  accessRole: 'owner',
} as const;

const ownerSession = {
  access_token: 'owner-e2e-token',
  expires_at: Math.floor(Date.now() / 1000) + 60 * 60,
  expires_in: 60 * 60,
  refresh_token: 'owner-e2e-refresh-token',
  token_type: 'bearer',
  user: {
    app_metadata: {},
    aud: 'authenticated',
    created_at: '2026-07-15T08:00:00.000Z',
    email: 'owner@example.com',
    id: 'owner-e2e-user',
    role: 'authenticated',
    user_metadata: {},
  },
} as const;

async function mockDailyApi(page: Page): Promise<void> {
  await page.route('**/api/reports', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify([{
      dailyDate: report.dailyDate,
      id: report.id,
      itemCount: report.items.length,
      publishedAt: '2026-07-15T08:00:00.000Z',
    }]),
  }));
  await page.route('**/api/reports/report-1', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(report),
  }));
  await page.route('**/api/media/session', (route) => route.fulfill({ status: 204 }));
}

async function mockCopyFallback(page: Page): Promise<void> {
  await page.addInitScript(() => {
    type CopySnapshot = {
      selectionEnd: number;
      selectionStart: number;
      value: string;
    };
    (window as Window & { __copyFallbackSnapshots?: CopySnapshot[] }).__copyFallbackSnapshots = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    document.execCommand = (command: string): boolean => {
      if (command !== 'copy') return false;
      const textarea = document.activeElement;
      if (!(textarea instanceof HTMLTextAreaElement)) return false;
      (window as Window & { __copyFallbackSnapshots?: CopySnapshot[] }).__copyFallbackSnapshots?.push({
        selectionEnd: textarea.selectionEnd,
        selectionStart: textarea.selectionStart,
        value: textarea.value,
      });
      return true;
    };
  });
}

async function openDaily(page: Page, width: number): Promise<void> {
  await page.setViewportSize({ height: 844, width });
  await page.addInitScript(() => window.localStorage.clear());
  await mockDailyApi(page);
  await page.goto('/daily');
  await expect(
    page.locator('.video-panel').getByRole('heading', { name: '响应式测试素材' }),
  ).toBeVisible();
}

async function openOwnerReport(page: Page): Promise<void> {
  await page.setViewportSize({ height: 844, width: 390 });
  await page.addInitScript((session) => {
    window.localStorage.clear();
    window.localStorage.setItem('sb-e2e-auth-token', JSON.stringify(session));
  }, ownerSession);
  await page.route('**/api/reports/report-1', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(ownerReport),
  }));
  await page.route('**/api/media/session', (route) => route.fulfill({ status: 204 }));
  await page.goto('/r/report-1');
  await expect(page.getByText('owner@example.com')).toBeVisible();
}

async function expectSingleColumnMobile(page: Page): Promise<void> {
  const gridColumns = await page.locator('.daily-layout[data-view="workbench"]').evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.split(' '),
  );

  expect(gridColumns).toHaveLength(1);
  expect(await page.locator('.daily-topbar').evaluate((element) => element.clientHeight)).toBeLessThanOrEqual(120);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
  await expect(page.locator('.daily-date')).toBeVisible();
  await expect(page.locator('.daily-date')).toHaveText(report.dailyDate);
  await expect(page.locator('.material-count')).toBeVisible();
  await expect(page.locator('.material-count')).toHaveText('1 条素材');
  await expect(page.locator('.mobile-workbench-transcript')).toBeVisible();
  await expect(page.locator('.video-metadata-transcript')).toBeHidden();
  await expect(page.getByRole('tab')).toHaveCount(0);

  const transcriptScroll = page.locator('.mobile-workbench-transcript .transcript-document p');
  const cardScroll = page.getByLabel('MAX口喷卡片');
  expect(await cardScroll.evaluate((element) => element.clientHeight)).toBe(
    await transcriptScroll.evaluate((element) => element.clientHeight),
  );
  expect(await cardScroll.evaluate((element) => getComputedStyle(element).overflowY)).toBe('auto');
  expect(await cardScroll.evaluate((element) => element.scrollHeight)).toBeGreaterThan(
    await cardScroll.evaluate((element) => element.clientHeight),
  );
}

test('390px keeps the mobile reading flow in one column', async ({ page }) => {
  await openDaily(page, 390);
  await expectSingleColumnMobile(page);
});

test('390px keeps long unbroken transcript text inside the page', async ({ page }) => {
  await openDaily(page, 390);

  const transcript = page.locator('.transcript-document p');
  await expect(transcript).toHaveText(longUnbrokenCaption);
  expect(await transcript.evaluate((element) => element.scrollWidth)).toBeLessThanOrEqual(
    await transcript.evaluate((element) => element.clientWidth),
  );
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
});

test('390px owner identity header keeps controls visible without overflow', async ({ page }) => {
  await openOwnerReport(page);

  const topbar = page.locator('.daily-topbar');
  await expect(topbar.getByText('MAX DAILY INTEL')).toBeVisible();
  await expect(topbar.locator('.daily-date')).toBeVisible();
  await expect(topbar.locator('.daily-date')).toHaveText(ownerReport.dailyDate);
  await expect(topbar.locator('.material-count')).toBeVisible();
  await expect(topbar.locator('.material-count')).toHaveText('1 条素材');
  await expect(page.getByRole('button', { name: '刷新日报' })).toBeVisible();
  await expect(page.getByRole('button', { name: '权限设置' })).toBeVisible();
  await expect(page.getByRole('button', { name: '退出登录' })).toBeVisible();
  expect(await topbar.evaluate((element) => element.clientHeight)).toBeLessThanOrEqual(120);
  expect(await topbar.evaluate((element) => element.scrollHeight)).toBeLessThanOrEqual(120);
  expect(await topbar.locator('*').evaluateAll((elements) => elements.flatMap((element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    if (
      style.display === 'none'
      || style.visibility === 'hidden'
      || rect.width === 0
      || rect.height === 0
    ) {
      return [];
    }
    return rect.left < 0 || rect.right > window.innerWidth || rect.top < 0 || rect.bottom > window.innerHeight
      ? [element.className || element.tagName]
      : [];
  }))).toEqual([]);
});

test('390px fallback copy fully selects transcript and MAX card text', async ({ page }) => {
  await mockCopyFallback(page);
  await openDaily(page, 390);

  await page.locator('.transcript-document').getByRole('button', { name: '复制文稿' }).click();
  await expect(page.locator('.transcript-document').getByRole('button', { name: '已复制' })).toBeVisible();
  await page.locator('.collaborative-panel').getByRole('button', { name: '复制口喷卡片' }).click();
  await expect(page.locator('.collaborative-panel').getByRole('button', { name: '已复制' })).toBeVisible();

  await expect.poll(() => page.evaluate(() => (
    (window as Window & {
      __copyFallbackSnapshots?: Array<{ selectionEnd: number; selectionStart: number; value: string }>;
    }).__copyFallbackSnapshots
  ))).toEqual([
    { selectionEnd: longUnbrokenCaption.length, selectionStart: 0, value: longUnbrokenCaption },
    { selectionEnd: report.items[0].maxDailyCard.length, selectionStart: 0, value: report.items[0].maxDailyCard },
  ]);
});

test('820px keeps the mobile reading flow in one column', async ({ page }) => {
  await openDaily(page, 820);
  await expectSingleColumnMobile(page);
});

test('821px keeps three readable workbench columns without page overflow', async ({ page }) => {
  await openDaily(page, 821);

  const layout = page.locator('.daily-layout[data-view="workbench"]');
  const gridColumns = await layout.evaluate((element) => (
    getComputedStyle(element).gridTemplateColumns
      .split(' ')
      .map((value) => Number.parseFloat(value))
  ));

  expect(gridColumns).toHaveLength(3);
  expect(gridColumns[1]).toBeGreaterThanOrEqual(280);
  expect(gridColumns[0]).toBeGreaterThanOrEqual(160);
  expect(gridColumns[2]).toBeGreaterThanOrEqual(220);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
  expect(await page.locator('.toolbar-shell').evaluate((element) => element.scrollWidth)).toBeLessThanOrEqual(
    await page.locator('.toolbar-shell').evaluate((element) => element.clientWidth),
  );
  await expect(page.getByRole('button', { name: '字段配置' })).toBeVisible();
  await expect(page.getByRole('button', { name: '筛选' })).toBeVisible();
  await expect(page.getByRole('button', { name: '排序' })).toBeVisible();
  await expect(page.getByRole('button', { name: '行高' })).toBeVisible();
  await expect(page.getByRole('button', { name: '调整空间' })).toBeVisible();
  await expect(page.locator('.mobile-workbench-transcript')).toBeHidden();
});
