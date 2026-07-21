import type {
  MobileInboxItemDto,
  MobileInboxSnapshotDto,
  MobileInboxSubmitRequest,
  MobileInboxSubmitResult,
} from '../../../packages/shared/src/contracts.js';

export interface MobileInboxStore {
  listRecent(limit: number): Promise<MobileInboxItemDto[]>;
  upsert(input: {
    note: string;
    platform: string;
    submittedAt: Date;
    url: string;
  }): Promise<{ created: boolean; item: MobileInboxItemDto }>;
  upsertMany?(inputs: Array<{
    note: string;
    platform: string;
    submittedAt: Date;
    url: string;
  }>): Promise<Array<{ created: boolean; item: MobileInboxItemDto }>>;
}

export class MobileInboxError extends Error {
  constructor(
    readonly status: 400 | 413 | 429 | 502 | 503,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'MobileInboxError';
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

const FEISHU_ORIGIN = 'https://open.feishu.cn';
const FEISHU_SOURCE = '手机收集箱';
const FEISHU_PENDING_STATUS = '待扒取';
const FEISHU_REPEATED_REQUEST_CODE = 1254608;
const FEISHU_FIELDS = {
  error: '错误信息',
  note: '手机备注',
  platform: '平台',
  source: '来源',
  status: '抓取状态',
  submittedAt: '提交时间',
  url: '作品链接',
} as const;
const MAX_NOTE_LENGTH = 500;
const MAX_TEXT_LENGTH = 30_000;
const MAX_LINKS = 20;
const MAX_ITEMS = 50;
const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/gi;
const SCHEME_URL_PATTERN = /\b[a-z][a-z0-9+.-]*:\/\/[^\s<>"'`]+/gi;
const TRAILING_PUNCTUATION = /[。；，、）)\]}>.,!?;:]+$/u;
const FEISHU_TOKEN_CACHES = new WeakMap<
  typeof fetch,
  Map<string, { expiresAtMs: number; value: string }>
>();

function normalizeUrl(value: string): string {
  return value.replace(TRAILING_PUNCTUATION, '');
}

function normalizeLookupUrl(value: string): string {
  return normalizeUrl(value.trim());
}

function assertLength(value: string, max: number, status: 413, code: string): void {
  if (Array.from(value).length > max) {
    throw new MobileInboxError(status, code, code === 'note_too_long'
      ? 'note is too long'
      : 'text is too long');
  }
}

function detectPlatform(url: string): string {
  const host = new URL(url).hostname.toLowerCase();
  if (host.includes('douyin') || host.includes('iesdouyin')) {
    return '抖音';
  }
  if (host.includes('xiaohongshu') || host.includes('xhslink')) {
    return '小红书';
  }
  if (host.includes('bilibili') || host.endsWith('b23.tv') || host.endsWith('bili2233.cn')) {
    return 'B站';
  }
  if (host.includes('weixin.qq.com') || host.includes('channels.weixin')) {
    return '视频号';
  }
  if (host.includes('youtube.com') || host.endsWith('youtu.be')) {
    return 'YouTube';
  }
  if (host.includes('instagram.com')) {
    return 'Instagram';
  }
  return '未知';
}

function sanitizeFeishuError(): MobileInboxError {
  return new MobileInboxError(
    502,
    'feishu_unavailable',
    '手机收集服务暂时不可用',
  );
}

function invalidJson(): MobileInboxError {
  return new MobileInboxError(400, 'invalid_json', 'invalid JSON body');
}

function parseMobileInboxBody(value: unknown): MobileInboxSubmitRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new MobileInboxError(400, 'invalid_request', 'request body is invalid');
  }
  const body = value as Partial<MobileInboxSubmitRequest>;
  if (typeof body.note !== 'string' || typeof body.text !== 'string') {
    throw new MobileInboxError(400, 'invalid_request', 'note and text are required');
  }
  return { note: body.note, text: body.text };
}

function feishuString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function feishuSubmittedAt(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return new Date(value).toISOString();
  }
  return '';
}

interface FeishuRecord {
  fields?: Record<string, unknown>;
  record_id?: unknown;
}

interface FeishuApiResponse {
  code?: unknown;
  data?: unknown;
  expire?: unknown;
  tenant_access_token?: unknown;
}

class FeishuApiError extends Error {
  constructor(readonly code: number) {
    super(`Feishu API returned error ${code}`);
    this.name = 'FeishuApiError';
  }
}

function mapFeishuRecord(record: FeishuRecord): MobileInboxItemDto {
  const fields = record.fields ?? {};
  return {
    id: feishuString(record.record_id),
    note: feishuString(fields[FEISHU_FIELDS.note]),
    platform: feishuString(fields[FEISHU_FIELDS.platform]),
    status: feishuString(fields[FEISHU_FIELDS.status]),
    submittedAt: feishuSubmittedAt(fields[FEISHU_FIELDS.submittedAt]),
    url: feishuString(fields[FEISHU_FIELDS.url]),
  };
}

async function readFeishuJson(response: Response): Promise<FeishuApiResponse> {
  const value = await response.json() as FeishuApiResponse;
  if (value.code !== 0) {
    throw new FeishuApiError(typeof value.code === 'number' ? value.code : -1);
  }
  return value;
}

function compareSubmittedAtDesc(left: MobileInboxItemDto, right: MobileInboxItemDto): number {
  const leftTime = Date.parse(left.submittedAt);
  const rightTime = Date.parse(right.submittedAt);
  if (Number.isNaN(leftTime) && Number.isNaN(rightTime)) return 0;
  if (Number.isNaN(leftTime)) return 1;
  if (Number.isNaN(rightTime)) return -1;
  return rightTime - leftTime;
}

function statusProgressRank(status: string): number {
  if (status.includes('成功') || status.includes('完成')) return 3;
  if (status && status !== FEISHU_PENDING_STATUS && status !== '扒取中') return 2;
  if (status === '扒取中') return 1;
  return 0;
}

function preferredRecord(records: MobileInboxItemDto[]): MobileInboxItemDto | undefined {
  return [...records].sort((left, right) => {
    const progress = statusProgressRank(right.status) - statusProgressRank(left.status);
    return progress || left.id.localeCompare(right.id);
  })[0];
}

function deduplicateRecords(records: MobileInboxItemDto[]): MobileInboxItemDto[] {
  const recordsByUrl = new Map<string, MobileInboxItemDto[]>();
  for (const item of records) {
    const normalizedUrl = normalizeLookupUrl(item.url);
    const matches = recordsByUrl.get(normalizedUrl) ?? [];
    matches.push(item);
    recordsByUrl.set(normalizedUrl, matches);
  }
  return [...recordsByUrl.values()]
    .map((matches) => preferredRecord(matches))
    .filter((item): item is MobileInboxItemDto => item !== undefined);
}

async function deterministicClientToken(value: string): Promise<string> {
  const bytes = new Uint8Array(
    await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)),
  ).slice(0, 16);
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-');
}

export function extractMobileInboxUrls(text: string): { urls: string[]; ignoredCount: number } {
  const urls: string[] = [];
  const seen = new Set<string>();

  for (const match of text.matchAll(URL_PATTERN)) {
    const normalized = normalizeUrl(match[0]);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    urls.push(normalized);
  }

  let ignoredCount = 0;
  for (const match of text.matchAll(SCHEME_URL_PATTERN)) {
    if (!/^https?:\/\//i.test(match[0])) {
      ignoredCount += 1;
    }
  }

  return { ignoredCount, urls };
}

export async function readMobileInboxJson(
  request: Request,
  maxBytes: number,
): Promise<MobileInboxSubmitRequest> {
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.byteLength > maxBytes) {
    throw new MobileInboxError(413, 'body_too_large', 'request body is too large');
  }

  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw invalidJson();
  }

  try {
    return parseMobileInboxBody(JSON.parse(text) as unknown);
  } catch (error) {
    if (error instanceof MobileInboxError) throw error;
    throw invalidJson();
  }
}

export async function readMobileInbox(
  store: MobileInboxStore,
): Promise<MobileInboxSnapshotDto> {
  const items = await store.listRecent(MAX_ITEMS);
  const sortedItems = [...items]
    .sort(compareSubmittedAtDesc)
    .slice(0, MAX_ITEMS);
  return {
    items: sortedItems,
    pendingCount: sortedItems.filter((item) => item.status === '待扒取').length,
  };
}

export async function submitMobileInbox(
  store: MobileInboxStore,
  request: MobileInboxSubmitRequest,
  submittedAt: Date,
): Promise<MobileInboxSubmitResult> {
  assertLength(request.note, MAX_NOTE_LENGTH, 413, 'note_too_long');
  assertLength(request.text, MAX_TEXT_LENGTH, 413, 'text_too_long');

  const { ignoredCount, urls } = extractMobileInboxUrls(request.text);
  if (urls.length === 0) {
    throw new MobileInboxError(400, 'no_valid_links', '没有识别到可用链接');
  }
  if (urls.length > MAX_LINKS) {
    throw new MobileInboxError(400, 'too_many_links', 'too many links');
  }

  const inputs = urls.map((url) => ({
      note: request.note,
      platform: detectPlatform(url),
      submittedAt,
      url,
  }));
  const upsertResults = store.upsertMany
    ? await store.upsertMany(inputs)
    : await Promise.all(inputs.map((input) => store.upsert(input)));
  let addedCount = 0;
  let existingCount = 0;
  for (const result of upsertResults) {
    if (result.created) {
      addedCount += 1;
    } else {
      existingCount += 1;
    }
  }

  const snapshot = await readMobileInbox(store);
  return {
    ...snapshot,
    addedCount,
    existingCount,
    ignoredCount,
  };
}

export interface FeishuMobileInboxStoreConfig {
  appId: string;
  appSecret: string;
  appToken: string;
  fetch?: typeof fetch;
  tableId: string;
}

export function createFeishuMobileInboxStore(
  config: FeishuMobileInboxStoreConfig,
): MobileInboxStore {
  const fetchFn = config.fetch ?? fetch;
  const existingTransportCache = FEISHU_TOKEN_CACHES.get(fetchFn);
  const transportCache = existingTransportCache
    ?? new Map<string, { expiresAtMs: number; value: string }>();
  if (!existingTransportCache) {
    FEISHU_TOKEN_CACHES.set(fetchFn, transportCache);
  }
  const tokenCacheKey = `${FEISHU_ORIGIN}:${config.appId}`;

  async function tenantToken(forceRefresh = false): Promise<string> {
    const nowMs = Date.now();
    const cachedToken = transportCache.get(tokenCacheKey);
    if (!forceRefresh && cachedToken && cachedToken.expiresAtMs > nowMs) {
      return cachedToken.value;
    }
    const response = await fetchFn(`${FEISHU_ORIGIN}/open-apis/auth/v3/tenant_access_token/internal`, {
      body: JSON.stringify({
        app_id: config.appId,
        app_secret: config.appSecret,
      }),
      headers: { 'content-type': 'application/json' },
      method: 'POST',
    });
    if (!response.ok) throw new Error('Feishu token request failed');
    const body = await readFeishuJson(response);
    if (typeof body.tenant_access_token !== 'string' || !body.tenant_access_token) {
      throw new Error('Feishu token response is invalid');
    }
    const expireSeconds = typeof body.expire === 'number' ? body.expire : 0;
    transportCache.set(tokenCacheKey, {
      expiresAtMs: nowMs + Math.max(0, expireSeconds - 60) * 1000,
      value: body.tenant_access_token,
    });
    return body.tenant_access_token;
  }

  function recordsUrl(recordId?: string): URL {
    const path = recordId
      ? `/open-apis/bitable/v1/apps/${encodeURIComponent(config.appToken)}/tables/${encodeURIComponent(config.tableId)}/records/${encodeURIComponent(recordId)}`
      : `/open-apis/bitable/v1/apps/${encodeURIComponent(config.appToken)}/tables/${encodeURIComponent(config.tableId)}/records`;
    return new URL(path, FEISHU_ORIGIN);
  }

  async function feishuRequest(
    url: URL,
    init: RequestInit,
    retried = false,
  ): Promise<FeishuApiResponse> {
    const token = await tenantToken(retried);
    const headers = new Headers(init.headers);
    headers.set('authorization', `Bearer ${token}`);
    if (init.body !== undefined) {
      headers.set('content-type', 'application/json');
    }
    const response = await fetchFn(url, { ...init, headers });
    if (response.status === 401 && !retried) {
      transportCache.delete(tokenCacheKey);
      return feishuRequest(url, init, true);
    }
    const body = await readFeishuJson(response);
    if (!response.ok) throw new Error('Feishu record request failed');
    return body;
  }

  async function listRecordsUnsafe(limit?: number): Promise<MobileInboxItemDto[]> {
    const boundedLimit = limit === undefined ? Number.POSITIVE_INFINITY : Math.max(1, limit);
    const items: MobileInboxItemDto[] = [];
    let pageToken: string | null = null;
    do {
      const url = recordsUrl();
      url.searchParams.set('page_size', String(Math.min(MAX_ITEMS, boundedLimit - items.length)));
      url.searchParams.set('field_names', JSON.stringify(Object.values(FEISHU_FIELDS)));
      if (pageToken) url.searchParams.set('page_token', pageToken);
      const body = await feishuRequest(url, { method: 'GET' });
      const data = body.data as {
        has_more?: unknown;
        items?: unknown;
        page_token?: unknown;
        total?: unknown;
      } | undefined;
      if (!data) throw new Error('Feishu list response is invalid');
      const records = data.items === undefined
        && data.has_more === false
        && data.total === 0
        ? []
        : data.items;
      if (!Array.isArray(records)) throw new Error('Feishu list response is invalid');
      items.push(...records.map((record) => mapFeishuRecord(record as FeishuRecord)));
      pageToken = data.has_more === true && typeof data.page_token === 'string'
        ? data.page_token
        : null;
    } while (pageToken && items.length < boundedLimit);
    return limit === undefined ? items : items.slice(0, limit);
  }

  async function writeUpsert(
    input: {
      note: string;
      platform: string;
      submittedAt: Date;
      url: string;
    },
    existing?: MobileInboxItemDto,
  ): Promise<{ created: boolean; item: MobileInboxItemDto }> {
    const normalizedUrl = normalizeLookupUrl(input.url);
    if (existing) {
      const body = {
        fields: {
          [FEISHU_FIELDS.submittedAt]: input.submittedAt.toISOString(),
          [FEISHU_FIELDS.note]: input.note,
          [FEISHU_FIELDS.source]: FEISHU_SOURCE,
        },
      };
      const response = await feishuRequest(recordsUrl(existing.id), {
        body: JSON.stringify(body),
        method: 'PUT',
      });
      const record = (response.data as { record?: FeishuRecord } | undefined)?.record;
      return {
        created: false,
        item: record ? mapFeishuRecord(record) : {
          ...existing,
          note: input.note,
          submittedAt: input.submittedAt.toISOString(),
        },
      };
    }

    const createUrl = recordsUrl();
    createUrl.searchParams.set(
      'client_token',
      await deterministicClientToken(`${config.appToken}:${config.tableId}:${normalizedUrl}`),
    );
    let response: FeishuApiResponse;
    try {
      response = await feishuRequest(createUrl, {
        body: JSON.stringify({
          fields: {
            [FEISHU_FIELDS.url]: normalizedUrl,
            [FEISHU_FIELDS.platform]: input.platform,
            [FEISHU_FIELDS.status]: FEISHU_PENDING_STATUS,
            [FEISHU_FIELDS.submittedAt]: input.submittedAt.toISOString(),
            [FEISHU_FIELDS.note]: input.note,
            [FEISHU_FIELDS.source]: FEISHU_SOURCE,
          },
        }),
        method: 'POST',
      });
    } catch (error) {
      if (!(error instanceof FeishuApiError)
        || error.code !== FEISHU_REPEATED_REQUEST_CODE) {
        throw error;
      }
      const existingAfterRepeat = preferredRecord(
        (await listRecordsUnsafe())
          .filter((item) => normalizeLookupUrl(item.url) === normalizedUrl),
      );
      if (!existingAfterRepeat) throw error;
      return writeUpsert(input, existingAfterRepeat);
    }
    const record = (response.data as { record?: FeishuRecord } | undefined)?.record;
    return {
      created: true,
      item: record ? mapFeishuRecord(record) : {
        id: '',
        note: input.note,
        platform: input.platform,
        status: FEISHU_PENDING_STATUS,
        submittedAt: input.submittedAt.toISOString(),
        url: normalizedUrl,
      },
    };
  }

  async function upsertManyUnsafe(inputs: Array<{
    note: string;
    platform: string;
    submittedAt: Date;
    url: string;
  }>): Promise<Array<{ created: boolean; item: MobileInboxItemDto }>> {
    const recordsBeforeWrite = await listRecordsUnsafe();
    const recordsBeforeWriteByUrl = new Map<string, MobileInboxItemDto[]>();
    for (const item of recordsBeforeWrite) {
      const normalizedUrl = normalizeLookupUrl(item.url);
      const matches = recordsBeforeWriteByUrl.get(normalizedUrl) ?? [];
      matches.push(item);
      recordsBeforeWriteByUrl.set(normalizedUrl, matches);
    }
    const existingByUrl = new Map<string, MobileInboxItemDto>();
    for (const [url, matches] of recordsBeforeWriteByUrl) {
      const survivor = preferredRecord(matches);
      if (survivor) existingByUrl.set(url, survivor);
    }
    const results: Array<{ created: boolean; item: MobileInboxItemDto }> = [];
    for (const input of inputs) {
      const normalizedUrl = normalizeLookupUrl(input.url);
      const result = await writeUpsert(input, existingByUrl.get(normalizedUrl));
      existingByUrl.set(normalizedUrl, result.item);
      results.push(result);
    }
    const recordsForReconciliation = results.some((result) => result.created)
      ? await listRecordsUnsafe()
      : recordsBeforeWrite;
    const recordsForReconciliationByUrl = new Map<string, MobileInboxItemDto[]>();
    for (const item of recordsForReconciliation) {
      const normalizedUrl = normalizeLookupUrl(item.url);
      const matches = recordsForReconciliationByUrl.get(normalizedUrl) ?? [];
      matches.push(item);
      recordsForReconciliationByUrl.set(normalizedUrl, matches);
    }
    for (let index = 0; index < inputs.length; index += 1) {
      const normalizedUrl = normalizeLookupUrl(inputs[index]!.url);
      const matches = (recordsForReconciliationByUrl.get(normalizedUrl) ?? [])
        .filter((item) => item.id);
      if (matches.length < 2) continue;
      const survivor = preferredRecord(matches);
      if (!survivor) continue;
      let survivorResult = survivor;
      if (results[index]!.item.id !== survivor.id) {
        survivorResult = (await writeUpsert(inputs[index]!, survivor)).item;
      }
      // Feishu record deletion has no version precondition, so automatic cleanup
      // could race the desktop status writeback and delete a newly processed row.
      results[index] = { created: false, item: survivorResult };
    }
    return results;
  }

  return {
    async listRecent(limit: number): Promise<MobileInboxItemDto[]> {
      try {
        return deduplicateRecords(await listRecordsUnsafe())
          .sort(compareSubmittedAtDesc)
          .slice(0, limit);
      } catch (error) {
        if (error instanceof MobileInboxError) throw error;
        throw sanitizeFeishuError();
      }
    },
    async upsert(input): Promise<{ created: boolean; item: MobileInboxItemDto }> {
      try {
        return (await upsertManyUnsafe([input]))[0]!;
      } catch (error) {
        if (error instanceof MobileInboxError) throw error;
        throw sanitizeFeishuError();
      }
    },
    async upsertMany(inputs): Promise<Array<{ created: boolean; item: MobileInboxItemDto }>> {
      try {
        return await upsertManyUnsafe(inputs);
      } catch (error) {
        if (error instanceof MobileInboxError) throw error;
        throw sanitizeFeishuError();
      }
    },
  };
}
