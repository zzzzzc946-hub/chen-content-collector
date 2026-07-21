import { describe, expect, it, vi } from 'vitest';
import type { Identity } from '../src/auth';
import {
  CollaborationLinkError,
  createCollaborationLink,
  exchangeCollaborationToken,
  resolveCollaborationSession,
  revokeCollaborationLink,
  type CollaborationLinkStorage,
  type PortalCollaborationLink,
} from '../src/collaboration-links';
import { hashToken } from '../src/invitations';

const OWNER_ID = '00000000-0000-0000-0000-000000000001';
const LINK_ID = '00000000-0000-0000-0000-000000000701';
const NOW = new Date('2026-07-15T04:00:00.000Z');
const SESSION_LIFETIME_SECONDS = 180 * 24 * 60 * 60;
const SESSION_SECRET = 'test-only-collaboration-secret-with-32-bytes';
const APP_ORIGIN = 'https://daily.example.com';
const BASE64_URL_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

const owner: Identity = {
  email: 'chen@example.com',
  id: OWNER_ID,
  role: 'owner',
};

class MemoryCollaborationStorage implements CollaborationLinkStorage {
  links = new Map<string, PortalCollaborationLink>();
  findByIdCalls: string[] = [];
  findByTokenHashCalls: string[] = [];
  operations: string[] = [];
  touchCalls: Array<{
    expectedSessionVersion: number;
    id: string;
    usedAt: Date;
  }> = [];
  revokeBeforeTouch = false;
  throwOnFind = false;
  throwOnReplace = false;
  throwOnRevoke = false;
  throwOnTouch = false;

  async findPortalLinkById(id: string): Promise<PortalCollaborationLink | null> {
    this.findByIdCalls.push(id);
    if (this.throwOnFind) throw new Error('database host and token leaked');
    return this.links.get(id) ?? null;
  }

  async findPortalLinkByTokenHash(
    tokenHash: string,
  ): Promise<PortalCollaborationLink | null> {
    this.findByTokenHashCalls.push(tokenHash);
    if (this.throwOnFind) throw new Error('database host and token leaked');
    return [...this.links.values()].find(
      (record) => record.tokenHash === tokenHash,
    ) ?? null;
  }

  async replaceActivePortalLink(record: PortalCollaborationLink): Promise<void> {
    this.operations.push('replace');
    if (this.throwOnReplace) throw new Error('rpc response leaked');

    for (const active of this.links.values()) {
      if (!active.revokedAt) {
        active.revokedAt = record.createdAt;
        active.sessionVersion += 1;
      }
    }
    this.links.set(record.id, structuredClone(record));
  }

  async revokePortalLink(id: string, revokedAt: Date): Promise<boolean> {
    this.operations.push('revoke');
    if (this.throwOnRevoke) throw new Error('rpc response leaked');

    const record = this.links.get(id);
    if (!record || record.revokedAt) return false;
    record.revokedAt = revokedAt;
    record.sessionVersion += 1;
    return true;
  }

  async touchPortalLink(
    id: string,
    expectedSessionVersion: number,
    usedAt: Date,
  ): Promise<boolean> {
    this.touchCalls.push({ expectedSessionVersion, id, usedAt });
    if (this.throwOnTouch) throw new Error('touch details leaked');

    const record = this.links.get(id);
    if (record && this.revokeBeforeTouch && !record.revokedAt) {
      record.revokedAt = usedAt;
      record.sessionVersion += 1;
    }
    if (
      !record
      || record.revokedAt
      || record.sessionVersion !== expectedSessionVersion
    ) {
      return false;
    }
    record.lastUsedAt = usedAt;
    return true;
  }
}

function identity(role: Identity['role']): Identity {
  return {
    email: 'person@example.com',
    id: '00000000-0000-0000-0000-000000000002',
    role,
  };
}

function base64UrlEncode(value: Uint8Array): string {
  let binary = '';
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '');
}

function base64UrlDecode(value: string): Uint8Array {
  const normalized = value.replaceAll('-', '+').replaceAll('_', '/');
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function makeNonCanonicalBase64Url(value: string): string {
  const remainder = value.length % 4;
  if (remainder !== 2 && remainder !== 3) {
    throw new Error('test value has no unused Base64URL bits');
  }
  const index = BASE64_URL_ALPHABET.indexOf(value.at(-1) ?? '');
  if (index < 0) throw new Error('test value is not Base64URL');
  return `${value.slice(0, -1)}${BASE64_URL_ALPHABET[index + 1]}`;
}

async function signedSession(
  payloadValue: unknown,
  secret = SESSION_SECRET,
): Promise<string> {
  const payload = base64UrlEncode(
    new TextEncoder().encode(JSON.stringify(payloadValue)),
  );
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { hash: 'SHA-256', name: 'HMAC' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign(
    'HMAC',
    key,
    new TextEncoder().encode(
      `max-daily-collaboration-session:v1:${payload}`,
    ),
  );
  return `${payload}.${base64UrlEncode(new Uint8Array(signature))}`;
}

function sessionCookie(response: Response): string {
  return response.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
}

function sessionValue(response: Response): string {
  return sessionCookie(response).split('=', 2)[1] ?? '';
}

function readSessionPayload(response: Response): Record<string, unknown> {
  const payload = sessionValue(response).split('.', 1)[0] ?? '';
  return JSON.parse(
    new TextDecoder().decode(base64UrlDecode(payload)),
  ) as Record<string, unknown>;
}

async function seedActiveLink(
  storage: MemoryCollaborationStorage,
  token = 'A'.repeat(43),
): Promise<{ record: PortalCollaborationLink; token: string }> {
  const record: PortalCollaborationLink = {
    createdAt: NOW,
    createdBy: OWNER_ID,
    id: LINK_ID,
    lastUsedAt: null,
    revokedAt: null,
    sessionVersion: 1,
    tokenHash: await hashToken(token),
  };
  storage.links.set(record.id, structuredClone(record));
  return { record, token };
}

async function exchangeSeededLink(
  storage: MemoryCollaborationStorage,
): Promise<{ response: Response; token: string }> {
  const { token } = await seedActiveLink(storage);
  const response = await exchangeCollaborationToken(
    storage,
    token,
    SESSION_SECRET,
    APP_ORIGIN,
    NOW,
  );
  return { response, token };
}

function invalidNow(kind: 'invalid-date' | 'negative-infinity' | 'positive-infinity'): Date {
  if (kind === 'invalid-date') return new Date(Number.NaN);
  const value = new Date(NOW);
  value.getTime = () => kind === 'positive-infinity'
    ? Number.POSITIVE_INFINITY
    : Number.NEGATIVE_INFINITY;
  return value;
}

describe('collaboration operation time validation', () => {
  it.each([
    'invalid-date',
    'negative-infinity',
    'positive-infinity',
  ] as const)('rejects %s at the beginning of every exported operation', async (kind) => {
    const operations = [
      (storage: MemoryCollaborationStorage) => createCollaborationLink(
        storage,
        identity(null),
        invalidNow(kind),
      ),
      (storage: MemoryCollaborationStorage) => revokeCollaborationLink(
        storage,
        identity(null),
        LINK_ID,
        invalidNow(kind),
      ),
      (storage: MemoryCollaborationStorage) => exchangeCollaborationToken(
        storage,
        'bad-token',
        'short-secret',
        'http://unsafe.example.com/path',
        invalidNow(kind),
      ),
      (storage: MemoryCollaborationStorage) => resolveCollaborationSession(
        storage,
        '',
        'short-secret',
        invalidNow(kind),
      ),
    ];

    for (const operation of operations) {
      const storage = new MemoryCollaborationStorage();
      await expect(operation(storage)).rejects.toMatchObject({
        code: 'collaboration_time_invalid',
        status: 500,
      });
      expect(storage.operations).toEqual([]);
      expect(storage.findByIdCalls).toEqual([]);
      expect(storage.findByTokenHashCalls).toEqual([]);
      expect(storage.touchCalls).toEqual([]);
    }
  });
});

describe('fixed collaboration link creation and revocation', () => {
  it('encodes deterministic 32-byte random input to the exact Base64URL token', async () => {
    const storage = new MemoryCollaborationStorage();
    const randomSpy = vi.spyOn(globalThis.crypto, 'getRandomValues')
      .mockImplementation((array) => {
        if (!(array instanceof Uint8Array) || array.byteLength !== 32) {
          throw new Error('expected one 32-byte random buffer');
        }
        array.set(Uint8Array.from({ length: 32 }, (_, index) => index));
        return array;
      });

    try {
      const created = await createCollaborationLink(storage, owner, NOW);

      expect(created.token).toBe(
        'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
      );
      expect(randomSpy).toHaveBeenCalledTimes(1);
    } finally {
      randomSpy.mockRestore();
    }
  });

  it('uses 32 bytes from Web Crypto and returns a 43-character URL-safe token', async () => {
    const storage = new MemoryCollaborationStorage();
    const randomSpy = vi.spyOn(globalThis.crypto, 'getRandomValues');

    try {
      const first = await createCollaborationLink(storage, owner, NOW);
      const second = await createCollaborationLink(
        storage,
        owner,
        new Date(NOW.getTime() + 1),
      );

      expect(first.token).toMatch(/^[A-Za-z0-9_-]{43}$/);
      expect(second.token).toMatch(/^[A-Za-z0-9_-]{43}$/);
      expect(second.token).not.toBe(first.token);
      expect(randomSpy.mock.calls.some(
        ([bytes]) => bytes instanceof Uint8Array && bytes.byteLength === 32,
      )).toBe(true);
    } finally {
      randomSpy.mockRestore();
    }
  });

  it('stores only the token hash and returns the raw token once', async () => {
    const storage = new MemoryCollaborationStorage();

    const created = await createCollaborationLink(storage, owner, NOW);
    const stored = storage.links.get(created.id);

    expect(created.createdAt).toBe(NOW.toISOString());
    expect(JSON.stringify(created).split(created.token)).toHaveLength(2);
    expect(stored).not.toHaveProperty('token');
    expect(stored?.tokenHash).toBe(await hashToken(created.token));
    expect(JSON.stringify(stored)).not.toContain(created.token);
    expect(storage.operations).toEqual(['replace']);
  });

  it.each([null, 'editor', 'viewer'] as const)(
    'rejects %s creation and revocation with a stable owner error',
    async (role) => {
      const storage = new MemoryCollaborationStorage();
      await seedActiveLink(storage);
      const actor = identity(role);

      await expect(
        createCollaborationLink(storage, actor, NOW),
      ).rejects.toMatchObject({
        code: 'owner_required',
        status: 403,
      });
      await expect(
        revokeCollaborationLink(storage, actor, LINK_ID, NOW),
      ).rejects.toMatchObject({
        code: 'owner_required',
        status: 403,
      });
      expect(storage.operations).toEqual([]);
    },
  );

  it('delegates replacement to one atomic storage operation', async () => {
    const storage = new MemoryCollaborationStorage();
    const existing = await seedActiveLink(storage);

    const created = await createCollaborationLink(
      storage,
      owner,
      new Date(NOW.getTime() + 1000),
    );

    expect(storage.operations).toEqual(['replace']);
    expect(storage.links.get(existing.record.id)).toMatchObject({
      revokedAt: new Date(NOW.getTime() + 1000),
      sessionVersion: 2,
    });
    expect(storage.links.get(created.id)).toMatchObject({
      revokedAt: null,
      sessionVersion: 1,
    });
  });

  it('revokes only as owner and maps unavailable links to a stable not-found error', async () => {
    const storage = new MemoryCollaborationStorage();
    await seedActiveLink(storage);

    await revokeCollaborationLink(storage, owner, LINK_ID, NOW);

    expect(storage.links.get(LINK_ID)).toMatchObject({
      revokedAt: NOW,
      sessionVersion: 2,
    });
    await expect(
      revokeCollaborationLink(storage, owner, LINK_ID, NOW),
    ).rejects.toMatchObject({
      code: 'collaboration_link_not_found',
      status: 404,
    });
  });

  it('maps storage failures without exposing adapter details', async () => {
    const storage = new MemoryCollaborationStorage();
    storage.throwOnReplace = true;

    const creation = createCollaborationLink(storage, owner, NOW);
    await expect(creation).rejects.toMatchObject({
      code: 'collaboration_storage_unavailable',
      status: 503,
    });
    await expect(creation).rejects.not.toThrow(/rpc response leaked/);
  });
});

describe('fixed collaboration token exchange', () => {
  it('sets a 180-day signed secure cookie and redirects safely to /daily', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response, token } = await exchangeSeededLink(storage);
    const setCookie = response.headers.get('set-cookie') ?? '';

    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe(`${APP_ORIGIN}/daily`);
    expect(response.headers.get('location')).not.toContain(token);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    expect(setCookie).toMatch(/^max_daily_collaboration=[^.]+\.[^;]+;/);
    expect(setCookie).toContain(`Max-Age=${SESSION_LIFETIME_SECONDS}`);
    expect(setCookie).toContain('Path=/');
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('Secure');
    expect(setCookie).toContain('SameSite=Lax');
    expect(setCookie).not.toContain(token);
    expect(storage.touchCalls).toEqual([{
      expectedSessionVersion: 1,
      id: LINK_ID,
      usedAt: NOW,
    }]);
  });

  it('rejects when the link is revoked after token lookup but before atomic touch', async () => {
    const storage = new MemoryCollaborationStorage();
    const { token } = await seedActiveLink(storage);
    storage.revokeBeforeTouch = true;

    await expect(
      exchangeCollaborationToken(
        storage,
        token,
        SESSION_SECRET,
        APP_ORIGIN,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    expect(storage.findByTokenHashCalls).toHaveLength(1);
    expect(storage.touchCalls).toEqual([{
      expectedSessionVersion: 1,
      id: LINK_ID,
      usedAt: NOW,
    }]);
  });

  it('maps atomic exchange touch failures to a stable sanitized storage error', async () => {
    const storage = new MemoryCollaborationStorage();
    const { token } = await seedActiveLink(storage);
    storage.throwOnTouch = true;

    const exchange = exchangeCollaborationToken(
      storage,
      token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    await expect(exchange).rejects.toMatchObject({
      code: 'collaboration_storage_unavailable',
      status: 503,
    });
    await expect(exchange).rejects.not.toThrow(/touch details leaked/);
    expect(storage.touchCalls).toEqual([{
      expectedSessionVersion: 1,
      id: LINK_ID,
      usedAt: NOW,
    }]);
  });

  it('emits the exact versioned collaboration payload schema', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);

    expect(readSessionPayload(response)).toEqual({
      exp: Math.floor(NOW.getTime() / 1000) + SESSION_LIFETIME_SECONDS,
      linkId: LINK_ID,
      sessionVersion: 1,
      v: 1,
    });
  });

  it.each(['', 'short-secret', 'x'.repeat(31)])(
    'fails closed when the collaboration session secret is shorter than 32 characters',
    async (secret) => {
      const storage = new MemoryCollaborationStorage();
      const { token } = await seedActiveLink(storage);

      const exchange = exchangeCollaborationToken(
        storage,
        token,
        secret,
        APP_ORIGIN,
        NOW,
      );
      await expect(exchange).rejects.toMatchObject({
        code: 'collaboration_session_secret_invalid',
        status: 500,
      });
      await expect(exchange).rejects.not.toThrow(token);
      expect(storage.touchCalls).toEqual([]);
    },
  );

  it.each(['x', 'A'.repeat(42), `${'A'.repeat(42)}=`, 'A'.repeat(44)])(
    'rejects malformed collaboration token %s before storage lookup',
    async (token) => {
      const storage = new MemoryCollaborationStorage();

      await expect(
        exchangeCollaborationToken(
          storage,
          token,
          SESSION_SECRET,
          APP_ORIGIN,
          NOW,
        ),
      ).rejects.toMatchObject({
        code: 'collaboration_token_invalid',
        status: 400,
      });
      expect(storage.findByTokenHashCalls).toEqual([]);
      expect(storage.touchCalls).toEqual([]);
    },
  );

  it('does not echo an unknown or revoked raw token in errors', async () => {
    const storage = new MemoryCollaborationStorage();
    const unknownToken = 'Z'.repeat(43);

    const unknown = exchangeCollaborationToken(
      storage,
      unknownToken,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    await expect(unknown).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    await expect(unknown).rejects.not.toThrow(unknownToken);

    const { record, token } = await seedActiveLink(storage);
    record.revokedAt = NOW;
    storage.links.set(record.id, record);
    const revoked = exchangeCollaborationToken(
      storage,
      token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    await expect(revoked).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    await expect(revoked).rejects.not.toThrow(token);
    expect(storage.touchCalls).toEqual([]);
  });

  it.each([
    'http://daily.example.com',
    'https://user:password@daily.example.com',
    'https://daily.example.com/nested',
    'https://daily.example.com/?next=https://attacker.example',
    'javascript:alert(1)',
  ])('rejects unsafe configured app origin %s', async (appOrigin) => {
    const storage = new MemoryCollaborationStorage();
    const { token } = await seedActiveLink(storage);

    await expect(
      exchangeCollaborationToken(
        storage,
        token,
        SESSION_SECRET,
        appOrigin,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'app_origin_invalid',
      status: 500,
    });
    expect(storage.touchCalls).toEqual([]);
  });

  it('joins /daily onto a safe origin with a trailing slash', async () => {
    const storage = new MemoryCollaborationStorage();
    const { token } = await seedActiveLink(storage);

    const response = await exchangeCollaborationToken(
      storage,
      token,
      SESSION_SECRET,
      `${APP_ORIGIN}/`,
      NOW,
    );

    expect(response.headers.get('location')).toBe(`${APP_ORIGIN}/daily`);
  });
});

describe('fixed collaboration session resolution', () => {
  it('returns a distinct collaborator session and revalidates storage on every request', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const cookie = sessionCookie(response);
    storage.findByIdCalls = [];
    storage.touchCalls = [];

    const first = await resolveCollaborationSession(
      storage,
      cookie,
      SESSION_SECRET,
      NOW,
    );
    const second = await resolveCollaborationSession(
      storage,
      cookie,
      SESSION_SECRET,
      NOW,
    );

    expect(first).toEqual({
      linkId: LINK_ID,
      role: 'collaborator',
      sessionVersion: 1,
    });
    expect(second).toEqual(first);
    expect(first).not.toHaveProperty('email');
    expect(first).not.toHaveProperty('id');
    expect(storage.findByIdCalls).toEqual([LINK_ID, LINK_ID]);
    expect(storage.touchCalls).toHaveLength(2);
  });

  it('rejects a link revoked after exchange and does not touch it', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const record = storage.links.get(LINK_ID);
    if (!record) throw new Error('test setup failed');
    record.revokedAt = new Date(NOW.getTime() + 1);
    record.sessionVersion += 1;
    storage.findByIdCalls = [];
    storage.touchCalls = [];

    await expect(
      resolveCollaborationSession(
        storage,
        sessionCookie(response),
        SESSION_SECRET,
        new Date(NOW.getTime() + 2),
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    expect(storage.findByIdCalls).toEqual([LINK_ID]);
    expect(storage.touchCalls).toEqual([]);
  });

  it('rejects an old session version on the next request without touching', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const record = storage.links.get(LINK_ID);
    if (!record) throw new Error('test setup failed');
    record.sessionVersion += 1;
    storage.touchCalls = [];

    await expect(
      resolveCollaborationSession(
        storage,
        sessionCookie(response),
        SESSION_SECRET,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    expect(storage.touchCalls).toEqual([]);
  });

  it('rejects when the link is revoked after session lookup but before atomic touch', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    storage.findByIdCalls = [];
    storage.touchCalls = [];
    storage.revokeBeforeTouch = true;

    await expect(
      resolveCollaborationSession(
        storage,
        sessionCookie(response),
        SESSION_SECRET,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_link_unavailable',
      status: 410,
    });
    expect(storage.findByIdCalls).toEqual([LINK_ID]);
    expect(storage.touchCalls).toEqual([{
      expectedSessionVersion: 1,
      id: LINK_ID,
      usedAt: NOW,
    }]);
  });

  it('rejects malformed and tampered signatures without touching storage', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const session = sessionValue(response);
    const tampered = `${session.slice(0, -1)}${session.endsWith('A') ? 'B' : 'A'}`;
    storage.findByIdCalls = [];
    storage.touchCalls = [];

    for (const cookie of [
      'max_daily_collaboration=malformed',
      `max_daily_collaboration=${tampered}`,
      'max_daily_collaboration=a.b.extra',
    ]) {
      await expect(
        resolveCollaborationSession(storage, cookie, SESSION_SECRET, NOW),
      ).rejects.toMatchObject({
        code: 'collaboration_session_invalid',
        status: 401,
      });
    }
    expect(storage.findByIdCalls).toEqual([]);
    expect(storage.touchCalls).toEqual([]);
  });

  it('rejects duplicate collaboration cookies before crypto or storage access', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const cookie = sessionCookie(response);
    storage.findByIdCalls = [];
    storage.touchCalls = [];
    const importKeySpy = vi.spyOn(crypto.subtle, 'importKey');

    try {
      await expect(
        resolveCollaborationSession(
          storage,
          `${cookie}; unrelated=value; ${cookie}`,
          SESSION_SECRET,
          NOW,
        ),
      ).rejects.toMatchObject({
        code: 'collaboration_session_invalid',
        status: 401,
      });
      expect(importKeySpy).not.toHaveBeenCalled();
      expect(storage.findByIdCalls).toEqual([]);
      expect(storage.touchCalls).toEqual([]);
    } finally {
      importKeySpy.mockRestore();
    }
  });

  it('accepts normal unrelated cookies while bounding the total header', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const cookie = sessionCookie(response);
    storage.findByIdCalls = [];
    storage.touchCalls = [];

    await expect(
      resolveCollaborationSession(
        storage,
        `preferences=${'x'.repeat(7000)}; ${cookie}; analytics=enabled`,
        SESSION_SECRET,
        NOW,
      ),
    ).resolves.toMatchObject({ role: 'collaborator' });
    await expect(
      resolveCollaborationSession(
        storage,
        `${cookie}; oversized=${'x'.repeat(8192)}`,
        SESSION_SECRET,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_session_invalid',
      status: 401,
    });
  });

  it('rejects oversized header session and payload before crypto or decoding', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const cookie = sessionCookie(response);
    const signature = sessionValue(response).split('.')[1] ?? '';
    storage.findByIdCalls = [];
    storage.touchCalls = [];
    const importKeySpy = vi.spyOn(crypto.subtle, 'importKey');
    const atobSpy = vi.spyOn(globalThis, 'atob');
    const oversizedCookies = [
      `${cookie}; unrelated=${'x'.repeat(8192)}`,
      `max_daily_collaboration=${'A'.repeat(1025)}`,
      `max_daily_collaboration=${'A'.repeat(513)}.${signature}`,
    ];

    try {
      for (const oversized of oversizedCookies) {
        await expect(
          resolveCollaborationSession(
            storage,
            oversized,
            SESSION_SECRET,
            NOW,
          ),
        ).rejects.toMatchObject({
          code: 'collaboration_session_invalid',
          status: 401,
        });
      }
      expect(importKeySpy).not.toHaveBeenCalled();
      expect(atobSpy).not.toHaveBeenCalled();
      expect(storage.findByIdCalls).toEqual([]);
      expect(storage.touchCalls).toEqual([]);
    } finally {
      importKeySpy.mockRestore();
      atobSpy.mockRestore();
    }
  });

  it('requires bounded canonical Base64URL segments and a 43-character signature', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const [payload = '', signature = ''] = sessionValue(response).split('.');
    const nonCanonicalPayload = makeNonCanonicalBase64Url(payload);
    const nonCanonicalSignature = makeNonCanonicalBase64Url(signature);
    expect(base64UrlDecode(nonCanonicalPayload)).toEqual(
      base64UrlDecode(payload),
    );
    expect(base64UrlDecode(nonCanonicalSignature)).toEqual(
      base64UrlDecode(signature),
    );
    storage.findByIdCalls = [];
    storage.touchCalls = [];
    const importKeySpy = vi.spyOn(crypto.subtle, 'importKey');
    const malformedSessions = [
      `${payload}=.${signature}`,
      `${nonCanonicalPayload}.${signature}`,
      `${payload}.${signature}=`,
      `${payload}.${signature.slice(0, -1)}`,
      `${payload}.${signature}A`,
      `${payload}.${nonCanonicalSignature}`,
    ];

    try {
      for (const malformed of malformedSessions) {
        await expect(
          resolveCollaborationSession(
            storage,
            `max_daily_collaboration=${malformed}`,
            SESSION_SECRET,
            NOW,
          ),
        ).rejects.toMatchObject({
          code: 'collaboration_session_invalid',
          status: 401,
        });
      }
      expect(importKeySpy).not.toHaveBeenCalled();
      expect(storage.findByIdCalls).toEqual([]);
      expect(storage.touchCalls).toEqual([]);
    } finally {
      importKeySpy.mockRestore();
    }
  });

  it.each([
    {
      exp: Math.floor(NOW.getTime() / 1000) + 10,
      linkId: LINK_ID,
      sessionVersion: 1,
      v: 2,
    },
    {
      exp: Math.floor(NOW.getTime() / 1000) + 10,
      extra: true,
      linkId: LINK_ID,
      sessionVersion: 1,
      v: 1,
    },
    {
      exp: Math.floor(NOW.getTime() / 1000) + 10.5,
      linkId: LINK_ID,
      sessionVersion: 1,
      v: 1,
    },
    {
      exp: Math.floor(NOW.getTime() / 1000) + 10,
      linkId: '',
      sessionVersion: 1,
      v: 1,
    },
    {
      exp: Math.floor(NOW.getTime() / 1000) + 10,
      linkId: LINK_ID,
      sessionVersion: 0,
      v: 1,
    },
  ])('rejects an invalid signed payload schema %#', async (payload) => {
    const storage = new MemoryCollaborationStorage();
    await seedActiveLink(storage);
    const session = await signedSession(payload);

    await expect(
      resolveCollaborationSession(
        storage,
        `max_daily_collaboration=${session}`,
        SESSION_SECRET,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_session_invalid',
      status: 401,
    });
    expect(storage.findByIdCalls).toEqual([]);
    expect(storage.touchCalls).toEqual([]);
  });

  it('accepts one second before exp and rejects exactly at exp', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    const cookie = sessionCookie(response);
    const expiresAt = new Date(NOW.getTime() + SESSION_LIFETIME_SECONDS * 1000);
    storage.touchCalls = [];

    await expect(
      resolveCollaborationSession(
        storage,
        cookie,
        SESSION_SECRET,
        new Date(expiresAt.getTime() - 1000),
      ),
    ).resolves.toMatchObject({ role: 'collaborator' });
    const touchesBeforeExpiry = storage.touchCalls.length;
    await expect(
      resolveCollaborationSession(
        storage,
        cookie,
        SESSION_SECRET,
        expiresAt,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_session_invalid',
      status: 401,
    });
    expect(storage.touchCalls).toHaveLength(touchesBeforeExpiry);
  });

  it('fails closed with a short secret when resolving a cookie', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    storage.findByIdCalls = [];

    await expect(
      resolveCollaborationSession(
        storage,
        sessionCookie(response),
        'x'.repeat(31),
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_session_secret_invalid',
      status: 500,
    });
    expect(storage.findByIdCalls).toEqual([]);
  });

  it('requires the collaboration cookie by its exact name', async () => {
    const storage = new MemoryCollaborationStorage();

    await expect(
      resolveCollaborationSession(
        storage,
        'other_cookie=value',
        SESSION_SECRET,
        NOW,
      ),
    ).rejects.toMatchObject({
      code: 'collaboration_session_required',
      status: 401,
    });
  });

  it('maps atomic touch failures to a stable sanitized storage error', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    storage.throwOnTouch = true;
    storage.touchCalls = [];

    const resolution = resolveCollaborationSession(
      storage,
      sessionCookie(response),
      SESSION_SECRET,
      NOW,
    );
    await expect(resolution).rejects.toMatchObject({
      code: 'collaboration_storage_unavailable',
      status: 503,
    });
    await expect(resolution).rejects.not.toThrow(/touch details leaked/);
    expect(storage.touchCalls).toEqual([{
      expectedSessionVersion: 1,
      id: LINK_ID,
      usedAt: NOW,
    }]);
  });

  it('maps lookup failures to a stable error without leaking storage details', async () => {
    const storage = new MemoryCollaborationStorage();
    const { response } = await exchangeSeededLink(storage);
    storage.throwOnFind = true;
    storage.touchCalls = [];

    const resolution = resolveCollaborationSession(
      storage,
      sessionCookie(response),
      SESSION_SECRET,
      NOW,
    );
    await expect(resolution).rejects.toBeInstanceOf(CollaborationLinkError);
    await expect(resolution).rejects.toMatchObject({
      code: 'collaboration_storage_unavailable',
      status: 503,
    });
    await expect(resolution).rejects.not.toThrow(/database host and token leaked/);
    expect(storage.touchCalls).toEqual([]);
  });
});
