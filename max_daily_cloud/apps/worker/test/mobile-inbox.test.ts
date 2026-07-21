import { describe, expect, it } from 'vitest';
import type { MobileInboxItemDto } from '../../../packages/shared/src/contracts.js';
import type { IdentityVerifier } from '../src/auth.js';
import type { RateLimiter, WorkerServices } from '../src/env.js';
import { createWorkerApp } from '../src/index.js';
import {
  createFeishuMobileInboxStore,
  extractMobileInboxUrls,
  readMobileInbox,
  readMobileInboxJson,
  submitMobileInbox,
  type MobileInboxStore,
} from '../src/mobile-inbox.js';

const NOW = new Date('2026-07-17T00:00:00.000Z');
const APP_ORIGIN = 'https://daily.example.com';

class MemoryMobileInboxStore implements MobileInboxStore {
  readonly items = new Map<string, MobileInboxItemDto>();
  readonly upserts: Array<{
    note: string;
    platform: string;
    submittedAt: Date;
    url: string;
  }> = [];
  error: Error | null = null;

  async listRecent(limit: number): Promise<MobileInboxItemDto[]> {
    if (this.error) throw this.error;
    return [...this.items.values()]
      .sort((left, right) => Date.parse(right.submittedAt) - Date.parse(left.submittedAt))
      .slice(0, limit);
  }

  async upsert(input: {
    note: string;
    platform: string;
    submittedAt: Date;
    url: string;
  }): Promise<{ created: boolean; item: MobileInboxItemDto }> {
    if (this.error) throw this.error;
    this.upserts.push(input);
    const existing = this.items.get(input.url);
    const item: MobileInboxItemDto = {
      id: existing?.id ?? `item-${this.items.size + 1}`,
      note: input.note,
      platform: input.platform,
      status: existing?.status ?? '待扒取',
      submittedAt: input.submittedAt.toISOString(),
      url: input.url,
    };
    this.items.set(input.url, item);
    return { created: !existing, item };
  }
}

function createStore(): MemoryMobileInboxStore {
  const store = new MemoryMobileInboxStore();
  store.items.set('https://xhslink.com/existing', {
    id: 'existing-item',
    note: '旧备注',
    platform: '小红书',
    status: '待扒取',
    submittedAt: '2026-07-16T23:00:00.000Z',
    url: 'https://xhslink.com/existing',
  });
  return store;
}

class AllowIdentityVerifier implements IdentityVerifier {
  async verify(): Promise<null> {
    return null;
  }
}

class TestRateLimiter implements RateLimiter {
  readonly keys: string[] = [];

  constructor(private readonly allowed = true) {}

  async limit(input: { key: string }): Promise<{ success: boolean }> {
    this.keys.push(input.key);
    return { success: this.allowed };
  }
}

function services(input: {
  mobileInbox?: MobileInboxStore;
  mobileInboxRateLimiter?: RateLimiter;
} = {}): WorkerServices {
  return {
    appOrigin: APP_ORIGIN,
    identityVerifier: new AllowIdentityVerifier(),
    invitationClaimRateLimiter: new TestRateLimiter(),
    mediaSessionSecret: 'test-only-media-session-secret-with-32-bytes',
    mobileInbox: input.mobileInbox ?? createStore(),
    mobileInboxRateLimiter: input.mobileInboxRateLimiter ?? new TestRateLimiter(),
    now: () => NOW,
    ownerEmail: 'chen@example.com',
    publicShareRateLimiter: new TestRateLimiter(),
    shareCookieSecret: 'test-only-share-cookie-secret-with-32-bytes',
    storage: {},
  } as WorkerServices;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    headers: { 'content-type': 'application/json' },
    status,
  });
}

describe('mobile inbox domain', () => {
  it('extracts and deduplicates links from Chinese share text', () => {
    expect(
      extractMobileInboxUrls(
        '抖音 https://v.douyin.com/abc。 再看 https://v.douyin.com/abc\nhttps://xhslink.com/xyz）',
      ),
    ).toEqual({
      ignoredCount: 0,
      urls: ['https://v.douyin.com/abc', 'https://xhslink.com/xyz'],
    });
  });

  it('rejects a submission with no usable http links', async () => {
    await expect(submitMobileInbox(
      createStore(),
      { note: '', text: '只有普通分享文案和 ftp://example.com/file' },
      NOW,
    )).rejects.toMatchObject({
      code: 'no_valid_links',
      status: 400,
    });
  });

  it('enforces note and text length boundaries', async () => {
    await expect(
      submitMobileInbox(createStore(), { note: 'a'.repeat(501), text: '' }, NOW),
    ).rejects.toMatchObject({ code: 'note_too_long', status: 413 });

    await expect(
      submitMobileInbox(
        createStore(),
        { note: 'a'.repeat(500), text: `https://example.com/${'b'.repeat(29_980)}` },
        NOW,
      ),
    ).resolves.toMatchObject({ addedCount: 1, pendingCount: 2 });

    await expect(
      submitMobileInbox(
        createStore(),
        { note: 'a'.repeat(500), text: `https://example.com/${'b'.repeat(29_981)}` },
        NOW,
      ),
    ).rejects.toMatchObject({ code: 'text_too_long', status: 413 });
  });

  it.each([
    ['https://www.douyin.com/video/123', '抖音'],
    ['https://www.xiaohongshu.com/explore/abc', '小红书'],
    ['https://www.bilibili.com/video/BV1xx411c7mD/', 'B站'],
    ['https://b23.tv/abc123', 'B站'],
    ['https://channels.weixin.qq.com/s/abc', '视频号'],
    ['https://www.youtube.com/watch?v=abc123xyz90', 'YouTube'],
    ['https://youtu.be/abc123xyz90', 'YouTube'],
    ['https://www.instagram.com/reel/ABCdef123/', 'Instagram'],
    ['https://example.com/other', '未知'],
  ] as const)('detects %s as %s', async (url, expectedPlatform) => {
    const result = await submitMobileInbox(createStore(), { note: '', text: url }, NOW);
    expect(result.items[0]?.platform).toBe(expectedPlatform);
  });

  it('reads at most 50 items sorted by submittedAt descending and counts only strict 待扒取', async () => {
    const seenLimits: number[] = [];
    const items: MobileInboxItemDto[] = Array.from({ length: 51 }, (_, index) => {
      const submittedAt = new Date(Date.UTC(2026, 6, 17, 12, 0, 0) - index * 60_000);
      return {
        id: `item-${index + 1}`,
        note: `note-${index + 1}`,
        platform: '未知',
        status:
          index === 0 ? '待扒取'
          : index === 1 ? '待扒取中'
          : index === 50 ? '待扒取'
          : '已完成',
        submittedAt: submittedAt.toISOString(),
        url: `https://example.com/${index + 1}`,
      };
    }).reverse();

    const store: MobileInboxStore = {
      async listRecent(limit: number): Promise<MobileInboxItemDto[]> {
        seenLimits.push(limit);
        return [...items];
      },
      async upsert(): Promise<{ created: boolean; item: MobileInboxItemDto }> {
        throw new Error('not used');
      },
    };

    const snapshot = await readMobileInbox(store);

    expect(seenLimits).toEqual([50]);
    expect(snapshot.items).toHaveLength(50);
    expect(snapshot.items[0]?.submittedAt).toBe('2026-07-17T12:00:00.000Z');
    expect(snapshot.items[49]?.submittedAt).toBe('2026-07-17T11:11:00.000Z');
    expect(snapshot.pendingCount).toBe(1);
  });

  it('rejects more than 20 valid links', async () => {
    const store = createStore();
    const text = Array.from({ length: 21 }, (_, index) => `https://example.com/${index}`).join('\n');
    await expect(submitMobileInbox(store, { note: '', text }, NOW))
      .rejects.toMatchObject({ code: 'too_many_links', status: 400 });
  });

  it('returns added existing ignored and pending counts', async () => {
    const result = await submitMobileInbox(
      createStore(),
      {
        note: '开头可参考',
        text: '文案 https://v.douyin.com/new 无效 ftp://bad https://xhslink.com/existing',
      },
      NOW,
    );
    expect(result).toMatchObject({
      addedCount: 1,
      existingCount: 1,
      ignoredCount: 1,
      pendingCount: 2,
    });
  });
});

describe('mobile inbox Worker routes', () => {
  it('creates only the allowed Feishu fields', async () => {
    const store = createStore();
    const app = createWorkerApp(services({ mobileInbox: store }));

    const response = await app.request('/api/mobile-inbox', {
      body: JSON.stringify({ note: '参考', text: 'https://v.douyin.com/abc' }),
      headers: { 'content-type': 'application/json', origin: APP_ORIGIN },
      method: 'POST',
    });

    expect(response.status).toBe(201);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(store.upserts[0]).toEqual({
      note: '参考',
      platform: '抖音',
      submittedAt: NOW,
      url: 'https://v.douyin.com/abc',
    });
  });

  it('rate limits public submissions by edge IP', async () => {
    const deniedApp = createWorkerApp(services({
      mobileInboxRateLimiter: new TestRateLimiter(false),
    }));

    const response = await deniedApp.request('/api/mobile-inbox', {
      body: JSON.stringify({ note: '', text: 'https://example.com/1' }),
      headers: {
        'cf-connecting-ip': '203.0.113.8',
        'content-type': 'application/json',
        origin: APP_ORIGIN,
      },
      method: 'POST',
    });

    expect(response.status).toBe(429);
    expect(response.headers.get('cache-control')).toBe('no-store');
  });

  it('rejects an oversized body before parsing JSON', async () => {
    const store = createStore();
    const app = createWorkerApp(services({ mobileInbox: store }));

    const response = await app.request('/api/mobile-inbox', {
      body: JSON.stringify({ note: '', text: 'x'.repeat(32_769) }),
      headers: { 'content-type': 'application/json', origin: APP_ORIGIN },
      method: 'POST',
    });

    expect(response.status).toBe(413);
    expect(store.upserts).toEqual([]);
  });

  it('maps Feishu failures without leaking credentials', async () => {
    const failingStore = createStore();
    failingStore.error = new Error('tenant_access_token=secret-value');
    const app = createWorkerApp(services({ mobileInbox: failingStore }));

    const response = await app.request('/api/mobile-inbox', {
      headers: { origin: APP_ORIGIN },
    });

    expect(response.status).toBe(502);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.text()).not.toContain('secret-value');
  });

  it('rate limits reads by edge IP', async () => {
    const limiter = new TestRateLimiter();
    const app = createWorkerApp(services({ mobileInboxRateLimiter: limiter }));

    const response = await app.request('/api/mobile-inbox', {
      headers: {
        'cf-connecting-ip': '203.0.113.9',
        origin: APP_ORIGIN,
      },
    });

    expect(response.status).toBe(200);
    expect(limiter.keys).toEqual(['read:203.0.113.9']);
    expect(response.headers.get('cache-control')).toBe('no-store');
  });
});

describe('readMobileInboxJson', () => {
  it('validates raw UTF-8 bytes before parsing JSON', async () => {
    await expect(readMobileInboxJson(new Request('https://example.com', {
      body: Uint8Array.from([0xff]),
      method: 'POST',
    }), 32 * 1024)).rejects.toMatchObject({
      code: 'invalid_json',
      status: 400,
    });

    await expect(readMobileInboxJson(new Request('https://example.com', {
      body: new TextEncoder().encode(JSON.stringify({ note: '', text: '你'.repeat(11) })),
      method: 'POST',
    }), 32)).rejects.toMatchObject({
      code: 'body_too_large',
      status: 413,
    });
  });
});

describe('Feishu mobile inbox store', () => {
  it('returns an existing record when Feishu rejects a repeated client token', async () => {
    let listRequests = 0;
    const url = 'https://example.com/repeated-client-token';
    const store = createFeishuMobileInboxStore({
      appId: 'repeated-client-token-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl = String(input);
        if (requestUrl.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token' });
        }
        if (init?.method === 'GET') {
          listRequests += 1;
          return jsonResponse({
            code: 0,
            data: {
              has_more: false,
              items: listRequests === 1
                ? []
                : [{
                    fields: {
                      '作品链接': url,
                      '平台': '未知',
                      '抓取状态': '待扒取',
                      '提交时间': NOW.toISOString(),
                    },
                    record_id: 'record-existing',
                  }],
            },
          });
        }
        if (init?.method === 'POST') {
          return jsonResponse(
            { code: 1254608, msg: 'Same API requests are submitted repeatedly' },
            403,
          );
        }
        if (init?.method === 'PUT') {
          return jsonResponse({ code: 0, data: {} });
        }
        throw new Error(`unexpected request ${init?.method} ${requestUrl}`);
      },
      tableId: 'table-id',
    });

    await expect(store.upsert({
      note: '重复提交',
      platform: '未知',
      submittedAt: NOW,
      url,
    })).resolves.toMatchObject({
      created: false,
      item: { id: 'record-existing' },
    });
    expect(listRequests).toBe(2);
  });

  it('deduplicates all records before limiting the mobile overview', async () => {
    const duplicateUrl = 'https://example.com/duplicate-overview';
    const uniqueLastUrl = 'https://example.com/unique-last';
    const store = createFeishuMobileInboxStore({
      appId: 'deduplicated-overview-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl = new URL(String(input));
        if (requestUrl.pathname.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token' });
        }
        if (init?.method === 'GET') {
          const secondPage = requestUrl.searchParams.get('page_token') === 'next-page';
          return jsonResponse({
            code: 0,
            data: secondPage
              ? {
                  has_more: false,
                  items: [{
                    fields: {
                      '作品链接': uniqueLastUrl,
                      '平台': '未知',
                      '抓取状态': '待扒取',
                      '提交时间': '2026-07-15T00:00:00.000Z',
                    },
                    record_id: 'record-unique-last',
                  }],
                }
              : {
                  has_more: true,
                  items: [
                    {
                      fields: {
                        '作品链接': duplicateUrl,
                        '平台': '未知',
                        '抓取状态': '待扒取',
                        '提交时间': NOW.toISOString(),
                      },
                      record_id: 'record-a',
                    },
                    {
                      fields: {
                        '作品链接': duplicateUrl,
                        '平台': '未知',
                        '抓取状态': '基础信息成功',
                        '提交时间': '2026-07-16T00:00:00.000Z',
                      },
                      record_id: 'record-z',
                    },
                    ...Array.from({ length: 48 }, (_, index) => ({
                      fields: {
                        '作品链接': `https://example.com/unique-${index}`,
                        '平台': '未知',
                        '抓取状态': '待扒取',
                        '提交时间': '2026-07-17T00:00:00.000Z',
                      },
                      record_id: `record-unique-${index}`,
                    })),
                  ],
                  page_token: 'next-page',
                },
          });
        }
        throw new Error(`unexpected request ${init?.method} ${requestUrl}`);
      },
      tableId: 'table-id',
    });

    const items = await store.listRecent(50);

    expect(items).toHaveLength(50);
    expect(items.filter((item) => item.url === duplicateUrl)).toEqual([
      expect.objectContaining({ id: 'record-z', status: '基础信息成功' }),
    ]);
    expect(items.some((item) => item.url === uniqueLastUrl)).toBe(true);
  });

  it('finds a duplicate beyond the first 50 records with batch-level scans', async () => {
    let listRequests = 0;
    let createRequests = 0;
    let updateRequests = 0;
    const duplicateUrl = 'https://example.com/older-duplicate';
    const store = createFeishuMobileInboxStore({
      appId: 'paged-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input));
        if (url.pathname.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token-paged' });
        }
        if (init?.method === 'GET') {
          listRequests += 1;
          const secondPage = url.searchParams.get('page_token') === 'next-page';
          return jsonResponse({
            code: 0,
            data: secondPage
              ? {
                  has_more: false,
                  items: [{
                    fields: {
                      '作品链接': duplicateUrl,
                      '平台': '未知',
                      '抓取状态': '待扒取',
                      '提交时间': '2026-07-16T00:00:00.000Z',
                    },
                    record_id: 'record-older',
                  }],
                }
              : {
                  has_more: true,
                  items: Array.from({ length: 50 }, (_, index) => ({
                    fields: {
                      '作品链接': `https://example.com/recent-${index}`,
                      '平台': '未知',
                      '抓取状态': '待扒取',
                      '提交时间': '2026-07-17T00:00:00.000Z',
                    },
                    record_id: `record-${index}`,
                  })),
                  page_token: 'next-page',
                },
          });
        }
        if (init?.method === 'PUT') {
          updateRequests += 1;
          return jsonResponse({ code: 0, data: {} });
        }
        if (init?.method === 'POST') {
          createRequests += 1;
          return jsonResponse({ code: 0, data: {} });
        }
        throw new Error(`unexpected request ${init?.method} ${url}`);
      },
      tableId: 'table-id',
    });

    const results = await store.upsertMany?.([
      { note: '重复', platform: '未知', submittedAt: NOW, url: duplicateUrl },
      { note: '新增', platform: '未知', submittedAt: NOW, url: 'https://example.com/new-batch' },
    ]);

    expect(results?.map((result) => result.created)).toEqual([false, true]);
    expect(listRequests).toBe(4);
    expect(updateRequests).toBe(1);
    expect(createRequests).toBe(1);
  });

  it('reuses a tenant token across store instances using the same fetch transport', async () => {
    let tokenRequests = 0;
    const fetcher = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/tenant_access_token/internal')) {
        tokenRequests += 1;
        return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'shared-token' });
      }
      if (init?.method === 'GET') {
        return jsonResponse({ code: 0, data: { has_more: false, items: [] } });
      }
      throw new Error(`unexpected request ${init?.method} ${url}`);
    };
    const config = {
      appId: 'shared-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: fetcher,
      tableId: 'table-id',
    };

    await createFeishuMobileInboxStore(config).listRecent(50);
    await createFeishuMobileInboxStore(config).listRecent(50);

    expect(tokenRequests).toBe(1);
  });

  it('reconciles a concurrently created duplicate without deleting records', async () => {
    const survivorWrites: unknown[] = [];
    let listRequests = 0;
    const url = 'https://example.com/concurrent';
    const store = createFeishuMobileInboxStore({
      appId: 'concurrent-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl = String(input);
        if (requestUrl.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'concurrent-token' });
        }
        if (init?.method === 'GET') {
          listRequests += 1;
          return jsonResponse({
            code: 0,
            data: {
              has_more: false,
              items: listRequests === 1
                ? []
                : [
                    {
                      fields: {
                        '作品链接': url,
                        '平台': '未知',
                        '抓取状态': '待扒取',
                        '提交时间': NOW.toISOString(),
                      },
                      record_id: 'record-a',
                    },
                    {
                      fields: {
                        '作品链接': url,
                        '平台': '未知',
                        '抓取状态': '待扒取',
                        '提交时间': NOW.toISOString(),
                      },
                      record_id: 'record-b',
                    },
                  ],
            },
          });
        }
        if (init?.method === 'POST') {
          return jsonResponse({
            code: 0,
            data: {
              record: {
                fields: {
                  '作品链接': url,
                  '平台': '未知',
                  '抓取状态': '待扒取',
                  '提交时间': NOW.toISOString(),
                },
                record_id: 'record-b',
              },
            },
          });
        }
        if (init?.method === 'PUT') {
          survivorWrites.push(JSON.parse(String(init.body)));
          return jsonResponse({ code: 0, data: {} });
        }
        throw new Error(`unexpected request ${init?.method} ${requestUrl}`);
      },
      tableId: 'table-id',
    });

    const result = await store.upsert({
      note: '',
      platform: '未知',
      submittedAt: NOW,
      url,
    });

    expect(result).toMatchObject({ created: false, item: { id: 'record-a' } });
    expect(survivorWrites).toEqual([{
      fields: {
        '提交时间': NOW.toISOString(),
        '手机备注': '',
        '来源': '手机收集箱',
      },
    }]);
    expect(listRequests).toBe(2);
  });

  it('keeps a processed record when a concurrent pending record has a smaller id', async () => {
    const survivorWrites: unknown[] = [];
    let listRequests = 0;
    const url = 'https://example.com/concurrent-processed';
    const store = createFeishuMobileInboxStore({
      appId: 'concurrent-processed-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl = String(input);
        if (requestUrl.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token' });
        }
        if (init?.method === 'GET') {
          listRequests += 1;
          return jsonResponse({
            code: 0,
            data: {
              has_more: false,
              items: listRequests === 1
                ? []
                : [
                    {
                      fields: {
                        '作品链接': url,
                        '平台': '未知',
                        '抓取状态': '待扒取',
                        '提交时间': NOW.toISOString(),
                      },
                      record_id: 'record-a',
                    },
                    {
                      fields: {
                        '作品链接': url,
                        '平台': '未知',
                        '抓取状态': '基础信息成功',
                        '提交时间': '2026-07-16T00:00:00.000Z',
                        '手机备注': '旧备注',
                      },
                      record_id: 'record-z',
                    },
                  ],
            },
          });
        }
        if (init?.method === 'POST') {
          return jsonResponse({
            code: 0,
            data: {
              record: {
                fields: {
                  '作品链接': url,
                  '平台': '未知',
                  '抓取状态': '待扒取',
                  '提交时间': NOW.toISOString(),
                  '手机备注': '新备注',
                },
                record_id: 'record-a',
              },
            },
          });
        }
        if (init?.method === 'PUT') {
          survivorWrites.push(JSON.parse(String(init.body)));
          return jsonResponse({ code: 0, data: {} });
        }
        throw new Error(`unexpected request ${init?.method} ${requestUrl}`);
      },
      tableId: 'table-id',
    });

    const result = await store.upsert({
      note: '新备注',
      platform: '未知',
      submittedAt: NOW,
      url,
    });

    expect(result).toMatchObject({
      created: false,
      item: { id: 'record-z', status: '基础信息成功' },
    });
    expect(survivorWrites).toEqual([{
      fields: {
        '提交时间': NOW.toISOString(),
        '手机备注': '新备注',
        '来源': '手机收集箱',
      },
    }]);
  });

  it('uses the most processed historical duplicate without deleting records', async () => {
    const updatedRecordIds: string[] = [];
    const url = 'https://example.com/historical-duplicate';
    const store = createFeishuMobileInboxStore({
      appId: 'historical-duplicate-app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl = String(input);
        if (requestUrl.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token' });
        }
        if (init?.method === 'GET') {
          return jsonResponse({
            code: 0,
            data: {
              has_more: false,
              items: [
                {
                  fields: {
                    '作品链接': url,
                    '平台': '未知',
                    '抓取状态': '待扒取',
                    '提交时间': NOW.toISOString(),
                  },
                  record_id: 'record-a',
                },
                {
                  fields: {
                    '作品链接': url,
                    '平台': '未知',
                    '抓取状态': '基础信息成功',
                    '提交时间': '2026-07-16T00:00:00.000Z',
                  },
                  record_id: 'record-z',
                },
              ],
            },
          });
        }
        if (init?.method === 'PUT') {
          updatedRecordIds.push(requestUrl.split('/').at(-1) ?? '');
          return jsonResponse({ code: 0, data: {} });
        }
        throw new Error(`unexpected request ${init?.method} ${requestUrl}`);
      },
      tableId: 'table-id',
    });

    const result = await store.upsert({
      note: '更新备注',
      platform: '未知',
      submittedAt: NOW,
      url,
    });

    expect(result).toMatchObject({
      created: false,
      item: { id: 'record-z', status: '基础信息成功' },
    });
    expect(updatedRecordIds).toEqual(['record-z']);
  });

  it('creates and updates only the allowed Feishu fields', async () => {
    const calls: Array<{ body: unknown; method: string; url: string }> = [];
    const store = createFeishuMobileInboxStore({
      appId: 'app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          body: init?.body ? JSON.parse(String(init.body)) : null,
          method: init?.method ?? 'GET',
          url,
        });
        if (url.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token-1' });
        }
        if (init?.method === 'GET') {
          return jsonResponse({ code: 0, data: { has_more: false, items: [] } });
        }
        if (init?.method === 'POST') {
          return jsonResponse({
            code: 0,
            data: {
              record: {
                fields: {
                  '作品链接': 'https://v.douyin.com/abc',
                  '平台': '抖音',
                  '抓取状态': '待扒取',
                  '提交时间': '2026-07-17T00:00:00.000Z',
                  '手机备注': '参考',
                  '来源': '手机收集箱',
                },
                record_id: 'record-1',
              },
            },
          });
        }
        throw new Error(`unexpected request ${init?.method} ${url}`);
      },
      tableId: 'table-id',
    });

    await store.upsert({
      note: '参考',
      platform: '抖音',
      submittedAt: NOW,
      url: 'https://v.douyin.com/abc',
    });

    const recordWrite = calls.find((call) => call.method === 'POST'
      && call.url.includes('/records'));
    expect(recordWrite?.body).toEqual({
      fields: {
        '作品链接': 'https://v.douyin.com/abc',
        '平台': '抖音',
        '抓取状态': '待扒取',
        '提交时间': NOW.toISOString(),
        '手机备注': '参考',
        '来源': '手机收集箱',
      },
    });
    expect(new URL(recordWrite!.url).searchParams.get('client_token'))
      .toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it('refreshes a tenant token only once after a 401', async () => {
    let tokenRequests = 0;
    let listRequests = 0;
    const store = createFeishuMobileInboxStore({
      appId: 'app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/tenant_access_token/internal')) {
          tokenRequests += 1;
          return jsonResponse({
            code: 0,
            expire: 7200,
            tenant_access_token: `token-${tokenRequests}`,
          });
        }
        if (init?.method === 'GET') {
          listRequests += 1;
          if (listRequests === 1) {
            return jsonResponse({ code: 999, msg: 'expired' }, 401);
          }
          return jsonResponse({ code: 0, data: { has_more: false, items: [] } });
        }
        throw new Error(`unexpected request ${init?.method} ${url}`);
      },
      tableId: 'table-id',
    });

    await expect(store.listRecent(50)).resolves.toEqual([]);
    expect(tokenRequests).toBe(2);
    expect(listRequests).toBe(2);
  });

  it('treats an omitted items field as an empty Feishu table', async () => {
    const store = createFeishuMobileInboxStore({
      appId: 'app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token-1' });
        }
        if (init?.method === 'GET') {
          return jsonResponse({ code: 0, data: { has_more: false, total: 0 } });
        }
        throw new Error(`unexpected request ${init?.method} ${url}`);
      },
      tableId: 'table-id',
    });

    await expect(store.listRecent(50)).resolves.toEqual([]);
  });

  it('preserves an existing processed status when upserting a known URL', async () => {
    const writes: unknown[] = [];
    const store = createFeishuMobileInboxStore({
      appId: 'app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/tenant_access_token/internal')) {
          return jsonResponse({ code: 0, expire: 7200, tenant_access_token: 'token-1' });
        }
        if (init?.method === 'GET') {
          return jsonResponse({
            code: 0,
            data: {
              has_more: false,
              items: [{
                fields: {
                  '作品链接': 'https://example.com/done',
                  '平台': '未知',
                  '抓取状态': '已完成',
                  '提交时间': '2026-07-16T00:00:00.000Z',
                  '手机备注': '旧',
                  '来源': '手机收集箱',
                },
                record_id: 'record-1',
              }],
            },
          });
        }
        if (init?.method === 'PUT') {
          writes.push(JSON.parse(String(init.body)));
          return jsonResponse({
            code: 0,
            data: {
              record: {
                fields: {
                  '作品链接': 'https://example.com/done',
                  '平台': '未知',
                  '抓取状态': '已完成',
                  '提交时间': NOW.toISOString(),
                  '手机备注': '新',
                  '来源': '手机收集箱',
                },
                record_id: 'record-1',
              },
            },
          });
        }
        throw new Error(`unexpected request ${init?.method} ${url}`);
      },
      tableId: 'table-id',
    });

    await expect(store.upsert({
      note: '新',
      platform: '未知',
      submittedAt: NOW,
      url: 'https://example.com/done',
    })).resolves.toMatchObject({ created: false });

    expect(writes[0]).toEqual({
      fields: {
        '提交时间': NOW.toISOString(),
        '手机备注': '新',
        '来源': '手机收集箱',
      },
    });
  });

  it('maps upstream errors to a sanitized service error', async () => {
    const store = createFeishuMobileInboxStore({
      appId: 'app-id',
      appSecret: 'app-secret',
      appToken: 'app-token',
      fetch: async () => {
        throw new Error('tenant_access_token=secret-value');
      },
      tableId: 'table-id',
    });

    await expect(store.listRecent(50)).rejects.toMatchObject({
      code: 'feishu_unavailable',
      message: '手机收集服务暂时不可用',
      status: 502,
    });
  });
});
