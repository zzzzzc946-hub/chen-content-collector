import { MediaError } from './media.js';

export const SUPABASE_TUS_CHUNK_SIZE = 6 * 1024 * 1024;

interface SupabaseErrorBody {
  code?: unknown;
  message?: unknown;
}

interface SignedUploadResponse {
  url?: unknown;
}

interface ObjectInfoResponse {
  contentType?: unknown;
  mimetype?: unknown;
  size?: unknown;
}

export interface SignedSupabaseUpload {
  chunkSize: number;
  objectKey: string;
  signedUploadUrl: string;
  signedUploadToken: string;
  tusEndpoint: string;
}

export interface SupabaseObjectHead {
  contentType: string;
  size: number;
}

export interface SupabaseObjectResponse extends SupabaseObjectHead {
  body: ReadableStream<Uint8Array>;
  status: 200 | 206;
}

function storageFailure(): MediaError {
  return new MediaError(
    502,
    'storage_error',
    'media storage request failed',
  );
}

function notFound(): MediaError {
  return new MediaError(404, 'media_not_found', 'media is unavailable');
}

function encodeObjectPath(bucket: string, objectKey: string): string {
  return [bucket, ...objectKey.split('/')]
    .map((segment) => encodeURIComponent(segment))
    .join('/');
}

function safeContentType(value: unknown): string {
  return typeof value === 'string' && value.trim()
    ? value.trim().toLowerCase()
    : 'application/octet-stream';
}

function safeSize(value: unknown): number | null {
  if (Number.isSafeInteger(value)) return value as number;
  if (typeof value === 'string' && /^\d+$/.test(value)) return Number(value);
  return null;
}

function parseTotalSize(response: Response): number | null {
  const contentRange = response.headers.get('content-range') ?? '';
  const match = /\/(\d+)$/.exec(contentRange);
  if (match?.[1]) return Number(match[1]);
  return safeSize(response.headers.get('content-length'));
}

function storageBaseUrl(supabaseUrl: string): string {
  const url = new URL(supabaseUrl);
  if (
    /\.supabase\.(co|in|red)$/.test(url.hostname)
    && !url.hostname.includes('.storage.supabase.')
  ) {
    url.hostname = url.hostname.replace('.supabase.', '.storage.supabase.');
  }
  url.pathname = '/storage/v1/upload/resumable';
  url.search = '';
  url.hash = '';
  return url.toString();
}

export class SupabaseMediaStore {
  private readonly baseUrl: string;
  private readonly bucket: string;
  private readonly fetcher: typeof fetch;
  private readonly serviceRoleKey: string;
  private readonly tusEndpoint: string;

  constructor(input: {
    bucket: string;
    fetch?: typeof fetch;
    serviceRoleKey: string;
    supabaseUrl: string;
  }) {
    this.baseUrl = input.supabaseUrl.replace(/\/+$/, '');
    this.bucket = input.bucket;
    this.fetcher = input.fetch ?? ((request, init) =>
      globalThis.fetch(request, init));
    this.serviceRoleKey = input.serviceRoleKey;
    this.tusEndpoint = storageBaseUrl(input.supabaseUrl);
  }

  async createSignedUpload(input: {
    contentType: string;
    objectKey: string;
  }): Promise<SignedSupabaseUpload> {
    const response = await this.request<SignedUploadResponse>(
      `/object/upload/sign/${encodeObjectPath(this.bucket, input.objectKey)}`,
      {
        body: '{}',
        headers: {
          'content-type': 'application/json',
          'x-upsert': 'true',
        },
        method: 'POST',
      },
    );
    if (typeof response.url !== 'string') {
      console.error('Supabase signed upload response is missing url');
      throw storageFailure();
    }
    const signedUrl = new URL(response.url, `${this.baseUrl}/storage/v1`);
    const signedUploadUrl = new URL(
      `/storage/v1${signedUrl.pathname}${signedUrl.search}`,
      this.baseUrl,
    );
    const token = signedUrl.searchParams.get('token');
    if (!token) {
      console.error('Supabase signed upload response is missing token');
      throw storageFailure();
    }
    return {
      chunkSize: SUPABASE_TUS_CHUNK_SIZE,
      objectKey: input.objectKey,
      signedUploadUrl: signedUploadUrl.toString(),
      signedUploadToken: token,
      tusEndpoint: this.tusEndpoint,
    };
  }

  async headObject(objectKey: string): Promise<SupabaseObjectHead> {
    const info = await this.request<ObjectInfoResponse>(
      `/object/info/${encodeObjectPath(this.bucket, objectKey)}`,
      { method: 'GET' },
    );
    const size = safeSize(info.size);
    if (size === null) throw storageFailure();
    return {
      contentType: safeContentType(info.contentType ?? info.mimetype),
      size,
    };
  }

  async getObject(
    objectKey: string,
    options: { range?: string } = {},
  ): Promise<SupabaseObjectResponse> {
    const response = await this.rawRequest(
      `/object/authenticated/${encodeObjectPath(this.bucket, objectKey)}`,
      {
        headers: options.range ? { range: options.range } : undefined,
        method: 'GET',
      },
    );
    if (response.status === 404) throw notFound();
    if (!response.ok || (response.status !== 200 && response.status !== 206)) {
      throw storageFailure();
    }
    if (!response.body) throw storageFailure();
    const size = parseTotalSize(response);
    if (size === null) throw storageFailure();
    return {
      body: response.body,
      contentType: safeContentType(response.headers.get('content-type')),
      size,
      status: response.status as 200 | 206,
    };
  }

  async deleteObject(objectKey: string): Promise<void> {
    try {
      await this.request<unknown>(`/object/${encodeURIComponent(this.bucket)}`, {
        body: JSON.stringify({ prefixes: [objectKey] }),
        headers: { 'content-type': 'application/json' },
        method: 'DELETE',
      });
    } catch (error) {
      if (error instanceof MediaError && error.code === 'media_not_found') return;
      throw error;
    }
  }

  private async request<T>(
    path: string,
    init: RequestInit,
  ): Promise<T> {
    const response = await this.rawRequest(path, init);
    if (response.status === 404) throw notFound();
    if (!response.ok) {
      console.error('Supabase media storage request failed', {
        path,
        status: response.status,
      });
      throw storageFailure();
    }
    try {
      return await response.json() as T;
    } catch {
      console.error('Supabase media storage response is not JSON', { path });
      throw storageFailure();
    }
  }

  private async rawRequest(
    path: string,
    init: RequestInit,
  ): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set('apikey', this.serviceRoleKey);
    headers.set('authorization', `Bearer ${this.serviceRoleKey}`);
    try {
      return await this.fetcher(
        `${this.baseUrl}/storage/v1${path}`,
        {
          ...init,
          headers,
        },
      );
    } catch (error) {
      console.error('Supabase media storage fetch failed', { error, path });
      throw storageFailure();
    }
  }
}
