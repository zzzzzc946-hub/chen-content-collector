import type { Role } from '../../../packages/shared/src/contracts.js';
import type { Identity } from './auth.js';

const INVITATION_LIFETIME_MS = 24 * 60 * 60 * 1000;
const PUBLIC_SESSION_LIFETIME_SECONDS = 30 * 60;
const PUBLIC_SESSION_COOKIE = 'max_daily_public';

export type MemberRole = Extract<Role, 'editor' | 'viewer'>;
export type ReportStatus = 'draft' | 'published' | 'withdrawn';

export interface InvitationRecord {
  claimedBy: string | null;
  createdAt: Date;
  createdBy: string;
  email: string;
  expiresAt: Date;
  id: string;
  reportId: string;
  revokedAt: Date | null;
  role: MemberRole;
  tokenHash: string;
  usedAt: Date | null;
}

export interface StoredShareLink {
  createdAt: Date;
  createdBy: string;
  expiresAt: Date | null;
  id: string;
  reportId: string;
  revokedAt: Date | null;
  tokenHash: string;
}

export interface ShareLinkRecord extends StoredShareLink {
  reportStatus: ReportStatus;
}

export interface InvitationStorage {
  claimInvitationAtomic(input: {
    claimedAt: Date;
    claimantEmail: string;
    claimantId: string;
    tokenHash: string;
  }): Promise<{ reportId: string; role: MemberRole }>;
  // Share reads must join the report's current status; callers never supply it.
  findShareLinkById(id: string): Promise<ShareLinkRecord | null>;
  findShareLinkByTokenHash(tokenHash: string): Promise<ShareLinkRecord | null>;
  insertInvitation(record: InvitationRecord): Promise<void>;
  insertShareLink(record: StoredShareLink): Promise<void>;
  revokeInvitation(invitationId: string, revokedAt: Date): Promise<boolean>;
  revokeShareLink(shareLinkId: string, revokedAt: Date): Promise<boolean>;
}

export class InvitationError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'InvitationError';
  }
}

export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}

export async function hashToken(token: string): Promise<string> {
  const bytes = new TextEncoder().encode(token);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

export function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '');
}

function requireOwnerIdentity(identity: Identity): void {
  if (identity.role !== 'owner') {
    throw new InvitationError(403, 'owner_required', 'owner access required');
  }
}

function requireMemberRole(role: string): asserts role is MemberRole {
  if (role !== 'editor' && role !== 'viewer') {
    throw new InvitationError(400, 'invalid_role', 'role must be editor or viewer');
  }
}

function requireToken(token: string): void {
  if (!token) {
    throw new InvitationError(400, 'token_required', 'token is required');
  }
}

export async function createInvitation(
  storage: InvitationStorage,
  actor: Identity,
  input: {
    email: string;
    reportId: string;
    role: MemberRole;
  },
  now = new Date(),
): Promise<{ expiresAt: string; id: string; token: string }> {
  requireOwnerIdentity(actor);
  requireMemberRole(input.role);

  const email = normalizeEmail(input.email);
  if (!email) {
    throw new InvitationError(400, 'email_required', 'email is required');
  }

  const token = randomToken();
  const expiresAt = new Date(now.getTime() + INVITATION_LIFETIME_MS);
  const id = crypto.randomUUID();
  await storage.insertInvitation({
    claimedBy: null,
    createdAt: now,
    createdBy: actor.id,
    email,
    expiresAt,
    id,
    reportId: input.reportId,
    revokedAt: null,
    role: input.role,
    tokenHash: await hashToken(token),
    usedAt: null,
  });

  return { expiresAt: expiresAt.toISOString(), id, token };
}

export async function claimInvitation(
  storage: InvitationStorage,
  claimant: Identity,
  token: string,
  now = new Date(),
): Promise<{ reportId: string; role: MemberRole }> {
  requireToken(token);
  const tokenHash = await hashToken(token);

  return storage.claimInvitationAtomic({
    claimedAt: now,
    claimantEmail: normalizeEmail(claimant.email),
    claimantId: claimant.id,
    tokenHash,
  });
}

export async function revokeInvitation(
  storage: InvitationStorage,
  actor: Identity,
  invitationId: string,
  now = new Date(),
): Promise<void> {
  requireOwnerIdentity(actor);
  if (!await storage.revokeInvitation(invitationId, now)) {
    throw new InvitationError(404, 'invitation_not_found', 'invitation is unavailable');
  }
}

export async function createShareLink(
  storage: InvitationStorage,
  actor: Identity,
  input: {
    expiresAt?: Date;
    reportId: string;
  },
  now = new Date(),
): Promise<{ expiresAt: string | null; id: string; token: string }> {
  requireOwnerIdentity(actor);

  const token = randomToken();
  const id = crypto.randomUUID();
  await storage.insertShareLink({
    createdAt: now,
    createdBy: actor.id,
    expiresAt: input.expiresAt ?? null,
    id,
    reportId: input.reportId,
    revokedAt: null,
    tokenHash: await hashToken(token),
  });

  return {
    expiresAt: input.expiresAt?.toISOString() ?? null,
    id,
    token,
  };
}

export async function revokeShareLink(
  storage: InvitationStorage,
  actor: Identity,
  shareLinkId: string,
  now = new Date(),
): Promise<void> {
  requireOwnerIdentity(actor);
  if (!await storage.revokeShareLink(shareLinkId, now)) {
    throw new InvitationError(404, 'share_not_found', 'share link is unavailable');
  }
}

function isActivePublishedShare(share: ShareLinkRecord, now: Date): boolean {
  return (
    share.reportStatus === 'published'
    && !share.revokedAt
    && (!share.expiresAt || share.expiresAt.getTime() > now.getTime())
  );
}

function base64UrlEncode(value: string | Uint8Array): string {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  return btoa(String.fromCharCode(...bytes))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '');
}

function base64UrlDecode(value: string): Uint8Array {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const normalized = value.replaceAll('-', '+').replaceAll('_', '/') + padding;
  return Uint8Array.from(atob(normalized), (character) => character.charCodeAt(0));
}

async function signSession(payload: string, secret: string): Promise<string> {
  if (secret.length < 32) {
    throw new InvitationError(500, 'session_secret_invalid', 'session secret is not configured');
  }
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
    new TextEncoder().encode(payload),
  );
  return base64UrlEncode(new Uint8Array(signature));
}

function equalSignatures(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

interface PublicSessionPayload {
  exp: number;
  reportId: string;
  shareLinkId: string;
  v: 1;
}

export interface PublicSession {
  reportId: string;
  role: 'public_reader';
  shareLinkId: string;
}

async function createPublicSession(
  share: ShareLinkRecord,
  secret: string,
  now: Date,
): Promise<string> {
  const payload = base64UrlEncode(JSON.stringify({
    exp: Math.floor(now.getTime() / 1000) + PUBLIC_SESSION_LIFETIME_SECONDS,
    reportId: share.reportId,
    shareLinkId: share.id,
    v: 1,
  } satisfies PublicSessionPayload));
  return `${payload}.${await signSession(payload, secret)}`;
}

function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(';')) {
    const [key, ...value] = part.trim().split('=');
    if (key === name) return value.join('=') || null;
  }
  return null;
}

async function verifyPublicSession(
  session: string,
  secret: string,
  now: Date,
): Promise<PublicSessionPayload> {
  const [payload, signature, extra] = session.split('.');
  if (!payload || !signature || extra) {
    throw new InvitationError(401, 'public_session_invalid', 'public session is invalid');
  }

  const expected = await signSession(payload, secret);
  if (!equalSignatures(signature, expected)) {
    throw new InvitationError(401, 'public_session_invalid', 'public session is invalid');
  }

  try {
    const decoded = new TextDecoder().decode(base64UrlDecode(payload));
    const value = JSON.parse(decoded) as Partial<PublicSessionPayload>;
    if (
      value.v !== 1
      || typeof value.exp !== 'number'
      || typeof value.reportId !== 'string'
      || typeof value.shareLinkId !== 'string'
      || value.exp <= Math.floor(now.getTime() / 1000)
    ) {
      throw new Error('invalid payload');
    }
    return value as PublicSessionPayload;
  } catch {
    throw new InvitationError(401, 'public_session_invalid', 'public session is invalid');
  }
}

export async function exchangeShareToken(
  storage: InvitationStorage,
  token: string,
  sessionSecret: string,
  appOrigin: string,
  now = new Date(),
): Promise<Response> {
  requireToken(token);
  const share = await storage.findShareLinkByTokenHash(await hashToken(token));
  if (!share || !isActivePublishedShare(share, now)) {
    throw new InvitationError(410, 'share_unavailable', 'share is expired or revoked');
  }

  const session = await createPublicSession(share, sessionSecret, now);
  const location = new URL(`/r/${encodeURIComponent(share.reportId)}`, appOrigin);
  const headers = new Headers({
    'cache-control': 'no-store',
    location: location.toString(),
    'referrer-policy': 'no-referrer',
  });
  headers.append(
    'set-cookie',
    `${PUBLIC_SESSION_COOKIE}=${session}; Max-Age=${PUBLIC_SESSION_LIFETIME_SECONDS}; Path=/; HttpOnly; Secure; SameSite=None`,
  );
  return new Response(null, { headers, status: 302 });
}

export async function readPublicSession(
  cookieHeader: string,
  sessionSecret: string,
  now = new Date(),
): Promise<PublicSession> {
  const session = readCookie(cookieHeader, PUBLIC_SESSION_COOKIE);
  if (!session) {
    throw new InvitationError(401, 'public_session_required', 'public session is required');
  }

  const payload = await verifyPublicSession(session, sessionSecret, now);
  return {
    reportId: payload.reportId,
    role: 'public_reader',
    shareLinkId: payload.shareLinkId,
  };
}

export async function resolvePublicSession(
  storage: InvitationStorage,
  cookieHeader: string,
  sessionSecret: string,
  now = new Date(),
): Promise<{ reportId: string; role: 'public_reader' }> {
  const session = await readPublicSession(cookieHeader, sessionSecret, now);
  const share = await storage.findShareLinkById(session.shareLinkId);
  if (
    !share
    || share.reportId !== session.reportId
    || !isActivePublishedShare(share, now)
  ) {
    throw new InvitationError(410, 'share_unavailable', 'share is expired or revoked');
  }

  return { reportId: share.reportId, role: 'public_reader' };
}
