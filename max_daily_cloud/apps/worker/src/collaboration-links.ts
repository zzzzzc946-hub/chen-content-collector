import type { Identity } from './auth.js';
import { hashToken, randomToken } from './invitations.js';

const COLLABORATION_COOKIE = 'max_daily_collaboration';
const COLLABORATION_SESSION_LIFETIME_SECONDS = 180 * 24 * 60 * 60;
const COLLABORATION_SIGNING_CONTEXT = 'max-daily-collaboration-session:v1';
const COLLABORATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const BASE64_URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const MAX_COOKIE_HEADER_LENGTH = 8192;
const MAX_SESSION_LENGTH = 1024;
const MAX_PAYLOAD_LENGTH = 512;
const MAX_PAYLOAD_BYTES = 384;
const SIGNATURE_LENGTH = 43;
const SIGNATURE_BYTES = 32;

export interface PortalCollaborationLink {
  createdAt: Date;
  createdBy: string;
  id: string;
  lastUsedAt: Date | null;
  revokedAt: Date | null;
  sessionVersion: number;
  tokenHash: string;
}

export interface CollaborationLinkStorage {
  findPortalLinkById(id: string): Promise<PortalCollaborationLink | null>;
  findPortalLinkByTokenHash(hash: string): Promise<PortalCollaborationLink | null>;
  replaceActivePortalLink(record: PortalCollaborationLink): Promise<void>;
  revokePortalLink(id: string, revokedAt: Date): Promise<boolean>;
  touchPortalLink(
    id: string,
    expectedSessionVersion: number,
    usedAt: Date,
  ): Promise<boolean>;
}

export interface CollaborationSession {
  linkId: string;
  role: 'collaborator';
  sessionVersion: number;
}

interface CollaborationSessionPayload {
  exp: number;
  linkId: string;
  sessionVersion: number;
  v: 1;
}

export class CollaborationLinkError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'CollaborationLinkError';
  }
}

function domainError(
  status: number,
  code: string,
  message: string,
): CollaborationLinkError {
  return new CollaborationLinkError(status, code, message);
}

function requireOwner(identity: Identity): void {
  if (identity.role !== 'owner') {
    throw domainError(403, 'owner_required', 'owner access required');
  }
}

function requireToken(token: string): void {
  if (!token) {
    throw domainError(
      400,
      'collaboration_token_required',
      'collaboration token is required',
    );
  }
  if (!COLLABORATION_TOKEN_PATTERN.test(token)) {
    throw domainError(
      400,
      'collaboration_token_invalid',
      'collaboration token is invalid',
    );
  }
}

function requireSessionSecret(secret: string): void {
  if (secret.length < 32) {
    throw domainError(
      500,
      'collaboration_session_secret_invalid',
      'collaboration session secret is not configured',
    );
  }
}

function requireOperationTime(now: Date): Date {
  let timestamp: number;
  try {
    timestamp = now.getTime();
  } catch {
    throw domainError(
      500,
      'collaboration_time_invalid',
      'collaboration time is invalid',
    );
  }
  if (!Number.isFinite(timestamp)) {
    throw domainError(
      500,
      'collaboration_time_invalid',
      'collaboration time is invalid',
    );
  }
  return new Date(timestamp);
}

function storageUnavailable(): CollaborationLinkError {
  return domainError(
    503,
    'collaboration_storage_unavailable',
    'collaboration storage is unavailable',
  );
}

async function readFromStorage<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch {
    throw storageUnavailable();
  }
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

async function signPayload(payload: string, secret: string): Promise<string> {
  requireSessionSecret(secret);
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
      `${COLLABORATION_SIGNING_CONTEXT}:${payload}`,
    ),
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

function invalidSession(): CollaborationLinkError {
  return domainError(
    401,
    'collaboration_session_invalid',
    'collaboration session is invalid or expired',
  );
}

function readCollaborationCookie(cookieHeader: string): string | null {
  if (cookieHeader.length > MAX_COOKIE_HEADER_LENGTH) throw invalidSession();

  let session: string | null = null;
  for (const part of cookieHeader.split(';')) {
    const [key, ...value] = part.trim().split('=');
    if (key !== COLLABORATION_COOKIE) continue;
    if (session !== null) throw invalidSession();
    session = value.join('=');
    if (!session) throw invalidSession();
  }
  return session;
}

function decodeCanonicalBase64Url(value: string): Uint8Array | null {
  if (!BASE64_URL_PATTERN.test(value)) return null;
  try {
    const decoded = base64UrlDecode(value);
    return base64UrlEncode(decoded) === value ? decoded : null;
  } catch {
    return null;
  }
}

function hasExactPayloadKeys(value: Record<string, unknown>): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === 4
    && keys[0] === 'exp'
    && keys[1] === 'linkId'
    && keys[2] === 'sessionVersion'
    && keys[3] === 'v';
}

async function verifySession(
  session: string,
  secret: string,
  now: Date,
): Promise<CollaborationSessionPayload> {
  if (session.length > MAX_SESSION_LENGTH) throw invalidSession();
  const [payload, signature, extra] = session.split('.');
  if (
    !payload
    || !signature
    || extra
    || payload.length > MAX_PAYLOAD_LENGTH
    || signature.length !== SIGNATURE_LENGTH
  ) {
    throw invalidSession();
  }

  const payloadBytes = decodeCanonicalBase64Url(payload);
  const signatureBytes = decodeCanonicalBase64Url(signature);
  if (
    !payloadBytes
    || payloadBytes.byteLength > MAX_PAYLOAD_BYTES
    || !signatureBytes
    || signatureBytes.byteLength !== SIGNATURE_BYTES
  ) {
    throw invalidSession();
  }

  const expected = await signPayload(payload, secret);
  if (!equalSignatures(signature, expected)) throw invalidSession();

  try {
    const parsed = JSON.parse(
      new TextDecoder().decode(payloadBytes),
    ) as unknown;
    if (
      !parsed
      || typeof parsed !== 'object'
      || Array.isArray(parsed)
      || !hasExactPayloadKeys(parsed as Record<string, unknown>)
    ) {
      throw new Error('invalid payload');
    }

    const value = parsed as Partial<CollaborationSessionPayload>;
    if (
      value.v !== 1
      || !Number.isInteger(value.exp)
      || typeof value.exp !== 'number'
      || value.exp <= Math.floor(now.getTime() / 1000)
      || typeof value.linkId !== 'string'
      || !value.linkId
      || !Number.isInteger(value.sessionVersion)
      || typeof value.sessionVersion !== 'number'
      || value.sessionVersion <= 0
    ) {
      throw new Error('invalid payload');
    }
    return value as CollaborationSessionPayload;
  } catch {
    throw invalidSession();
  }
}

async function createSession(
  link: PortalCollaborationLink,
  secret: string,
  now: Date,
): Promise<string> {
  const payload = base64UrlEncode(new TextEncoder().encode(JSON.stringify({
    exp: Math.floor(now.getTime() / 1000)
      + COLLABORATION_SESSION_LIFETIME_SECONDS,
    linkId: link.id,
    sessionVersion: link.sessionVersion,
    v: 1,
  } satisfies CollaborationSessionPayload)));
  return `${payload}.${await signPayload(payload, secret)}`;
}

function collaborationRedirect(appOrigin: string): URL {
  try {
    const origin = new URL(appOrigin);
    const localDevelopmentOrigin = origin.protocol === 'http:'
      && (origin.hostname === 'localhost'
        || origin.hostname === '127.0.0.1'
        || origin.hostname === '[::1]');
    if (
      (origin.protocol !== 'https:' && !localDevelopmentOrigin)
      || origin.username
      || origin.password
      || origin.pathname !== '/'
      || origin.search
      || origin.hash
    ) {
      throw new Error('unsafe origin');
    }
    return new URL('/daily', origin.origin);
  } catch {
    throw domainError(
      500,
      'app_origin_invalid',
      'application origin is not configured',
    );
  }
}

function isActiveLink(link: PortalCollaborationLink | null): link is PortalCollaborationLink {
  return Boolean(
    link
    && !link.revokedAt
    && link.id
    && Number.isInteger(link.sessionVersion)
    && link.sessionVersion > 0,
  );
}

async function requireAtomicTouch(
  storage: CollaborationLinkStorage,
  linkId: string,
  expectedSessionVersion: number,
  usedAt: Date,
): Promise<void> {
  const touched = await readFromStorage(
    () => storage.touchPortalLink(linkId, expectedSessionVersion, usedAt),
  );
  if (!touched) {
    throw domainError(
      410,
      'collaboration_link_unavailable',
      'collaboration link is unavailable',
    );
  }
}

export async function createCollaborationLink(
  storage: CollaborationLinkStorage,
  actor: Identity,
  now = new Date(),
): Promise<{ createdAt: string; id: string; token: string }> {
  const operationTime = requireOperationTime(now);
  requireOwner(actor);

  const token = randomToken();
  const record: PortalCollaborationLink = {
    createdAt: operationTime,
    createdBy: actor.id,
    id: crypto.randomUUID(),
    lastUsedAt: null,
    revokedAt: null,
    sessionVersion: 1,
    tokenHash: await hashToken(token),
  };
  await readFromStorage(() => storage.replaceActivePortalLink(record));

  return {
    createdAt: operationTime.toISOString(),
    id: record.id,
    token,
  };
}

export async function revokeCollaborationLink(
  storage: CollaborationLinkStorage,
  actor: Identity,
  linkId: string,
  now = new Date(),
): Promise<void> {
  const operationTime = requireOperationTime(now);
  requireOwner(actor);
  const revoked = await readFromStorage(
    () => storage.revokePortalLink(linkId, operationTime),
  );
  if (!revoked) {
    throw domainError(
      404,
      'collaboration_link_not_found',
      'collaboration link is unavailable',
    );
  }
}

export async function exchangeCollaborationToken(
  storage: CollaborationLinkStorage,
  token: string,
  sessionSecret: string,
  appOrigin: string,
  now = new Date(),
): Promise<Response> {
  const operationTime = requireOperationTime(now);
  requireToken(token);
  requireSessionSecret(sessionSecret);
  const redirect = collaborationRedirect(appOrigin);
  const tokenHash = await hashToken(token);
  const link = await readFromStorage(
    () => storage.findPortalLinkByTokenHash(tokenHash),
  );
  if (!isActiveLink(link)) {
    throw domainError(
      410,
      'collaboration_link_unavailable',
      'collaboration link is unavailable',
    );
  }

  await requireAtomicTouch(
    storage,
    link.id,
    link.sessionVersion,
    operationTime,
  );
  const session = await createSession(link, sessionSecret, operationTime);
  const headers = new Headers({
    'cache-control': 'no-store',
    location: redirect.toString(),
    'referrer-policy': 'no-referrer',
  });
  headers.append(
    'set-cookie',
    `${COLLABORATION_COOKIE}=${session}; Max-Age=${COLLABORATION_SESSION_LIFETIME_SECONDS}; Path=/; HttpOnly; Secure; SameSite=Lax`,
  );
  return new Response(null, { headers, status: 302 });
}

export async function resolveCollaborationSession(
  storage: CollaborationLinkStorage,
  cookieHeader: string,
  sessionSecret: string,
  now = new Date(),
): Promise<CollaborationSession> {
  const operationTime = requireOperationTime(now);
  requireSessionSecret(sessionSecret);
  const session = readCollaborationCookie(cookieHeader);
  if (!session) {
    throw domainError(
      401,
      'collaboration_session_required',
      'collaboration session is required',
    );
  }

  const payload = await verifySession(session, sessionSecret, operationTime);
  const link = await readFromStorage(
    () => storage.findPortalLinkById(payload.linkId),
  );
  if (
    !isActiveLink(link)
    || link.sessionVersion !== payload.sessionVersion
  ) {
    throw domainError(
      410,
      'collaboration_link_unavailable',
      'collaboration link is unavailable',
    );
  }

  await requireAtomicTouch(
    storage,
    link.id,
    payload.sessionVersion,
    operationTime,
  );
  return {
    linkId: link.id,
    role: 'collaborator',
    sessionVersion: link.sessionVersion,
  };
}
