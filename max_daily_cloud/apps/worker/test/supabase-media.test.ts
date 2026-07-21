import { afterEach, describe, expect, it, vi } from 'vitest';
import { MediaError } from '../src/media';
import { SupabaseMediaStore } from '../src/supabase-media';

const SUPABASE_URL = 'https://project-ref.supabase.co';
const SERVICE_ROLE_KEY = 'service-role-secret';
const BUCKET = 'max-daily-media';

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      'content-type': 'application/json',
      ...init.headers,
    },
  });
}

describe('SupabaseMediaStore', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('calls the default global fetch with the runtime global as this', async () => {
    let fetchThis: unknown;
    vi.stubGlobal('fetch', function (this: unknown) {
      fetchThis = this;
      return Promise.resolve(new Response(JSON.stringify({
        url: '/object/upload/sign/bucket/reports/item.mp4?token=signed-token',
      }), {
        headers: { 'content-type': 'application/json' },
        status: 200,
      }));
    });
    const store = new SupabaseMediaStore({
      bucket: 'bucket',
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await store.createSignedUpload({
      contentType: 'video/mp4',
      objectKey: 'reports/item.mp4',
    });
    expect(fetchThis).toBe(globalThis);
  });

  it('creates signed TUS upload tokens without exposing the service role key', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        url: '/object/upload/sign/max-daily-media/reports/report-1/source.mp4?token=signed-token',
      }),
    );
    const store = new SupabaseMediaStore({
      bucket: BUCKET,
      fetch: fetcher,
      serviceRoleKey: SERVICE_ROLE_KEY,
      supabaseUrl: SUPABASE_URL,
    });

    const result = await store.createSignedUpload({
      contentType: 'video/mp4',
      objectKey: 'reports/report-1/source.mp4',
    });

    expect(result).toEqual({
      chunkSize: 6 * 1024 * 1024,
      objectKey: 'reports/report-1/source.mp4',
      signedUploadToken: 'signed-token',
      signedUploadUrl:
        'https://project-ref.supabase.co/storage/v1/object/upload/sign/max-daily-media/reports/report-1/source.mp4?token=signed-token',
      tusEndpoint:
        'https://project-ref.storage.supabase.co/storage/v1/upload/resumable',
    });
    expect(fetcher).toHaveBeenCalledWith(
      'https://project-ref.supabase.co/storage/v1/object/upload/sign/max-daily-media/reports/report-1/source.mp4',
      expect.objectContaining({
        body: '{}',
        method: 'POST',
      }),
    );
    const headers = new Headers(fetcher.mock.calls[0]?.[1]?.headers);
    expect(headers.get('authorization')).toBe(`Bearer ${SERVICE_ROLE_KEY}`);
    expect(headers.get('apikey')).toBe(SERVICE_ROLE_KEY);
    expect(headers.get('content-type')).toBe('application/json');
    expect(JSON.stringify(result)).not.toContain(SERVICE_ROLE_KEY);
  });

  it('reads private object metadata and hides upstream storage errors', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        contentType: 'video/mp4',
        mimetype: 'video/mp4',
        size: 123,
      }))
      .mockResolvedValueOnce(jsonResponse({ message: 'bucket exploded' }, {
        status: 500,
      }));
    const store = new SupabaseMediaStore({
      bucket: BUCKET,
      fetch: fetcher,
      serviceRoleKey: SERVICE_ROLE_KEY,
      supabaseUrl: SUPABASE_URL,
    });

    await expect(store.headObject('reports/report-1/source.mp4')).resolves.toEqual({
      contentType: 'video/mp4',
      size: 123,
    });
    await expect(store.headObject('reports/report-1/missing.mp4')).rejects
      .toMatchObject({
        code: 'storage_error',
        status: 502,
      } satisfies Partial<MediaError>);
  });

  it('forwards private object Range requests with service-role authorization', async () => {
    const body = new Uint8Array([1, 2, 3]);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(body, {
        headers: {
          'content-range': 'bytes 10-12/123',
          'content-type': 'video/mp4',
        },
        status: 206,
      }),
    );
    const store = new SupabaseMediaStore({
      bucket: BUCKET,
      fetch: fetcher,
      serviceRoleKey: SERVICE_ROLE_KEY,
      supabaseUrl: SUPABASE_URL,
    });

    const response = await store.getObject('reports/report-1/source.mp4', {
      range: 'bytes=10-12',
    });

    expect(response.status).toBe(206);
    expect(response.size).toBe(123);
    expect(response.contentType).toBe('video/mp4');
    expect(await new Response(response.body).arrayBuffer()).toEqual(
      body.buffer,
    );
    expect(fetcher).toHaveBeenCalledWith(
      'https://project-ref.supabase.co/storage/v1/object/authenticated/max-daily-media/reports/report-1/source.mp4',
      expect.objectContaining({ method: 'GET' }),
    );
    const headers = new Headers(fetcher.mock.calls[0]?.[1]?.headers);
    expect(headers.get('authorization')).toBe(`Bearer ${SERVICE_ROLE_KEY}`);
    expect(headers.get('range')).toBe('bytes=10-12');
  });

  it('treats an already-missing object as a successful idempotent deletion', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(new Response(null, { status: 404 }));
    const store = new SupabaseMediaStore({
      bucket: BUCKET,
      fetch: fetcher,
      serviceRoleKey: SERVICE_ROLE_KEY,
      supabaseUrl: SUPABASE_URL,
    });

    await expect(store.deleteObject('reports/report-1/missing.mp4'))
      .resolves.toBeUndefined();
    await expect(store.getObject('reports/report-1/missing.mp4')).rejects
      .toMatchObject({ code: 'media_not_found', status: 404 } satisfies Partial<MediaError>);
  });
});
