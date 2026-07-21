import { describe, expect, it, vi } from 'vitest';
import defaultWorker, { createWorkerApp } from '../src/index';
import {
  InvitationError,
  claimInvitation,
  createInvitation,
  createShareLink,
  exchangeShareToken,
  hashToken,
  resolvePublicSession,
  type InvitationRecord,
  type InvitationStorage,
  type MemberRole,
  type ShareLinkRecord,
  type StoredShareLink,
} from '../src/invitations';
import { canPerform } from '../src/permissions';
import type {
  Identity,
  IdentityRole,
  IdentityRoleStore,
  IdentityVerifier,
  VerifiedIdentity,
} from '../src/auth';
import { createSupabaseIdentityVerifier } from '../src/auth';

const REPORT_ID = '00000000-0000-0000-0000-000000000101';
const OWNER_ID = '00000000-0000-0000-0000-000000000001';
const MAX_ID = '00000000-0000-0000-0000-000000000002';
const OTHER_ID = '00000000-0000-0000-0000-000000000003';
const INVITED_ID = '00000000-0000-0000-0000-000000000004';
const BOOTSTRAP_OWNER_ID = '00000000-0000-0000-0000-000000000005';
const UNTRUSTED_ID = '00000000-0000-0000-0000-000000000006';
const NOW = new Date('2026-07-10T08:00:00.000Z');
const SESSION_SECRET = 'test-only-public-session-secret-with-32-bytes';
const MEDIA_SESSION_SECRET = 'test-only-media-session-secret-with-32-bytes';
const APP_ORIGIN = 'https://daily.example.com';

const owner: Identity = {
  id: OWNER_ID,
  email: 'chen@example.com',
  role: 'owner',
};

class MemoryStorage implements InvitationStorage, IdentityRoleStore {
  invitations: InvitationRecord[] = [];
  shares: ShareLinkRecord[] = [];
  members = new Map<string, MemberRole>();
  ownerClaims: Array<{ email: string; userId: string }> = [];
  reportStatuses = new Map<string, ShareLinkRecord['reportStatus']>([
    [REPORT_ID, 'published'],
  ]);
  roles = new Map<string, IdentityRole>([
    [OWNER_ID, 'owner'],
    [MAX_ID, 'editor'],
    [OTHER_ID, 'viewer'],
  ]);

  private transactionTail: Promise<void> = Promise.resolve();

  async getIdentityRole(userId: string): Promise<IdentityRole | null> {
    return this.roles.get(userId) ?? null;
  }

  async claimOwner(input: {
    email: string;
    userId: string;
  }): Promise<'owner'> {
    this.ownerClaims.push(input);
    const existingOwner = [...this.roles.entries()]
      .find(([, role]) => role === 'owner');
    if (existingOwner && existingOwner[0] !== input.userId) {
      throw new InvitationError(403, 'owner_already_claimed', 'owner already claimed');
    }
    this.roles.set(input.userId, 'owner');
    return 'owner';
  }

  async insertInvitation(record: InvitationRecord): Promise<void> {
    this.invitations.push(structuredClone(record));
  }

  async claimInvitationAtomic(input: {
    claimedAt: Date;
    claimantEmail: string;
    claimantId: string;
    tokenHash: string;
  }): Promise<{ reportId: string; role: MemberRole }> {
    return this.transaction(async (tx) => {
      const invitation = await tx.findInvitationByTokenHashForUpdate(input.tokenHash);
      if (
        !invitation
        || invitation.revokedAt
        || invitation.usedAt
        || invitation.expiresAt.getTime() <= input.claimedAt.getTime()
      ) {
        throw new InvitationError(
          410,
          'invitation_unavailable',
          'invitation is expired, used, or revoked',
        );
      }
      if (input.claimantEmail !== invitation.email) {
        throw new InvitationError(
          403,
          'invitation_email_mismatch',
          'authenticated email does not match invitation',
        );
      }
      await tx.upsertReportMember({
        createdBy: invitation.createdBy,
        reportId: invitation.reportId,
        role: invitation.role,
        userId: input.claimantId,
      });
      if (!await tx.markInvitationUsed({
        claimedBy: input.claimantId,
        invitationId: invitation.id,
        usedAt: input.claimedAt,
      })) {
        throw new InvitationError(
          410,
          'invitation_unavailable',
          'invitation is unavailable',
        );
      }
      return { reportId: invitation.reportId, role: invitation.role };
    });
  }

  async findInvitationByTokenHashForUpdate(tokenHash: string): Promise<InvitationRecord | null> {
    return this.invitations.find((record) => record.tokenHash === tokenHash) ?? null;
  }

  async upsertReportMember(input: {
    reportId: string;
    userId: string;
    role: MemberRole;
    createdBy: string;
  }): Promise<void> {
    this.members.set(`${input.reportId}:${input.userId}`, input.role);
  }

  async markInvitationUsed(input: {
    invitationId: string;
    claimedBy: string;
    usedAt: Date;
  }): Promise<boolean> {
    const record = this.invitations.find((invite) => invite.id === input.invitationId);
    if (!record || record.usedAt) return false;
    record.claimedBy = input.claimedBy;
    record.usedAt = input.usedAt;
    return true;
  }

  async transaction<T>(operation: (tx: MemoryStorage) => Promise<T>): Promise<T> {
    let release = () => {};
    const previous = this.transactionTail;
    this.transactionTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;

    const invitations = structuredClone(this.invitations);
    const members = new Map(this.members);
    try {
      return await operation(this);
    } catch (error) {
      this.invitations = invitations;
      this.members = members;
      throw error;
    } finally {
      release();
    }
  }

  async insertShareLink(record: StoredShareLink): Promise<void> {
    this.shares.push(structuredClone({
      ...record,
      reportStatus: this.reportStatuses.get(record.reportId) ?? 'draft',
    }));
  }

  async findShareLinkByTokenHash(tokenHash: string): Promise<ShareLinkRecord | null> {
    return this.shares.find((record) => record.tokenHash === tokenHash) ?? null;
  }

  async findShareLinkById(id: string): Promise<ShareLinkRecord | null> {
    return this.shares.find((record) => record.id === id) ?? null;
  }

  async revokeInvitation(invitationId: string, revokedAt: Date): Promise<boolean> {
    const invitation = this.invitations.find((record) => record.id === invitationId);
    if (!invitation || invitation.revokedAt || invitation.usedAt) return false;
    invitation.revokedAt = revokedAt;
    return true;
  }

  async revokeShareLink(shareLinkId: string, revokedAt: Date): Promise<boolean> {
    const share = this.shares.find((record) => record.id === shareLinkId);
    if (!share || share.revokedAt) return false;
    share.revokedAt = revokedAt;
    return true;
  }
}

class TestIdentityVerifier implements IdentityVerifier {
  private identities = new Map<string, VerifiedIdentity>([
    ['owner-token', { id: OWNER_ID, email: 'chen@example.com' }],
    ['max-token', { id: MAX_ID, email: 'max@example.com' }],
    ['other-token', { id: OTHER_ID, email: 'other@example.com' }],
    ['invited-token', { id: INVITED_ID, email: 'new@example.com' }],
    ['bootstrap-owner-token', { id: BOOTSTRAP_OWNER_ID, email: ' Chen@Example.COM ' }],
    ['untrusted-token', { id: UNTRUSTED_ID, email: 'attacker@example.com' }],
  ]);

  async verify(request: Request): Promise<VerifiedIdentity | null> {
    const authorization = request.headers.get('authorization');
    const token = authorization?.startsWith('Bearer ') ? authorization.slice(7) : '';
    return this.identities.get(token) ?? null;
  }
}

class MemoryRateLimiter {
  readonly keys: string[] = [];
  private remaining: number;

  constructor(limit = Number.POSITIVE_INFINITY) {
    this.remaining = limit;
  }

  async limit(input: { key: string }): Promise<{ success: boolean }> {
    this.keys.push(input.key);
    if (this.remaining <= 0) return { success: false };
    this.remaining -= 1;
    return { success: true };
  }
}

function workerServices(
  storage: MemoryStorage,
  input: {
    invitationClaimRateLimiter?: MemoryRateLimiter;
    ownerEmail?: string;
    publicShareRateLimiter?: MemoryRateLimiter;
  } = {},
) {
  return {
    appOrigin: APP_ORIGIN,
    storage,
    identityVerifier: new TestIdentityVerifier(),
    invitationClaimRateLimiter:
      input.invitationClaimRateLimiter ?? new MemoryRateLimiter(),
    mediaSessionSecret: MEDIA_SESSION_SECRET,
    now: () => NOW,
    ownerEmail: input.ownerEmail ?? 'chen@example.com',
    publicShareRateLimiter:
      input.publicShareRateLimiter ?? new MemoryRateLimiter(),
    shareCookieSecret: SESSION_SECRET,
  };
}

function identity(id: string, email: string, role: MemberRole = 'editor'): Identity {
  return { id, email, role };
}

function statusOf(error: unknown): number {
  if (error instanceof InvitationError) return error.status;
  throw error;
}

async function createHarness() {
  const storage = new MemoryStorage();
  const createInvite = (email: string, role: MemberRole = 'editor') =>
    createInvitation(storage, owner, { reportId: REPORT_ID, email, role }, NOW);

  return {
    storage,
    createInvite,
    claim: async (token: string, claimant: Identity) => {
      try {
        await claimInvitation(storage, claimant, token, NOW);
        return 204;
      } catch (error) {
        return statusOf(error);
      }
    },
  };
}

describe('secure invitations', () => {
  it('rejects a forwarded editor invitation claimed by another email without membership', async () => {
    const harness = await createHarness();
    const invite = await harness.createInvite('max@example.com');

    const status = await harness.claim(
      invite.token,
      identity(OTHER_ID, 'other@example.com'),
    );

    expect(status).toBe(403);
    expect(harness.storage.members.size).toBe(0);
  });

  it('matches normalized authenticated email and consumes the invitation once', async () => {
    const harness = await createHarness();
    const invite = await harness.createInvite(' Max@Example.COM ');

    expect(
      await harness.claim(invite.token, identity(MAX_ID, '  MAX@example.com ')),
    ).toBe(204);
    expect(
      await harness.claim(invite.token, identity(MAX_ID, 'max@example.com')),
    ).toBe(410);
    expect(harness.storage.members.get(`${REPORT_ID}:${MAX_ID}`)).toBe('editor');
  });

  it('expires invitations exactly 24 hours after creation', async () => {
    const storage = new MemoryStorage();
    const invite = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'viewer' },
      NOW,
    );

    expect(invite.expiresAt).toBe('2026-07-11T08:00:00.000Z');
    await expect(
      claimInvitation(
        storage,
        identity(MAX_ID, 'max@example.com'),
        invite.token,
        new Date('2026-07-11T08:00:00.000Z'),
      ),
    ).rejects.toMatchObject({ status: 410 });
  });

  it('rejects a revoked invitation without creating membership', async () => {
    const harness = await createHarness();
    const invite = await harness.createInvite('max@example.com');
    if (harness.storage.invitations[0]) {
      harness.storage.invitations[0].revokedAt = NOW;
    }

    expect(
      await harness.claim(invite.token, identity(MAX_ID, 'max@example.com')),
    ).toBe(410);
    expect(harness.storage.members.size).toBe(0);
  });

  it('stores only invitation token hashes', async () => {
    const storage = new MemoryStorage();
    const invite = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'editor' },
      NOW,
    );

    expect(storage.invitations).toHaveLength(1);
    expect(storage.invitations[0]).not.toHaveProperty('token');
    expect(storage.invitations[0]?.tokenHash).toBe(await hashToken(invite.token));
    expect(JSON.stringify(storage.invitations[0])).not.toContain(invite.token);
  });

  it('serializes concurrent claims so only one transaction consumes the token', async () => {
    const storage = new MemoryStorage();
    const invite = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'editor' },
      NOW,
    );
    const claimant = identity(MAX_ID, 'max@example.com');

    const results = await Promise.allSettled([
      claimInvitation(storage, claimant, invite.token, NOW),
      claimInvitation(storage, claimant, invite.token, NOW),
    ]);

    expect(results.filter(({ status }) => status === 'fulfilled')).toHaveLength(1);
    expect(results.filter(({ status }) => status === 'rejected')).toHaveLength(1);
    expect(storage.members.size).toBe(1);
  });

  it('allows only the owner to create invitations', async () => {
    const storage = new MemoryStorage();

    await expect(
      createInvitation(
        storage,
        identity(MAX_ID, 'max@example.com'),
        { reportId: REPORT_ID, email: 'other@example.com', role: 'viewer' },
        NOW,
      ),
    ).rejects.toMatchObject({ status: 403 });
    expect(storage.invitations).toHaveLength(0);
  });
});

describe('verified identity and trusted owner bootstrap', () => {
  it('rejects a Supabase identity until its email is confirmed', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      id: MAX_ID,
      email: 'max@example.com',
      email_confirmed_at: null,
    }), {
      headers: { 'content-type': 'application/json' },
      status: 200,
    }));
    const verifier = createSupabaseIdentityVerifier({
      anonKey: 'test-anon-key',
      fetch: fetcher,
      supabaseUrl: 'https://project.supabase.co',
    });

    const verified = await verifier.verify(new Request('https://worker.example/api', {
      headers: { authorization: 'Bearer test-token' },
    }));

    expect(verified).toBeNull();
  });

  it('bootstraps only the identity matching OWNER_EMAIL', async () => {
    const storage = new MemoryStorage();
    storage.roles.delete(OWNER_ID);
    const app = createWorkerApp(workerServices(storage, {
      ownerEmail: '  CHEN@example.COM ',
    }));

    const created = await app.request('/api/invitations', {
      method: 'POST',
      headers: {
        authorization: 'Bearer bootstrap-owner-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        email: 'max@example.com',
        reportId: REPORT_ID,
        role: 'editor',
      }),
    });
    const denied = await app.request('/api/invitations', {
      method: 'POST',
      headers: {
        authorization: 'Bearer untrusted-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        email: 'max@example.com',
        reportId: REPORT_ID,
        role: 'editor',
      }),
    });

    expect(created.status).toBe(201);
    expect(denied.status).toBe(403);
    expect(storage.ownerClaims).toEqual([{
      email: 'chen@example.com',
      userId: BOOTSTRAP_OWNER_ID,
    }]);
  });

  it('does not expose a browser owner-bootstrap route', async () => {
    const storage = new MemoryStorage();
    storage.roles.delete(OWNER_ID);
    const app = createWorkerApp(workerServices(storage));

    const response = await app.request('/api/owner/bootstrap', {
      method: 'POST',
      headers: { authorization: 'Bearer bootstrap-owner-token' },
    });

    expect(response.status).toBe(404);
    expect(storage.ownerClaims).toHaveLength(0);
  });
});

describe('public read-only shares', () => {
  it('exchanges a public token for a 30-minute HttpOnly read-only cookie', async () => {
    const storage = new MemoryStorage();
    const share = await createShareLink(
      storage,
      owner,
      { reportId: REPORT_ID },
      NOW,
    );

    const app = createWorkerApp(workerServices(storage));
    const response = await app.request(
      `/api/shares/exchange?token=${encodeURIComponent(share.token)}`,
    );

    expect(response.status).toBe(302);
    expect(response.headers.get('set-cookie')).toContain('HttpOnly');
    expect(response.headers.get('set-cookie')).toContain('SameSite=None');
    expect(response.headers.get('set-cookie')).toContain('Max-Age=1800');
    expect(response.headers.get('location')).toBe(`${APP_ORIGIN}/r/${REPORT_ID}`);
    expect(response.headers.get('location')).not.toContain(share.token);
    expect(response.headers.get('set-cookie')).not.toContain(share.token);
  });

  it('stores only share token hashes and permits owner creation only', async () => {
    const storage = new MemoryStorage();
    const share = await createShareLink(
      storage,
      owner,
      { reportId: REPORT_ID },
      NOW,
    );

    expect(storage.shares[0]).not.toHaveProperty('token');
    expect(storage.shares[0]?.tokenHash).toBe(await hashToken(share.token));
    await expect(
      createShareLink(
        storage,
        identity(MAX_ID, 'max@example.com'),
        { reportId: REPORT_ID },
        NOW,
      ),
    ).rejects.toMatchObject({ status: 403 });
  });

  it('rejects unpublished, expired, and revoked public shares', async () => {
    const statuses = ['draft', 'published', 'published'] as const;
    const storage = new MemoryStorage();

    for (const [index, reportStatus] of statuses.entries()) {
      const reportId = `${REPORT_ID.slice(0, -1)}${index + 2}`;
      storage.reportStatuses.set(reportId, reportStatus);
      const share = await createShareLink(
        storage,
        owner,
        {
          reportId,
          expiresAt: index === 1 ? new Date(NOW.getTime() - 1) : undefined,
        },
        NOW,
      );
      if (index === 2 && storage.shares[index]) {
        storage.shares[index].revokedAt = NOW;
      }

      await expect(
        exchangeShareToken(storage, share.token, SESSION_SECRET, APP_ORIGIN, NOW),
      ).rejects.toMatchObject({ status: 410 });
    }
  });

  it('resolves a signed public session as read-only and rechecks revocation', async () => {
    const storage = new MemoryStorage();
    const share = await createShareLink(
      storage,
      owner,
      { reportId: REPORT_ID },
      NOW,
    );
    const response = await exchangeShareToken(
      storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = response.headers.get('set-cookie')?.split(';', 1)[0] ?? '';

    const session = await resolvePublicSession(storage, cookie, SESSION_SECRET, NOW);
    expect(session).toEqual({ reportId: REPORT_ID, role: 'public_reader' });
    expect(canPerform(session.role, 'read_report')).toBe(true);
    expect(canPerform(session.role, 'edit_report')).toBe(false);
    expect(canPerform(session.role, 'manage_access')).toBe(false);

    if (storage.shares[0]) storage.shares[0].revokedAt = NOW;
    await expect(
      resolvePublicSession(storage, cookie, SESSION_SECRET, NOW),
    ).rejects.toMatchObject({ status: 410 });
  });

  it('expires the public session after 30 minutes', async () => {
    const storage = new MemoryStorage();
    const share = await createShareLink(storage, owner, { reportId: REPORT_ID }, NOW);
    const response = await exchangeShareToken(
      storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = response.headers.get('set-cookie')?.split(';', 1)[0] ?? '';

    await expect(
      resolvePublicSession(
        storage,
        cookie,
        SESSION_SECRET,
        new Date(NOW.getTime() + 30 * 60 * 1000),
      ),
    ).rejects.toMatchObject({ status: 401 });
  });
});

describe('Hono access routes', () => {
  it('enforces owner-only creation and claims with the authenticated email', async () => {
    const storage = new MemoryStorage();
    const app = createWorkerApp(workerServices(storage));

    const denied = await app.request('/api/invitations', {
      method: 'POST',
      headers: {
        authorization: 'Bearer max-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        reportId: REPORT_ID,
        email: 'other@example.com',
        role: 'viewer',
      }),
    });
    expect(denied.status).toBe(403);

    const created = await app.request('/api/invitations', {
      method: 'POST',
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        reportId: REPORT_ID,
        email: 'max@example.com',
        role: 'editor',
      }),
    });
    expect(created.status).toBe(201);
    const { token } = await created.json<{ token: string }>();

    const forwarded = await app.request('/api/invitations/claim', {
      method: 'POST',
      headers: {
        authorization: 'Bearer other-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({ token }),
    });
    expect(forwarded.status).toBe(403);
    expect(storage.members.size).toBe(0);
  });

  it('creates shares from a report id without trusting client-supplied report status', async () => {
    const storage = new MemoryStorage();
    const app = createWorkerApp(workerServices(storage));

    const response = await app.request('/api/shares', {
      method: 'POST',
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({ reportId: REPORT_ID }),
    });

    expect(response.status).toBe(201);
    expect(storage.shares).toHaveLength(1);
  });

  it('lets an authenticated role-less invitee claim their first membership', async () => {
    const storage = new MemoryStorage();
    const invite = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'new@example.com', role: 'viewer' },
      NOW,
    );
    const app = createWorkerApp(workerServices(storage));

    const response = await app.request('/api/invitations/claim', {
      method: 'POST',
      headers: {
        authorization: 'Bearer invited-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({ token: invite.token }),
    });

    expect(response.status).toBe(200);
    expect(storage.members.get(`${REPORT_ID}:${INVITED_ID}`)).toBe('viewer');
  });

  it('lets only the owner revoke an unused invitation', async () => {
    const storage = new MemoryStorage();
    const invite = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'editor' },
      NOW,
    );
    const invitationId = storage.invitations[0]?.id ?? '';
    const app = createWorkerApp(workerServices(storage));

    const denied = await app.request(`/api/invitations/${invitationId}`, {
      method: 'DELETE',
      headers: { authorization: 'Bearer max-token' },
    });
    const revoked = await app.request(`/api/invitations/${invitationId}`, {
      method: 'DELETE',
      headers: { authorization: 'Bearer owner-token' },
    });

    expect(denied.status).toBe(403);
    expect(revoked.status).toBe(204);
    await expect(
      claimInvitation(storage, identity(MAX_ID, 'max@example.com'), invite.token, NOW),
    ).rejects.toMatchObject({ status: 410 });
  });

  it('lets only the owner revoke a share link and invalidates its session', async () => {
    const storage = new MemoryStorage();
    const share = await createShareLink(storage, owner, { reportId: REPORT_ID }, NOW);
    const shareLinkId = storage.shares[0]?.id ?? '';
    const exchange = await exchangeShareToken(
      storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
    const app = createWorkerApp(workerServices(storage));

    const denied = await app.request(`/api/shares/${shareLinkId}`, {
      method: 'DELETE',
      headers: { authorization: 'Bearer max-token' },
    });
    const revoked = await app.request(`/api/shares/${shareLinkId}`, {
      method: 'DELETE',
      headers: { authorization: 'Bearer owner-token' },
    });

    expect(denied.status).toBe(403);
    expect(revoked.status).toBe(204);
    await expect(
      resolvePublicSession(storage, cookie, SESSION_SECRET, NOW),
    ).rejects.toMatchObject({ status: 410 });
  });

  it('throttles invitation claims by verified identity across different tokens', async () => {
    const storage = new MemoryStorage();
    const first = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'editor' },
      NOW,
    );
    const second = await createInvitation(
      storage,
      owner,
      { reportId: REPORT_ID, email: 'max@example.com', role: 'viewer' },
      NOW,
    );
    const limiter = new MemoryRateLimiter(1);
    const app = createWorkerApp(workerServices(storage, {
      invitationClaimRateLimiter: limiter,
    }));

    const firstResponse = await app.request('/api/invitations/claim', {
      method: 'POST',
      headers: {
        authorization: 'Bearer max-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({ token: first.token, rateLimitKey: 'first' }),
    });
    const secondResponse = await app.request('/api/invitations/claim', {
      method: 'POST',
      headers: {
        authorization: 'Bearer max-token',
        'content-type': 'application/json',
      },
      body: JSON.stringify({ token: second.token, rateLimitKey: 'bypass' }),
    });

    expect(firstResponse.status).toBe(200);
    expect(secondResponse.status).toBe(429);
    expect(limiter.keys).toEqual([
      `identity:${MAX_ID}`,
      `identity:${MAX_ID}`,
    ]);
    expect(storage.invitations[1]?.usedAt).toBeNull();
  });

  it('throttles public token exchange by Cloudflare edge IP across different tokens', async () => {
    const storage = new MemoryStorage();
    const first = await createShareLink(storage, owner, { reportId: REPORT_ID }, NOW);
    const second = await createShareLink(storage, owner, { reportId: REPORT_ID }, NOW);
    const limiter = new MemoryRateLimiter(1);
    const app = createWorkerApp(workerServices(storage, {
      publicShareRateLimiter: limiter,
    }));
    const headers = { 'cf-connecting-ip': '203.0.113.10' };

    const firstResponse = await app.request(
      `/api/shares/exchange?token=${encodeURIComponent(first.token)}&rateLimitKey=first`,
      { headers },
    );
    const secondResponse = await app.request(
      `/api/shares/exchange?token=${encodeURIComponent(second.token)}&rateLimitKey=bypass`,
      { headers },
    );

    expect(firstResponse.status).toBe(302);
    expect(secondResponse.status).toBe(429);
    expect(limiter.keys).toEqual([
      'ip:203.0.113.10',
      'ip:203.0.113.10',
    ]);
  });
});

describe('production Worker wiring', () => {
  it('builds trusted Supabase storage from declared encrypted bindings', async () => {
    const serviceKey = 'test-service-role-key-never-returned';
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/v1/user')) {
        return new Response(JSON.stringify({
          id: BOOTSTRAP_OWNER_ID,
          email: 'chen@example.com',
          email_confirmed_at: '2026-07-10T07:00:00.000Z',
        }), {
          headers: { 'content-type': 'application/json' },
          status: 200,
        });
      }
      if (url.includes('/rest/v1/profiles?')) {
        return new Response('[]', {
          headers: { 'content-type': 'application/json' },
          status: 200,
        });
      }
      if (url.endsWith('/rest/v1/rpc/claim_owner')) {
        return new Response(JSON.stringify({
          id: BOOTSTRAP_OWNER_ID,
          email: 'chen@example.com',
          global_role: 'owner',
        }), {
          headers: { 'content-type': 'application/json' },
          status: 200,
        });
      }
      if (url.endsWith('/rest/v1/invitations') && init?.method === 'POST') {
        return new Response(null, { status: 201 });
      }
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetcher);

    try {
      const response = await defaultWorker.request('/api/invitations', {
        method: 'POST',
        headers: {
          authorization: 'Bearer confirmed-owner-token',
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          email: 'max@example.com',
          reportId: REPORT_ID,
          role: 'editor',
        }),
      }, {
        APP_ORIGIN,
        INVITATION_CLAIM_RATE_LIMITER: new MemoryRateLimiter(),
        MEDIA_SESSION_SECRET,
        OWNER_EMAIL: 'chen@example.com',
        PUBLIC_SHARE_RATE_LIMITER: new MemoryRateLimiter(),
        SHARE_COOKIE_SECRET: SESSION_SECRET,
        SUPABASE_ANON_KEY: 'test-anon-key',
        SUPABASE_SERVICE_ROLE_KEY: serviceKey,
        SUPABASE_STORAGE_BUCKET: 'max-daily-media',
        SUPABASE_URL: 'https://project.supabase.co',
      });

      expect(response.status).toBe(201);
      expect(await response.text()).not.toContain(serviceKey);
      const serviceRequests = fetcher.mock.calls.filter(([input]) =>
        String(input).includes('/rest/v1/'));
      expect(serviceRequests.length).toBeGreaterThan(0);
      for (const [, init] of serviceRequests) {
        const headers = new Headers(init?.headers);
        expect(headers.get('apikey')).toBe(serviceKey);
        expect(headers.get('authorization')).toBe(`Bearer ${serviceKey}`);
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
