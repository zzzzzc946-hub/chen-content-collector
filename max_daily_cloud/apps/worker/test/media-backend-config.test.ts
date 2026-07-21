import { describe, expect, it, vi } from 'vitest';
import type { WorkerBindings } from '../src/env';
import { createConfiguredServices } from '../src/index';
import { SupabaseMediaStore } from '../src/supabase-media';

function bindings(): WorkerBindings {
  return {
    APP_ORIGIN: 'https://daily.example.com',
    INVITATION_CLAIM_RATE_LIMITER: { limit: async () => ({ success: true }) },
    MEDIA_SESSION_SECRET: 'media-session-secret-at-least-32-bytes',
    OWNER_EMAIL: 'owner@example.com',
    PUBLIC_SHARE_RATE_LIMITER: { limit: async () => ({ success: true }) },
    PUBLISHER_TOKEN_PEPPER: 'publisher-token-pepper-at-least-32-bytes',
    SHARE_COOKIE_SECRET: 'share-cookie-secret-at-least-32-bytes',
    SUPABASE_ANON_KEY: 'anon-key',
    SUPABASE_SERVICE_ROLE_KEY: 'service-role-key',
    SUPABASE_STORAGE_BUCKET: 'max-daily-media',
    SUPABASE_URL: 'https://project-ref.supabase.co',
  };
}

describe('production media backend configuration', () => {
  it('uses Supabase Storage even when a legacy NAS value is present', () => {
    const services = createConfiguredServices({
      ...bindings(),
      NAS_MEDIA_BASE_URL: 'https://legacy-nas.example.com',
    } as WorkerBindings);

    expect(services.mediaObjectStore).toBeInstanceOf(SupabaseMediaStore);
  });

  it('rejects a missing Supabase Storage bucket during startup', () => {
    expect(() => createConfiguredServices({
      ...bindings(),
      SUPABASE_STORAGE_BUCKET: '',
    })).toThrow('SUPABASE_STORAGE_BUCKET is required');
  });

  it.each(['MEDIA_SESSION_SECRET', 'SHARE_COOKIE_SECRET'] as const)(
    'rejects an unsafe %s during startup',
    (name) => {
      expect(() => createConfiguredServices({
        ...bindings(),
        [name]: 'too-short',
      })).toThrow(`${name} must be at least 32 characters`);
    },
  );

  it('reads only sanitized fields for the current collaboration-link status', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify([{
      created_at: '2026-07-15T04:00:00.000Z',
      id: '00000000-0000-0000-0000-000000000501',
      last_used_at: null,
    }]), {
      headers: { 'content-type': 'application/json' },
      status: 200,
    }));
    vi.stubGlobal('fetch', fetcher);

    try {
      const reader = createConfiguredServices(bindings())
        .readCurrentCollaborationLink;
      expect(await reader?.()).toEqual({
        createdAt: new Date('2026-07-15T04:00:00.000Z'),
        id: '00000000-0000-0000-0000-000000000501',
        lastUsedAt: null,
      });
      const url = new URL(String(fetcher.mock.calls[0]?.[0]));
      expect(url.searchParams.get('select')).toBe('id,created_at,last_used_at');
      expect(url.search).not.toContain('token');
      expect(url.searchParams.get('revoked_at')).toBe('is.null');
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
