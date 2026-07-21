import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { VerifiedIdentity } from '../src/auth';
import type {
  PortalCollaborationLink,
} from '../src/collaboration-links';
import type { WorkerServices } from '../src/env';
import { exchangeShareToken, hashToken } from '../src/invitations';
import type { MediaObjectStore } from '../src/media';
import { MediaError } from '../src/media';
import { createMediaSessionCookie } from '../src/media-session';
import { ReportError } from '../src/reports';
import { createWorkerApp } from '../src/index';

const APP_ORIGIN = 'https://daily.example.com';
const NOW = new Date('2026-07-15T04:00:00.000Z');
const COLLABORATION_SECRET = 'collaboration-and-share-cookie-secret-32-bytes';
const MEDIA_SECRET = 'media-session-cookie-secret-at-least-32-bytes';
const REPORT_ID = '00000000-0000-0000-0000-000000000101';
const ITEM_ID = '00000000-0000-0000-0000-000000000201';
const MEDIA_ID = '00000000-0000-0000-0000-000000000301';
const SHARE_ID = '00000000-0000-0000-0000-000000000401';
const MEDIA_BYTES = Uint8Array.from({ length: 100 }, (_, index) => index);

function cookiePair(response: Response, name: string): string {
  const value = response.headers.get('set-cookie') ?? '';
  const pair = value.split(';', 1)[0] ?? '';
  expect(pair).toMatch(new RegExp(`^${name}=`));
  return pair;
}

function decodeCookiePayload(cookie: string): Record<string, unknown> {
  const value = cookie.slice(cookie.indexOf('=') + 1);
  const payload = value.split('.', 1)[0] ?? '';
  return JSON.parse(
    new TextDecoder().decode(
      Uint8Array.from(
        atob(payload.replaceAll('-', '+').replaceAll('_', '/')),
        (character) => character.charCodeAt(0),
      ),
    ),
  ) as Record<string, unknown>;
}

class TestIdentityVerifier {
  readonly requests: string[] = [];

  async verify(request: Request): Promise<VerifiedIdentity | null> {
    const authorization = request.headers.get('authorization') ?? '';
    this.requests.push(authorization);
    if (authorization === 'Bearer owner-token') {
      return { email: 'chen@example.com', id: 'owner-id' };
    }
    if (authorization === 'Bearer editor-token') {
      return { email: 'editor@example.com', id: 'editor-id' };
    }
    return null;
  }
}

class RecordingRateLimiter {
  readonly keys: string[] = [];

  constructor(private readonly allowed = true) {}

  async limit(input: { key: string }): Promise<{ success: boolean }> {
    this.keys.push(input.key);
    return { success: this.allowed };
  }
}

class FakeObjectStore implements MediaObjectStore {
  async createSignedUpload(): Promise<never> {
    throw new Error('not used');
  }

  async deleteObject(): Promise<void> {}

  async getObject(_key: string, options: { range?: string } = {}) {
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.range ?? '');
    const body = match
      ? MEDIA_BYTES.slice(Number(match[1]), Number(match[2]) + 1)
      : MEDIA_BYTES;
    return {
      body: new Blob([body]).stream(),
      contentType: 'video/mp4',
      size: MEDIA_BYTES.byteLength,
      status: match ? 206 as const : 200 as const,
    };
  }

  async headObject() {
    return { contentType: 'video/mp4', size: MEDIA_BYTES.byteLength };
  }
}

class RouteStorage {
  readonly links = new Map<string, PortalCollaborationLink>();
  readonly shares = new Map<string, {
    createdAt: Date;
    createdBy: string;
    expiresAt: Date | null;
    id: string;
    reportId: string;
    reportStatus: 'published';
    revokedAt: Date | null;
    tokenHash: string;
  }>();
  ordinaryMediaActorIds: Array<string | null> = [];
  collaborationMediaLinkIds: string[] = [];
  ownerReportInputs: Array<{ actorId: string; readAt: Date }> = [];

  async claimOwner(): Promise<'owner'> {
    return 'owner';
  }

  async getIdentityRole(id: string) {
    if (id === 'owner-id') return 'owner' as const;
    if (id === 'editor-id') return 'editor' as const;
    return null;
  }

  async replaceActivePortalLink(record: PortalCollaborationLink): Promise<void> {
    for (const link of this.links.values()) {
      if (!link.revokedAt) {
        link.revokedAt = record.createdAt;
        link.sessionVersion += 1;
      }
    }
    this.links.set(record.id, { ...record });
  }

  async findPortalLinkById(id: string): Promise<PortalCollaborationLink | null> {
    return this.links.get(id) ?? null;
  }

  async findPortalLinkByTokenHash(hash: string): Promise<PortalCollaborationLink | null> {
    return [...this.links.values()].find((link) => link.tokenHash === hash) ?? null;
  }

  async revokePortalLink(id: string, revokedAt: Date): Promise<boolean> {
    const link = this.links.get(id);
    if (!link || link.revokedAt) return false;
    link.revokedAt = revokedAt;
    link.sessionVersion += 1;
    return true;
  }

  async touchPortalLink(
    id: string,
    expectedSessionVersion: number,
    usedAt: Date,
  ): Promise<boolean> {
    const link = this.links.get(id);
    if (!link || link.revokedAt || link.sessionVersion !== expectedSessionVersion) {
      return false;
    }
    link.lastUsedAt = usedAt;
    return true;
  }

  currentLink(): PortalCollaborationLink | null {
    return [...this.links.values()].find((link) => !link.revokedAt) ?? null;
  }

  private requireActiveLink(linkId: string): void {
    if (this.links.get(linkId)?.revokedAt !== null) {
      throw new ReportError(
        410,
        'collaboration_link_unavailable',
        'collaboration link is unavailable',
      );
    }
  }

  async listReportsForCollaborationAtomic(input: { linkId: string }) {
    this.requireActiveLink(input.linkId);
    return [{
      dailyDate: '2026-07-15',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: '2026-07-15T01:00:00.000Z',
    }];
  }

  async listReportsForOwnerAtomic(input: { actorId: string; readAt: Date }) {
    this.ownerReportInputs.push(input);
    return [{
      dailyDate: '2026-07-15',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: '2026-07-15T01:00:00.000Z',
    }];
  }

  private report(accessRole: 'collaborator' | 'editor' | 'public_reader') {
    return {
      accessRole,
      dailyDate: '2026-07-15',
      draftVersion: 1,
      id: REPORT_ID,
      items: [{
        caption: 'caption',
        id: ITEM_ID,
        localRecordId: 'record-1',
        maxDailyCard: '',
        maxFeedback: '',
        mediaId: MEDIA_ID,
        reviewStatus: '',
        sourceUrl: 'https://example.com/source',
        title: 'title',
        version: 1,
      }],
      publishedAt: '2026-07-15T01:00:00.000Z',
      publishedVersion: 1,
      status: 'published' as const,
    };
  }

  async readReportForCollaborationAtomic(input: { linkId: string }) {
    this.requireActiveLink(input.linkId);
    return this.report('collaborator');
  }

  async readReportAtomic(input: {
    actorId: string | null;
    shareLinkId: string | null;
  }) {
    if (input.actorId === 'editor-id') return this.report('editor');
    if (input.shareLinkId === SHARE_ID) return this.report('public_reader');
    throw new ReportError(404, 'report_not_found', 'report is unavailable');
  }

  async updateCollaborativeFieldForLinkAtomic(input: {
    itemId: string;
    linkId: string;
    value: string;
  }) {
    this.requireActiveLink(input.linkId);
    return { ...this.report('collaborator').items[0], maxFeedback: input.value, version: 2 };
  }

  async updateCollaborativeFieldAtomic(input: { actorId: string; value: string }) {
    if (input.actorId !== 'editor-id') {
      throw new ReportError(403, 'edit_forbidden', 'report is read-only');
    }
    return { ...this.report('editor').items[0], maxFeedback: input.value, version: 2 };
  }

  async findShareLinkById(id: string) {
    return this.shares.get(id) ?? null;
  }

  async findShareLinkByTokenHash(tokenHash: string) {
    return [...this.shares.values()].find((share) => share.tokenHash === tokenHash) ?? null;
  }

  async authorizeMediaForCollaborationAtomic(input: { linkId: string }) {
    const link = this.links.get(input.linkId);
    if (!link || link.revokedAt) {
      throw new MediaError(
        410,
        'collaboration_link_unavailable',
        'collaboration link is unavailable',
      );
    }
    this.collaborationMediaLinkIds.push(input.linkId);
    return {
      byteSize: MEDIA_BYTES.byteLength,
      contentType: 'video/mp4',
      dailyDate: '2026-07-15',
      id: MEDIA_ID,
      objectKey: 'reports/today.mp4',
      reportId: REPORT_ID,
    };
  }

  async authorizeMediaReadAtomic(input: {
    actorId: string | null;
    publicReportId: string | null;
  }) {
    this.ordinaryMediaActorIds.push(input.actorId);
    if (
      input.actorId !== 'editor-id'
      && input.actorId !== 'owner-id'
      && input.publicReportId !== REPORT_ID
    ) {
      throw new MediaError(404, 'media_not_found', 'media is unavailable');
    }
    return {
      byteSize: MEDIA_BYTES.byteLength,
      contentType: 'video/mp4',
      dailyDate: '2026-07-15',
      id: MEDIA_ID,
      objectKey: 'reports/today.mp4',
      reportId: REPORT_ID,
    };
  }

  async attachMediaUploadAtomic(): Promise<never> { throw new Error('not used'); }
  async beginMediaUploadAtomic(): Promise<never> { throw new Error('not used'); }
  async claimExpiredMediaObjects() { return []; }
  async claimMediaUploadAbortAtomic(): Promise<never> { throw new Error('not used'); }
  async claimMediaUploadCompletionAtomic(): Promise<never> { throw new Error('not used'); }
  async claimStaleMediaUploadCompletionAtomic(): Promise<never> { throw new Error('not used'); }
  async finalizeMediaUploadAbortAtomic(): Promise<never> { throw new Error('not used'); }
  async finalizeMediaUploadCompletionAtomic(): Promise<never> { throw new Error('not used'); }
  async finishMediaObjectPurge(): Promise<void> {}
  async getMediaUploadAtomic(): Promise<never> { throw new Error('not used'); }
  async listMediaUploadPartsAtomic() { return []; }
  async listMediaUploadRecoveryPage() { return []; }
  async listMediaUploadRecoveryPartsAtomic() { return []; }
  async recordMediaUploadPartAtomic(): Promise<never> { throw new Error('not used'); }
  async resetStaleMediaUploadCompletionAtomic(): Promise<never> { throw new Error('not used'); }
}

function harness(input: {
  limiter?: RecordingRateLimiter;
  mediaSessionSecret?: string;
  now?: () => Date;
} = {}) {
  const storage = new RouteStorage();
  const identityVerifier = new TestIdentityVerifier();
  const limiter = input.limiter ?? new RecordingRateLimiter();
  const services = {
    appOrigin: APP_ORIGIN,
    identityVerifier,
    invitationClaimRateLimiter: new RecordingRateLimiter(),
    mediaObjectStore: new FakeObjectStore(),
    mediaSessionSecret: input.mediaSessionSecret ?? MEDIA_SECRET,
    now: input.now ?? (() => NOW),
    ownerEmail: 'chen@example.com',
    publicShareRateLimiter: limiter,
    readCurrentCollaborationLink: async () => storage.currentLink(),
    shareCookieSecret: COLLABORATION_SECRET,
    storage,
  } as unknown as WorkerServices;
  return {
    app: createWorkerApp(services),
    identityVerifier,
    limiter,
    storage,
  };
}

async function createAndExchange(
  app: ReturnType<typeof createWorkerApp>,
): Promise<{ cookie: string; id: string; token: string }> {
  const created = await app.request('/api/collaboration-links', {
    body: '{}',
    headers: {
      authorization: 'Bearer owner-token',
      'content-type': 'application/json',
      origin: APP_ORIGIN,
    },
    method: 'POST',
  });
  expect(created.status).toBe(201);
  const body = await created.json() as { id: string; token: string };
  const exchanged = await app.request(`/c/${body.token}`, {
    headers: { 'cf-connecting-ip': '203.0.113.4' },
  });
  expect(exchanged.status).toBe(302);
  return {
    cookie: cookiePair(exchanged, 'max_daily_collaboration'),
    id: body.id,
    token: body.token,
  };
}

async function publicCookie(storage: RouteStorage): Promise<string> {
  const token = 'public-share-token';
  storage.shares.set(SHARE_ID, {
    createdAt: NOW,
    createdBy: 'owner-id',
    expiresAt: null,
    id: SHARE_ID,
    reportId: REPORT_ID,
    reportStatus: 'published',
    revokedAt: null,
    tokenHash: await hashToken(token),
  });
  return cookiePair(
    await exchangeShareToken(storage, token, COLLABORATION_SECRET, APP_ORIGIN, NOW),
    'max_daily_public',
  );
}

async function identityMediaCookie(
  app: ReturnType<typeof createWorkerApp>,
  authorization = 'Bearer editor-token',
): Promise<string> {
  const response = await app.request('/api/media/session', {
    headers: {
      authorization,
      origin: APP_ORIGIN,
    },
    method: 'POST',
  });
  expect(response.status).toBe(204);
  return cookiePair(response, 'max_daily_media');
}

describe('fixed collaboration Worker routes', () => {
  it('runs /c/* through the Worker before SPA assets while preserving /api/*', () => {
    const wrangler = readFileSync(
      fileURLToPath(new URL('../wrangler.toml', import.meta.url)),
      'utf8',
    );
    expect(wrangler).toMatch(/run_worker_first\s*=\s*\[[^\]]*"\/api\/\*"/s);
    expect(wrangler).toMatch(/run_worker_first\s*=\s*\[[^\]]*"\/c\/\*"/s);
  });

  it('deploys the same-origin Worker app only from canonical master', () => {
    const workflow = readFileSync(
      fileURLToPath(new URL('../../../../.github/workflows/max-daily-cloud.yml', import.meta.url)),
      'utf8',
    );
    expect(workflow).toMatch(/push:\s+branches:\s+- "master"/);
    expect(workflow).toContain("if: github.ref == 'refs/heads/master'");
    expect(workflow).toContain(
      'WORKER_ORIGIN: "https://max-daily-cloud-worker.1643434181.workers.dev"',
    );
    expect(workflow).toContain('VITE_API_BASE_URL: ${{ env.WORKER_ORIGIN }}');
    expect(workflow).toContain('name: Confirm deployment still targets current master');
    expect(workflow).toContain('git rev-parse origin/master');
    expect(workflow).toContain('npx supabase link --project-ref "$SUPABASE_PROJECT_REF"');
    expect(workflow).toContain('npx supabase db push --linked --password "$SUPABASE_DB_PASSWORD"');
    expect(workflow).toContain('npx wrangler secret put APP_ORIGIN');
    expect(workflow).toContain('npx wrangler deploy --config apps/worker/wrangler.toml');
    expect(workflow).not.toContain('wrangler pages deploy');
    expect(workflow).not.toContain('codex/max-daily-publisher-task7');
    expect(workflow).not.toMatch(/^\s+- "max-daily-cloud"\s*$/m);
    for (const requiredStep of [
      'Reject tracked secrets',
      'Run cloud unit tests',
      'Typecheck',
      'Test local publisher',
      'Verify Worker bundle',
      'Audit high-severity production dependencies',
      'Frozen collector smoke tests',
    ]) {
      expect(workflow).toContain(`name: ${requiredStep}`);
    }
    for (const requiredSecret of [
      'CLOUDFLARE_ACCOUNT_ID',
      'CLOUDFLARE_API_TOKEN',
      'SUPABASE_ACCESS_TOKEN',
      'SUPABASE_DB_PASSWORD',
      'SUPABASE_PROJECT_REF',
      'VITE_SUPABASE_ANON_KEY',
      'VITE_SUPABASE_URL',
    ]) {
      expect(workflow).toContain(`secrets.${requiredSecret}`);
    }
  });

  it('lets only owners create, inspect, and revoke the current link without re-exposing secrets', async () => {
    const { app } = harness();
    const denied = await app.request('/api/collaboration-links', {
      headers: { authorization: 'Bearer editor-token' },
      method: 'POST',
    });
    expect(denied.status).toBe(403);

    const created = await app.request('/api/collaboration-links', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(created.status).toBe(201);
    const createBody = await created.json() as Record<string, unknown>;
    expect(createBody.token).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(createBody).not.toHaveProperty('tokenHash');

    const current = await app.request('/api/collaboration-links/current', {
      headers: { authorization: 'Bearer owner-token' },
    });
    expect(current.status).toBe(200);
    const currentText = await current.text();
    const currentBody = JSON.parse(currentText) as Record<string, unknown>;
    expect(currentBody).toMatchObject({ active: true, id: createBody.id });
    expect(currentText).not.toContain(String(createBody.token));
    expect(currentBody).not.toHaveProperty('token');
    expect(currentBody).not.toHaveProperty('tokenHash');

    expect((await app.request(`/api/collaboration-links/${createBody.id}`, {
      headers: { authorization: 'Bearer editor-token' },
      method: 'DELETE',
    })).status).toBe(403);
    expect((await app.request(`/api/collaboration-links/${createBody.id}`, {
      headers: { authorization: 'Bearer owner-token' },
      method: 'DELETE',
    })).status).toBe(204);

    const revoked = await app.request('/api/collaboration-links/current', {
      headers: { authorization: 'Bearer owner-token' },
    });
    expect(await revoked.json()).toEqual({ active: false });
  });

  it('exchanges through the public-share limiter with a bounded IP key and sanitized output', async () => {
    const limiter = new RecordingRateLimiter();
    const { app } = harness({ limiter });
    const created = await app.request('/api/collaboration-links', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    const { token } = await created.json() as { token: string };
    const response = await app.request(`/c/${token}`, {
      headers: { 'cf-connecting-ip': 'x'.repeat(500) },
    });
    const setCookie = response.headers.get('set-cookie') ?? '';

    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe(`${APP_ORIGIN}/daily`);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('Secure');
    expect(setCookie).toContain('SameSite=Lax');
    expect(setCookie).not.toContain(token);
    expect(response.headers.get('location')).not.toContain(token);
    expect(limiter.keys).toEqual([`ip:${'x'.repeat(128)}`]);

    const invalid = await app.request('/c/not-a-valid-token');
    expect(invalid.status).toBe(400);
    expect(await invalid.json()).toMatchObject({
      code: 'collaboration_token_invalid',
      message: 'collaboration token is invalid',
    });
  });

  it('uses Bearer then collaboration then public actors and fails closed on invalid higher-priority credentials', async () => {
    const { app, storage } = harness();
    const collaboration = await createAndExchange(app);
    const publicSession = await publicCookie(storage);

    const index = await app.request('/api/reports', {
      headers: { cookie: collaboration.cookie },
    });
    expect(index.status).toBe(200);
    expect(await index.json()).toEqual([expect.objectContaining({ id: REPORT_ID })]);
    expect((await app.request('/api/reports', {
      headers: { authorization: 'Bearer owner-token' },
    })).status).toBe(200);
    expect((await app.request('/api/reports', {
      headers: { cookie: publicSession },
    })).status).toBe(403);

    const editorWins = await app.request('/api/reports', {
      headers: {
        authorization: 'Bearer editor-token',
        cookie: collaboration.cookie,
      },
    });
    expect(editorWins.status).toBe(403);

    const invalidBearer = await app.request(`/api/reports/${REPORT_ID}`, {
      headers: {
        authorization: 'Bearer invalid-token',
        cookie: collaboration.cookie,
      },
    });
    expect(invalidBearer.status).toBe(401);
  });

  it('lists reports for an Owner without an active collaboration link', async () => {
    const { app, storage } = harness();

    const response = await app.request('/api/reports', {
      headers: { authorization: 'Bearer owner-token' },
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual([
      expect.objectContaining({ id: REPORT_ID }),
    ]);
    expect(storage.currentLink()).toBeNull();
    expect(storage.ownerReportInputs).toEqual([{
      actorId: 'owner-id',
      readAt: NOW,
    }]);
  });

  it('does not fall through an invalid collaboration cookie to a valid public session', async () => {
    const { app, storage } = harness();
    const publicSession = await publicCookie(storage);
    const response = await app.request(`/api/reports/${REPORT_ID}`, {
      headers: {
        cookie: `max_daily_collaboration=invalid; ${publicSession}`,
      },
    });
    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ code: 'collaboration_session_invalid' });
  });

  it('reads reports for identity, collaborator, and public actors while keeping public edits read-only', async () => {
    const { app, storage } = harness();
    const collaboration = await createAndExchange(app);
    const publicSession = await publicCookie(storage);

    const identity = await app.request(`/api/reports/${REPORT_ID}`, {
      headers: { authorization: 'Bearer editor-token' },
    });
    const collaborator = await app.request(`/api/reports/${REPORT_ID}`, {
      headers: { cookie: collaboration.cookie },
    });
    const publicRead = await app.request(`/api/reports/${REPORT_ID}`, {
      headers: { cookie: publicSession },
    });
    expect((await identity.json() as { accessRole: string }).accessRole).toBe('editor');
    expect((await collaborator.json() as { accessRole: string }).accessRole).toBe('collaborator');
    expect((await publicRead.json() as { accessRole: string }).accessRole).toBe('public_reader');

    const patch = JSON.stringify({
      expectedVersion: 1,
      field: 'max_feedback',
      value: 'ship it',
    });
    expect((await app.request(`/api/items/${ITEM_ID}`, {
      body: patch,
      headers: { 'content-type': 'application/json', cookie: collaboration.cookie },
      method: 'PATCH',
    })).status).toBe(200);
    expect((await app.request(`/api/items/${ITEM_ID}`, {
      body: patch,
      headers: { 'content-type': 'application/json', cookie: publicSession },
      method: 'PATCH',
    })).status).toBe(403);
    expect((await app.request(`/api/items/${ITEM_ID}`, {
      body: patch,
      headers: {
        authorization: 'Bearer editor-token',
        'content-type': 'application/json',
      },
      method: 'PATCH',
    })).status).toBe(200);
  });

  it('creates typed collaborator media sessions and revalidates the active link for Range playback', async () => {
    const { app, storage } = harness();
    const collaboration = await createAndExchange(app);
    const session = await app.request('/api/media/session', {
      headers: { cookie: collaboration.cookie, origin: APP_ORIGIN },
      method: 'POST',
    });
    const mediaCookie = cookiePair(session, 'max_daily_media');
    const payload = decodeCookiePayload(mediaCookie);
    expect(session.status).toBe(204);
    expect(payload).toMatchObject({
      subject: { kind: 'collaborator', linkId: collaboration.id },
      v: 2,
    });
    expect(payload).not.toHaveProperty('actorId');
    expect(session.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
    expect(session.headers.get('access-control-allow-credentials')).toBe('true');

    const range = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        cookie: `${collaboration.cookie}; ${mediaCookie}`,
        origin: APP_ORIGIN,
        range: 'bytes=10-19',
      },
    });
    expect(range.status).toBe(206);
    expect(range.headers.get('content-range')).toBe('bytes 10-19/100');
    expect(storage.collaborationMediaLinkIds).toEqual([collaboration.id]);
    expect(storage.ordinaryMediaActorIds).toEqual([]);

    await storage.revokePortalLink(collaboration.id, NOW);
    const revoked = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${collaboration.cookie}; ${mediaCookie}` },
    });
    expect(revoked.status).toBe(410);
    expect(await revoked.json()).toMatchObject({
      code: 'collaboration_link_unavailable',
    });
  });

  it('authorizes collaborator HEAD Range playback through the collaboration context', async () => {
    const { app, storage } = harness();
    const collaboration = await createAndExchange(app);
    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        cookie: collaboration.cookie,
        range: 'bytes=20-29',
      },
      method: 'HEAD',
    });

    expect(response.status).toBe(206);
    expect(response.headers.get('content-range')).toBe('bytes 20-29/100');
    expect(response.headers.get('content-length')).toBe('10');
    expect((await response.arrayBuffer()).byteLength).toBe(0);
    expect(storage.collaborationMediaLinkIds).toEqual([collaboration.id]);
    expect(storage.ordinaryMediaActorIds).toEqual([]);
  });

  it('treats a valid identity media session as the one authenticated playback actor', async () => {
    const { app, storage } = harness();
    const publicSession = await publicCookie(storage);
    const mediaSession = await identityMediaCookie(app);

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${publicSession}; ${mediaSession}` },
    });

    expect(response.status).toBe(200);
    expect(storage.ordinaryMediaActorIds).toEqual(['editor-id']);
  });

  it('lets an Owner media session survive a stale collaboration cookie left by /c', async () => {
    const { app, storage } = harness();
    const collaboration = await createAndExchange(app);
    const mediaSession = await identityMediaCookie(app, 'Bearer owner-token');
    await storage.revokePortalLink(collaboration.id, NOW);

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${collaboration.cookie}; ${mediaSession}` },
    });

    expect(response.status).toBe(200);
    expect(storage.ordinaryMediaActorIds).toEqual(['owner-id']);
    expect(storage.collaborationMediaLinkIds).toEqual([]);
  });

  it('fails closed before readMedia when collaborator media and report sessions mismatch', async () => {
    const { app, storage } = harness();
    const first = await createAndExchange(app);
    const firstSession = await app.request('/api/media/session', {
      headers: { cookie: first.cookie, origin: APP_ORIGIN },
      method: 'POST',
    });
    const firstMediaCookie = cookiePair(firstSession, 'max_daily_media');
    const second = await createAndExchange(app);

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${second.cookie}; ${firstMediaCookie}` },
    });

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      code: 'media_authorization_conflict',
    });
    expect(storage.ordinaryMediaActorIds).toEqual([]);
    expect(storage.collaborationMediaLinkIds).toEqual([]);
  });

  it('ignores an invalid standalone media session when a valid public actor exists', async () => {
    const { app, storage } = harness();
    const publicSession = await publicCookie(storage);

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${publicSession}; max_daily_media=invalid` },
    });

    expect(response.status).toBe(200);
    expect(storage.ordinaryMediaActorIds).toEqual([null]);
  });

  it('ignores an expired standalone media session when a valid public actor exists', async () => {
    let currentTime = NOW;
    const { app, storage } = harness({ now: () => currentTime });
    const publicSession = await publicCookie(storage);
    const mediaSession = await identityMediaCookie(app);
    currentTime = new Date(NOW.getTime() + 6 * 60 * 1000);

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${publicSession}; ${mediaSession}` },
    });

    expect(response.status).toBe(200);
    expect(storage.ordinaryMediaActorIds).toEqual([null]);
  });

  it('does not let public playback hide a media-session configuration failure', async () => {
    const { app, storage } = harness({ mediaSessionSecret: 'too-short' });
    const publicSession = await publicCookie(storage);
    const mediaSession = (await createMediaSessionCookie(
      'editor-id',
      MEDIA_SECRET,
      NOW,
    )).split(';', 1)[0];

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${publicSession}; ${mediaSession}` },
    });

    expect(response.status).toBe(500);
    expect(await response.json()).toMatchObject({
      code: 'media_session_secret_invalid',
    });
    expect(storage.ordinaryMediaActorIds).toEqual([]);
  });

  it('preserves identity and public media playback and exact credentialed CORS denial', async () => {
    const { app, storage } = harness();
    const publicSession = await publicCookie(storage);
    expect((await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { authorization: 'Bearer editor-token' },
    })).status).toBe(200);
    expect((await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: publicSession },
    })).status).toBe(200);

    const preflight = await app.request('/api/media/session', {
      headers: {
        'access-control-request-headers': 'authorization',
        'access-control-request-method': 'POST',
        origin: APP_ORIGIN,
      },
      method: 'OPTIONS',
    });
    expect(preflight.status).toBe(204);
    expect(preflight.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
    expect(preflight.headers.get('access-control-allow-credentials')).toBe('true');

    const denied = await app.request('/api/media/session', {
      headers: {
        authorization: 'Bearer editor-token',
        origin: 'https://attacker.example',
      },
      method: 'POST',
    });
    expect(denied.status).toBe(403);
    expect(denied.headers.get('access-control-allow-origin')).toBeNull();
  });
});
