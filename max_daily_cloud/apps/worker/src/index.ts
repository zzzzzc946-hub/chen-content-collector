import { Hono } from 'hono';
import { HTTPException } from 'hono/http-exception';
import type { Context } from 'hono';
import type { ContentfulStatusCode } from 'hono/utils/http-status';
import { createSupabaseIdentityVerifier, requireIdentity } from './auth.js';
import type { Identity } from './auth.js';
import {
  CollaborationLinkError,
  createCollaborationLink,
  exchangeCollaborationToken,
  resolveCollaborationSession,
  revokeCollaborationLink,
  type CollaborationSession,
} from './collaboration-links.js';
import type {
  CurrentCollaborationLink,
  WorkerAppEnv,
  WorkerServices,
} from './env.js';
import {
  InvitationError,
  claimInvitation,
  createInvitation,
  createShareLink,
  exchangeShareToken,
  readPublicSession,
  resolvePublicSession,
  revokeInvitation,
  revokeShareLink,
  type MemberRole,
  type PublicSession,
} from './invitations.js';
import {
  MediaError,
  abortMediaUpload,
  cleanupStaleMediaUploads,
  completeMediaUpload,
  createMediaUpload,
  disabledPublisherDeviceAuthenticator,
  listMediaUploadParts,
  purgeExpiredMediaObjects,
  readMedia,
  uploadMediaPart,
  type MediaBucket,
  type MediaCleanupResult,
  type MediaObjectStore,
  type MediaStorage,
  type MediaUploadActor,
} from './media.js';
import {
  createFeishuMobileInboxStore,
  MobileInboxError,
  readMobileInbox,
  readMobileInboxJson,
  submitMobileInbox,
  type MobileInboxStore,
} from './mobile-inbox.js';
import { requireOwner } from './permissions.js';
import { createPublisherDeviceAuthenticator } from './publisher-auth.js';
import { createMediaSessionCookie, readMediaSession } from './media-session.js';
import {
  applyReportMediaPolicy,
  ReportError,
  getReportForCollaborator,
  type PublisherDraftItemInput,
  getReportForIdentity,
  getReportForPublicSession,
  listReportsForCollaborator,
  listReportsForOwner,
  patchItem,
  patchItemForCollaborator,
  publishReportFromPublisher,
  publishReport,
  restoreRevision,
  upsertPublisherDraftReport,
} from './reports.js';
import { createSupabaseRestStorage } from './storage.js';
import { SupabaseMediaStore } from './supabase-media.js';
import type { ReportItemField } from '../../../packages/shared/src/contracts.js';

interface InvitationBody {
  email?: unknown;
  reportId?: unknown;
  role?: unknown;
}

interface ClaimBody {
  token?: unknown;
}

interface ShareBody {
  expiresAt?: unknown;
  reportId?: unknown;
}

interface PatchItemBody {
  expectedVersion?: unknown;
  field?: unknown;
  value?: unknown;
}

interface RestoreRevisionBody {
  expectedVersion?: unknown;
}

interface CreateMediaUploadBody {
  byteSize?: unknown;
  contentType?: unknown;
  reportId?: unknown;
  reportItemId?: unknown;
}

interface CompleteMediaUploadBody {
  sha256?: unknown;
}

interface PublisherDraftItemBody {
  caption?: unknown;
  itemOrder?: unknown;
  localRecordId?: unknown;
  maxDailyCard?: unknown;
  maxFeedback?: unknown;
  reviewStatus?: unknown;
  sourceUrl?: unknown;
  title?: unknown;
}

interface PublisherDraftBody {
  dailyDate?: unknown;
  items?: unknown;
  sourceTableId?: unknown;
}

interface PublisherPublishBody {
  expectedDraftVersion?: unknown;
}

type ReportActor =
  | { identity: Identity; kind: 'identity' }
  | { kind: 'collaborator'; session: CollaborationSession }
  | { kind: 'public'; session: PublicSession };

type MediaSessionActor =
  | { actorId: string; kind: 'identity' }
  | { kind: 'collaborator'; sessionVersion: number; linkId: string };

interface CollaboratorMediaSessionPayload {
  exp: number;
  subject: {
    kind: 'collaborator';
    linkId: string;
    sessionVersion: number;
  };
  v: 2;
}

const COLLABORATION_COOKIE = 'max_daily_collaboration';
const PUBLIC_COOKIE = 'max_daily_public';
const MEDIA_COOKIE = 'max_daily_media';
const MEDIA_SESSION_LIFETIME_SECONDS = 5 * 60;
const COLLABORATOR_MEDIA_SIGNING_CONTEXT = 'max-daily-media-session:v2';
const MAX_COOKIE_HEADER_LENGTH = 8192;
const MAX_MEDIA_SESSION_LENGTH = 1024;

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

function mediaSessionInvalid(): MediaError {
  return new MediaError(
    401,
    'media_session_invalid',
    'media session is invalid or expired',
  );
}

function readUniqueCookie(cookieHeader: string, name: string): string | null {
  if (cookieHeader.length > MAX_COOKIE_HEADER_LENGTH) throw mediaSessionInvalid();
  let found: string | null = null;
  for (const part of cookieHeader.split(';')) {
    const [key, ...value] = part.trim().split('=');
    if (key !== name) continue;
    if (found !== null) throw mediaSessionInvalid();
    found = value.join('=');
    if (!found) throw mediaSessionInvalid();
  }
  return found;
}

function hasCookie(cookieHeader: string, name: string): boolean {
  return cookieHeader.split(';').some((part) => {
    const separator = part.indexOf('=');
    return separator >= 0 && part.slice(0, separator).trim() === name;
  });
}

function requireRuntimeSecret(name: string, secret: string): void {
  if (typeof secret !== 'string' || secret.length < 32) {
    throw new Error(`${name} must be at least 32 characters`);
  }
}

async function signCollaboratorMediaPayload(
  payload: string,
  secret: string,
): Promise<string> {
  if (secret.length < 32) {
    throw new MediaError(
      500,
      'media_session_secret_invalid',
      'media session secret is not configured',
    );
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
    new TextEncoder().encode(
      `${COLLABORATOR_MEDIA_SIGNING_CONTEXT}:${payload}`,
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

async function createCollaboratorMediaSessionCookie(
  session: CollaborationSession,
  secret: string,
  createdAt: Date,
): Promise<string> {
  const payload = base64UrlEncode(new TextEncoder().encode(JSON.stringify({
    exp: Math.floor(createdAt.getTime() / 1000) + MEDIA_SESSION_LIFETIME_SECONDS,
    subject: {
      kind: 'collaborator',
      linkId: session.linkId,
      sessionVersion: session.sessionVersion,
    },
    v: 2,
  } satisfies CollaboratorMediaSessionPayload)));
  const signature = await signCollaboratorMediaPayload(payload, secret);
  return `${MEDIA_COOKIE}=${payload}.${signature}; Max-Age=${MEDIA_SESSION_LIFETIME_SECONDS}; Path=/api/media; HttpOnly; Secure; SameSite=None`;
}

function parseUntrustedMediaVersion(session: string): number | null {
  if (session.length > MAX_MEDIA_SESSION_LENGTH) throw mediaSessionInvalid();
  const [payload] = session.split('.', 1);
  if (!payload) throw mediaSessionInvalid();
  try {
    const value = JSON.parse(
      new TextDecoder().decode(base64UrlDecode(payload)),
    ) as { v?: unknown };
    return typeof value.v === 'number' ? value.v : null;
  } catch {
    throw mediaSessionInvalid();
  }
}

async function readCollaboratorMediaSession(
  session: string,
  secret: string,
  readAt: Date,
): Promise<Extract<MediaSessionActor, { kind: 'collaborator' }>> {
  const [payload, signature, extra] = session.split('.');
  if (!payload || !signature || extra) throw mediaSessionInvalid();
  const expected = await signCollaboratorMediaPayload(payload, secret);
  if (!equalSignatures(signature, expected)) throw mediaSessionInvalid();

  try {
    const value = JSON.parse(
      new TextDecoder().decode(base64UrlDecode(payload)),
    ) as Partial<CollaboratorMediaSessionPayload>;
    const subject = value.subject;
    if (
      value.v !== 2
      || typeof value.exp !== 'number'
      || !Number.isInteger(value.exp)
      || value.exp <= Math.floor(readAt.getTime() / 1000)
      || !subject
      || subject.kind !== 'collaborator'
      || typeof subject.linkId !== 'string'
      || !subject.linkId
      || typeof subject.sessionVersion !== 'number'
      || !Number.isInteger(subject.sessionVersion)
      || subject.sessionVersion <= 0
    ) {
      throw new Error('invalid media session');
    }
    return {
      kind: 'collaborator',
      linkId: subject.linkId,
      sessionVersion: subject.sessionVersion,
    };
  } catch {
    throw mediaSessionInvalid();
  }
}

async function readMediaSessionActor(
  cookieHeader: string,
  secret: string,
  readAt: Date,
): Promise<MediaSessionActor | null> {
  const session = readUniqueCookie(cookieHeader, MEDIA_COOKIE);
  if (!session) return null;
  if (parseUntrustedMediaVersion(session) === 2) {
    return readCollaboratorMediaSession(session, secret, readAt);
  }
  const identitySession = await readMediaSession(cookieHeader, secret, readAt);
  return identitySession
    ? { actorId: identitySession.actorId, kind: 'identity' }
    : null;
}

function now(services: WorkerServices): Date {
  return services.now?.() ?? new Date();
}

function appendVary(headers: Headers, value: string): void {
  const values = new Set(
    (headers.get('vary') ?? '')
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean),
  );
  values.add(value);
  headers.set('vary', [...values].join(', '));
}

function applyCredentialedCors(headers: Headers, origin: string): void {
  headers.set('access-control-allow-credentials', 'true');
  headers.set('access-control-allow-origin', origin);
  headers.set(
    'access-control-expose-headers',
    'Accept-Ranges, Content-Length, Content-Range, Content-Type',
  );
  appendVary(headers, 'Origin');
}

function parseMemberRole(value: unknown): MemberRole {
  if (value !== 'editor' && value !== 'viewer') {
    throw new InvitationError(400, 'invalid_role', 'role must be editor or viewer');
  }
  return value;
}

function parseRequiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new InvitationError(400, 'invalid_request', `${field} is required`);
  }
  return value;
}

function parseOptionalDate(value: unknown): Date | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') {
    throw new InvitationError(400, 'invalid_expiration', 'expiration must be an ISO date');
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    throw new InvitationError(400, 'invalid_expiration', 'expiration must be an ISO date');
  }
  return date;
}

function parseExpectedVersion(value: unknown): number {
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw new ReportError(
      400,
      'invalid_version',
      'expectedVersion must be a non-negative integer',
    );
  }
  return value as number;
}

function parseReportItemField(value: unknown): ReportItemField {
  if (
    value !== 'max_daily_card'
    && value !== 'max_feedback'
    && value !== 'review_status'
    && value !== 'title'
    && value !== 'caption'
    && value !== 'source_url'
  ) {
    throw new ReportError(400, 'invalid_field', 'field is invalid');
  }
  return value;
}

function parseRevisionId(value: string): number {
  const revisionId = Number(value);
  if (!Number.isSafeInteger(revisionId) || revisionId <= 0) {
    throw new ReportError(400, 'invalid_revision', 'revision id is invalid');
  }
  return revisionId;
}

function parseStringValue(value: unknown): string {
  if (typeof value !== 'string') {
    throw new ReportError(400, 'invalid_value', 'value must be a string');
  }
  return value;
}

function parsePublisherDate(value: unknown): string {
  const text = parseRequiredString(value, 'dailyDate');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    throw new ReportError(400, 'invalid_daily_date', 'dailyDate is invalid');
  }
  return text;
}

function parsePublisherItem(value: unknown): PublisherDraftItemInput {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ReportError(400, 'invalid_item', 'publisher item is invalid');
  }
  const item = value as PublisherDraftItemBody;
  if (!Number.isSafeInteger(item.itemOrder)) {
    throw new ReportError(400, 'invalid_item_order', 'itemOrder must be an integer');
  }
  return {
    caption: parseStringValue(item.caption),
    itemOrder: item.itemOrder as number,
    localRecordId: parseRequiredString(item.localRecordId, 'localRecordId'),
    maxDailyCard: parseStringValue(item.maxDailyCard),
    maxFeedback: parseStringValue(item.maxFeedback),
    reviewStatus: parseStringValue(item.reviewStatus),
    sourceUrl: parseStringValue(item.sourceUrl),
    title: parseStringValue(item.title),
  };
}

function parseMediaString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new MediaError(400, 'invalid_request', `${field} is required`);
  }
  return value;
}

function parseMediaByteSize(value: unknown): number {
  if (!Number.isSafeInteger(value)) {
    throw new MediaError(400, 'invalid_byte_size', 'byteSize must be an integer');
  }
  return value as number;
}

function parsePartNumber(value: string): number {
  if (!/^\d+$/.test(value)) {
    throw new MediaError(
      400,
      'invalid_part_number',
      'part number must be an integer',
    );
  }
  const partNumber = Number(value);
  if (!Number.isSafeInteger(partNumber)) {
    throw new MediaError(
      400,
      'invalid_part_number',
      'part number must be an integer',
    );
  }
  return partNumber;
}

function requireMediaServices(services: WorkerServices): {
  bucket?: MediaBucket;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
} {
  const storage = services.storage as Partial<MediaStorage>;
  if (
    (!services.mediaBucket && !services.mediaObjectStore)
    || typeof storage.attachMediaUploadAtomic !== 'function'
    || typeof storage.authorizeMediaReadAtomic !== 'function'
    || typeof storage.beginMediaUploadAtomic !== 'function'
    || typeof storage.claimMediaUploadAbortAtomic !== 'function'
    || typeof storage.claimMediaUploadCompletionAtomic !== 'function'
    || typeof storage.finalizeMediaUploadAbortAtomic !== 'function'
    || typeof storage.finalizeMediaUploadCompletionAtomic !== 'function'
    || typeof storage.getMediaUploadAtomic !== 'function'
    || typeof storage.listMediaUploadPartsAtomic !== 'function'
    || typeof storage.listMediaUploadRecoveryPartsAtomic !== 'function'
    || typeof storage.listMediaUploadRecoveryPage !== 'function'
    || typeof storage.recordMediaUploadPartAtomic !== 'function'
    || typeof storage.resetStaleMediaUploadCompletionAtomic !== 'function'
    || typeof storage.claimStaleMediaUploadCompletionAtomic !== 'function'
  ) {
    throw new MediaError(
      500,
      'media_service_unavailable',
      'media service is not configured',
    );
  }
  return {
    bucket: services.mediaBucket,
    objectStore: services.mediaObjectStore,
    storage: storage as MediaStorage,
  };
}

async function requireMediaUploadActor(
  c: Context<WorkerAppEnv>,
): Promise<MediaUploadActor> {
  const services = c.get('services');
  const publisher = await (
    services.publisherDeviceAuthenticator
    ?? disabledPublisherDeviceAuthenticator
  ).authenticate(c.req.raw);
  if (publisher) {
    return { actorId: null, publisherDeviceId: publisher.deviceId };
  }

  const identity = await requireIdentity(c);
  if (identity.role !== 'owner') {
    throw new MediaError(403, 'owner_required', 'owner access required');
  }
  return { actorId: identity.id, publisherDeviceId: null };
}

async function requirePublisherDevice(
  c: Context<WorkerAppEnv>,
): Promise<string> {
  const services = c.get('services');
  const publisher = await (
    services.publisherDeviceAuthenticator
    ?? disabledPublisherDeviceAuthenticator
  ).authenticate(c.req.raw);
  if (!publisher) {
    throw new MediaError(
      401,
      'missing_publisher_token',
      'publisher token is required',
    );
  }
  return publisher.deviceId;
}

function currentCollaborationLinkReader(
  env: WorkerAppEnv['Bindings'],
): () => Promise<CurrentCollaborationLink | null> {
  const endpoint = new URL('/rest/v1/portal_collaboration_links', env.SUPABASE_URL);
  endpoint.searchParams.set('revoked_at', 'is.null');
  endpoint.searchParams.set('select', 'id,created_at,last_used_at');
  endpoint.searchParams.set('limit', '1');

  return async () => {
    try {
      const response = await fetch(endpoint, {
        headers: {
          apikey: env.SUPABASE_SERVICE_ROLE_KEY,
          authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
        },
      });
      if (!response.ok) throw new Error('storage request failed');
      const value = await response.json() as unknown;
      if (!Array.isArray(value)) throw new Error('invalid storage response');
      const row = value[0] as Record<string, unknown> | undefined;
      if (!row) return null;
      const createdAt = typeof row.created_at === 'string'
        ? new Date(row.created_at)
        : null;
      const lastUsedAt = row.last_used_at === null
        ? null
        : typeof row.last_used_at === 'string'
          ? new Date(row.last_used_at)
          : undefined;
      if (
        typeof row.id !== 'string'
        || !row.id
        || !createdAt
        || Number.isNaN(createdAt.getTime())
        || lastUsedAt === undefined
        || (lastUsedAt !== null && Number.isNaN(lastUsedAt.getTime()))
      ) {
        throw new Error('invalid storage response');
      }
      return { createdAt, id: row.id, lastUsedAt };
    } catch {
      throw new CollaborationLinkError(
        503,
        'collaboration_storage_unavailable',
        'collaboration storage is unavailable',
      );
    }
  };
}

async function readCurrentCollaborationLink(
  services: WorkerServices,
): Promise<CurrentCollaborationLink | null> {
  if (!services.readCurrentCollaborationLink) {
    throw new CollaborationLinkError(
      500,
      'collaboration_status_unavailable',
      'collaboration status is unavailable',
    );
  }
  return services.readCurrentCollaborationLink();
}

async function resolveReportActor(
  c: Context<WorkerAppEnv>,
): Promise<ReportActor> {
  if (c.req.header('authorization') !== undefined) {
    return { identity: await requireIdentity(c), kind: 'identity' };
  }

  const services = c.get('services');
  const cookieHeader = c.req.header('cookie') ?? '';
  const readAt = now(services);
  if (hasCookie(cookieHeader, COLLABORATION_COOKIE)) {
    return {
      kind: 'collaborator',
      session: await resolveCollaborationSession(
        services.storage,
        cookieHeader,
        services.shareCookieSecret,
        readAt,
      ),
    };
  }

  const session = await readPublicSession(
    cookieHeader,
    services.shareCookieSecret,
    readAt,
  );
  const resolved = await resolvePublicSession(
    services.storage,
    cookieHeader,
    services.shareCookieSecret,
    readAt,
  );
  if (resolved.reportId !== session.reportId) {
    throw new InvitationError(
      410,
      'share_unavailable',
      'share is expired or revoked',
    );
  }
  return { kind: 'public', session };
}

export function createConfiguredServices(
  env: WorkerAppEnv['Bindings'],
): WorkerServices {
  requireRuntimeSecret('MEDIA_SESSION_SECRET', env.MEDIA_SESSION_SECRET);
  requireRuntimeSecret('SHARE_COOKIE_SECRET', env.SHARE_COOKIE_SECRET);
  const bucket = env.SUPABASE_STORAGE_BUCKET.trim();
  if (!bucket) {
    throw new Error('SUPABASE_STORAGE_BUCKET is required');
  }
  const storage = createSupabaseRestStorage({
    serviceRoleKey: env.SUPABASE_SERVICE_ROLE_KEY,
    supabaseUrl: env.SUPABASE_URL,
  });
  return {
    appOrigin: env.APP_ORIGIN,
    identityVerifier: createSupabaseIdentityVerifier({
      anonKey: env.SUPABASE_ANON_KEY,
      supabaseUrl: env.SUPABASE_URL,
    }),
    invitationClaimRateLimiter: env.INVITATION_CLAIM_RATE_LIMITER,
    mediaObjectStore: new SupabaseMediaStore({
      bucket,
      serviceRoleKey: env.SUPABASE_SERVICE_ROLE_KEY,
      supabaseUrl: env.SUPABASE_URL,
    }),
    mediaSessionSecret: env.MEDIA_SESSION_SECRET,
    mobileInbox: createFeishuMobileInboxStore({
      appId: env.FEISHU_APP_ID,
      appSecret: env.FEISHU_APP_SECRET,
      appToken: env.FEISHU_APP_TOKEN,
      tableId: env.FEISHU_MOBILE_INBOX_TABLE_ID,
    }),
    mobileInboxRateLimiter: env.MOBILE_INBOX_RATE_LIMITER,
    ownerEmail: env.OWNER_EMAIL,
    publicShareRateLimiter: env.PUBLIC_SHARE_RATE_LIMITER,
    publisherDeviceAuthenticator: createPublisherDeviceAuthenticator({
      pepper: env.PUBLISHER_TOKEN_PEPPER,
      storage,
    }),
    readCurrentCollaborationLink: currentCollaborationLinkReader(env),
    shareCookieSecret: env.SHARE_COOKIE_SECRET,
    storage,
  };
}

export async function runScheduledMediaCleanup(
  services: WorkerServices,
): Promise<MediaCleanupResult> {
  const media = requireMediaServices(services);
  const cleanupResult = await cleanupStaleMediaUploads({
    bucket: media.bucket,
    now: now(services),
    objectStore: media.objectStore,
    storage: media.storage,
  });
  if (media.objectStore) {
    if (
      typeof media.storage.claimExpiredMediaObjects !== 'function'
      || typeof media.storage.finishMediaObjectPurge !== 'function'
    ) {
      throw new MediaError(
        500,
        'media_service_unavailable',
        'media retention service is not configured',
      );
    }
    await purgeExpiredMediaObjects({
      now: now(services),
      objectStore: media.objectStore,
      storage: media.storage,
    });
  }
  return cleanupResult;
}

function deferMediaCleanup(
  c: Context<WorkerAppEnv>,
  services: WorkerServices,
): void {
  const cleanup = runScheduledMediaCleanup(services).catch((error: unknown) => {
    console.error('scheduled media cleanup failed', error);
  });
  try {
    c.executionCtx.waitUntil(cleanup);
  } catch {
    // Unit requests have no Worker execution context; keep the detached
    // promise handled without delaying the upload response.
    void cleanup;
  }
}

async function requireRateLimit(
  limiter: WorkerServices['invitationClaimRateLimiter'],
  key: string,
): Promise<void> {
  if (!(await limiter.limit({ key })).success) {
    throw new InvitationError(429, 'rate_limited', 'too many requests');
  }
}

function requireMobileInboxStore(services: WorkerServices): MobileInboxStore {
  if (!services.mobileInbox) {
    throw new MobileInboxError(
      503,
      'mobile_inbox_unavailable',
      '手机收集服务暂时不可用',
    );
  }
  return services.mobileInbox;
}

async function requireMobileInboxRateLimit(
  services: WorkerServices,
  key: string,
): Promise<void> {
  const limiter = services.mobileInboxRateLimiter;
  if (!limiter) {
    throw new MobileInboxError(
      503,
      'mobile_inbox_unavailable',
      '手机收集服务暂时不可用',
    );
  }
  if (!(await limiter.limit({ key })).success) {
    throw new MobileInboxError(429, 'rate_limited', 'too many requests');
  }
}

function mapMobileInboxUpstreamError(error: unknown): never {
  if (error instanceof MobileInboxError) throw error;
  throw new MobileInboxError(
    502,
    'feishu_unavailable',
    '手机收集服务暂时不可用',
  );
}

function buildWorkerApp(injectedServices?: WorkerServices): Hono<WorkerAppEnv> {
  const app = new Hono<WorkerAppEnv>();

  app.use('*', async (c, next) => {
    c.set('services', injectedServices ?? createConfiguredServices(c.env));
    await next();
  });

  app.use('/api/*', async (c, next) => {
    const origin = c.req.header('origin');
    if (!origin) {
      await next();
      return;
    }

    const services = c.get('services');
    const configuredOrigin = new URL(services.appOrigin).origin;
    if (origin !== configuredOrigin) {
      throw new HTTPException(403, { message: 'origin is not allowed' });
    }

    if (c.req.method === 'OPTIONS') {
      const headers = new Headers({
        'access-control-allow-headers':
          'Authorization, Content-Type, Range, X-Publisher-Token',
        'access-control-allow-methods':
          'GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS',
        'access-control-max-age': '600',
      });
      applyCredentialedCors(headers, configuredOrigin);
      return new Response(null, {
        headers,
        status: 204,
      });
    }
    await next();
    const headers = new Headers(c.res.headers);
    applyCredentialedCors(headers, configuredOrigin);
    c.res = new Response(c.res.body, {
      headers,
      status: c.res.status,
      statusText: c.res.statusText,
    });
  });

  app.get('/api/mobile-inbox', async (c) => {
    const services = c.get('services');
    try {
      await requireMobileInboxRateLimit(
        services,
        `read:${(c.req.header('cf-connecting-ip') ?? 'unknown').slice(0, 128)}`,
      );
      const store = requireMobileInboxStore(services);
      c.header('cache-control', 'no-store');
      return c.json(await readMobileInbox(store), 200);
    } catch (error) {
      mapMobileInboxUpstreamError(error);
    }
  });

  app.post('/api/mobile-inbox', async (c) => {
    const services = c.get('services');
    try {
      await requireMobileInboxRateLimit(
        services,
        `ip:${(c.req.header('cf-connecting-ip') ?? 'unknown').slice(0, 128)}`,
      );
      const body = await readMobileInboxJson(c.req.raw, 32 * 1024);
      c.header('cache-control', 'no-store');
      return c.json(await submitMobileInbox(
        requireMobileInboxStore(services),
        body,
        now(services),
      ), 201);
    } catch (error) {
      mapMobileInboxUpstreamError(error);
    }
  });

  app.post('/api/collaboration-links', async (c) => {
    const actor = await requireOwner(c);
    const services = c.get('services');
    const link = await createCollaborationLink(
      services.storage,
      actor,
      now(services),
    );
    c.header('cache-control', 'no-store');
    return c.json(link, 201);
  });

  app.get('/api/collaboration-links/current', async (c) => {
    await requireOwner(c);
    const link = await readCurrentCollaborationLink(c.get('services'));
    c.header('cache-control', 'no-store');
    return c.json(link
      ? {
          active: true,
          createdAt: link.createdAt.toISOString(),
          id: link.id,
          lastUsedAt: link.lastUsedAt?.toISOString() ?? null,
        }
      : { active: false }, 200);
  });

  app.delete('/api/collaboration-links/:id', async (c) => {
    const actor = await requireOwner(c);
    const services = c.get('services');
    await revokeCollaborationLink(
      services.storage,
      actor,
      parseRequiredString(c.req.param('id'), 'collaboration link id'),
      now(services),
    );
    return c.body(null, 204);
  });

  app.get('/c/:token', async (c) => {
    const services = c.get('services');
    const edgeIp = c.req.header('cf-connecting-ip')?.trim() || 'unknown';
    await requireRateLimit(
      services.publicShareRateLimiter,
      `ip:${edgeIp.slice(0, 128)}`,
    );
    return exchangeCollaborationToken(
      services.storage,
      c.req.param('token'),
      services.shareCookieSecret,
      services.appOrigin,
      now(services),
    );
  });

  app.post('/api/invitations', async (c) => {
    const actor = await requireOwner(c);
    const body = await c.req.json<InvitationBody>();
    const services = c.get('services');
    const invitation = await createInvitation(
      services.storage,
      actor,
      {
        email: parseRequiredString(body.email, 'email'),
        reportId: parseRequiredString(body.reportId, 'reportId'),
        role: parseMemberRole(body.role),
      },
      now(services),
    );
    c.header('cache-control', 'no-store');
    return c.json(invitation, 201);
  });

  app.post('/api/invitations/claim', async (c) => {
    const claimant = await requireIdentity(c);
    const services = c.get('services');
    await requireRateLimit(
      services.invitationClaimRateLimiter,
      `identity:${claimant.id}`,
    );
    const body = await c.req.json<ClaimBody>();
    const membership = await claimInvitation(
      services.storage,
      claimant,
      parseRequiredString(body.token, 'token'),
      now(services),
    );
    return c.json(membership, 200);
  });

  app.delete('/api/invitations/:id', async (c) => {
    const actor = await requireOwner(c);
    const services = c.get('services');
    await revokeInvitation(
      services.storage,
      actor,
      parseRequiredString(c.req.param('id'), 'invitation id'),
      now(services),
    );
    return c.body(null, 204);
  });

  app.post('/api/shares', async (c) => {
    const actor = await requireOwner(c);
    const body = await c.req.json<ShareBody>();
    const services = c.get('services');
    const share = await createShareLink(
      services.storage,
      actor,
      {
        expiresAt: parseOptionalDate(body.expiresAt),
        reportId: parseRequiredString(body.reportId, 'reportId'),
      },
      now(services),
    );
    c.header('cache-control', 'no-store');
    return c.json(share, 201);
  });

  app.get('/api/shares/exchange', async (c) => {
    const services = c.get('services');
    const edgeIp = c.req.header('cf-connecting-ip')?.trim() || 'unknown';
    await requireRateLimit(
      services.publicShareRateLimiter,
      `ip:${edgeIp.slice(0, 128)}`,
    );
    return exchangeShareToken(
      services.storage,
      c.req.query('token') ?? '',
      services.shareCookieSecret,
      services.appOrigin,
      now(services),
    );
  });

  app.delete('/api/shares/:id', async (c) => {
    const actor = await requireOwner(c);
    const services = c.get('services');
    await revokeShareLink(
      services.storage,
      actor,
      parseRequiredString(c.req.param('id'), 'share link id'),
      now(services),
    );
    return c.body(null, 204);
  });

  app.get('/api/reports', async (c) => {
    const services = c.get('services');
    const readAt = now(services);
    const actor = await resolveReportActor(c);
    let reports;
    if (actor.kind === 'collaborator') {
      reports = await listReportsForCollaborator(
        services.storage,
        actor.session,
        readAt,
      );
    } else if (actor.kind === 'identity' && actor.identity.role === 'owner') {
      reports = await listReportsForOwner(
        services.storage,
        actor.identity,
        readAt,
      );
    } else {
      throw new ReportError(403, 'owner_required', 'owner access required');
    }
    c.header('cache-control', 'no-store');
    return c.json(reports, 200);
  });

  app.get('/api/reports/:id', async (c) => {
    const services = c.get('services');
    const reportId = parseRequiredString(c.req.param('id'), 'report id');
    const readAt = now(services);
    const actor = await resolveReportActor(c);
    let report;
    if (actor.kind === 'identity') {
      report = applyReportMediaPolicy(await getReportForIdentity(
        services.storage,
        actor.identity,
        reportId,
        readAt,
      ), readAt);
    } else if (actor.kind === 'collaborator') {
      report = await getReportForCollaborator(
        services.storage,
        actor.session,
        reportId,
        readAt,
      );
    } else {
      report = applyReportMediaPolicy(await getReportForPublicSession(
        services.storage,
        reportId,
        actor.session.shareLinkId,
        readAt,
      ), readAt);
    }
    c.header('cache-control', 'no-store');
    return c.json(report, 200);
  });

  app.put('/api/publisher/reports', async (c) => {
    const publisherDeviceId = await requirePublisherDevice(c);
    const body = await c.req.json<PublisherDraftBody>();
    const rawItems = Array.isArray(body.items) ? body.items : null;
    if (!rawItems || rawItems.length === 0) {
      throw new ReportError(400, 'invalid_items', 'items are required');
    }
    const services = c.get('services');
    const report = await upsertPublisherDraftReport(
      services.storage,
      {
        dailyDate: parsePublisherDate(body.dailyDate),
        items: rawItems.map((item) => parsePublisherItem(item)),
        publisherDeviceId,
        sourceTableId: parseRequiredString(body.sourceTableId, 'sourceTableId'),
        upsertedAt: now(services),
      },
    );
    c.header('cache-control', 'no-store');
    return c.json(report, 200);
  });

  app.post('/api/publisher/reports/:id/publish', async (c) => {
    const publisherDeviceId = await requirePublisherDevice(c);
    const body = await c.req.json<PublisherPublishBody>();
    const services = c.get('services');
    const report = await publishReportFromPublisher(
      services.storage,
      {
        expectedDraftVersion: parseExpectedVersion(body.expectedDraftVersion),
        publishedAt: now(services),
        publisherDeviceId,
        reportId: parseRequiredString(c.req.param('id'), 'report id'),
      },
    );
    c.header('cache-control', 'no-store');
    return c.json(report, 200);
  });

  app.patch('/api/items/:id', async (c) => {
    const services = c.get('services');
    const actor = await resolveReportActor(c);
    const body = await c.req.json<PatchItemBody>();
    const itemId = parseRequiredString(c.req.param('id'), 'item id');
    const input = {
      expectedVersion: parseExpectedVersion(body.expectedVersion),
      field: parseReportItemField(body.field),
      value: parseStringValue(body.value),
    };
    const changedAt = now(services);
    let item;
    if (actor.kind === 'identity') {
      item = await patchItem(
        services.storage,
        actor.identity,
        itemId,
        input,
        changedAt,
      );
    } else if (actor.kind === 'collaborator') {
      item = await patchItemForCollaborator(
        services.storage,
        actor.session,
        itemId,
        input,
        changedAt,
      );
    } else {
      throw new ReportError(403, 'edit_forbidden', 'report is read-only');
    }
    c.header('cache-control', 'no-store');
    return c.json(item, 200);
  });

  app.post('/api/reports/:id/publish', async (c) => {
    const identity = await requireOwner(c);
    const services = c.get('services');
    const report = await publishReport(
      services.storage,
      identity,
      parseRequiredString(c.req.param('id'), 'report id'),
      now(services),
    );
    c.header('cache-control', 'no-store');
    return c.json(report, 200);
  });

  app.post('/api/revisions/:id/restore', async (c) => {
    const identity = await requireOwner(c);
    const body = await c.req.json<RestoreRevisionBody>();
    const services = c.get('services');
    const item = await restoreRevision(
      services.storage,
      identity,
      parseRevisionId(c.req.param('id')),
      parseExpectedVersion(body.expectedVersion),
      now(services),
    );
    c.header('cache-control', 'no-store');
    return c.json(item, 200);
  });

  app.post('/api/media/uploads/cleanup', async (c) => {
    await requireOwner(c);
    const services = c.get('services');
    return c.json(await runScheduledMediaCleanup(services), 200);
  });

  app.post('/api/media/uploads', async (c) => {
    const actor = await requireMediaUploadActor(c);
    const body = await c.req.json<CreateMediaUploadBody>();
    const services = c.get('services');
    const media = requireMediaServices(services);
    deferMediaCleanup(c, services);
    const created = await createMediaUpload({
      actor,
      bucket: media.bucket,
      byteSize: parseMediaByteSize(body.byteSize),
      contentType: parseMediaString(body.contentType, 'contentType'),
      objectStore: media.objectStore,
      reportId: parseMediaString(body.reportId, 'reportId'),
      reportItemId: parseMediaString(body.reportItemId, 'reportItemId'),
      startedAt: now(services),
      storage: media.storage,
    });
    c.header('cache-control', 'no-store');
    return c.json(created.body, created.status);
  });

  app.put('/api/media/uploads/:id/parts/:partNumber', async (c) => {
    const actor = await requireMediaUploadActor(c);
    const services = c.get('services');
    const media = requireMediaServices(services);
    if (!media.bucket) {
      throw new MediaError(
        500,
        'media_service_unavailable',
        'multipart media service is not configured',
      );
    }
    const part = await uploadMediaPart({
      actor,
      body: c.req.raw.body,
      bucket: media.bucket,
      contentLength: c.req.header('content-length') ?? null,
      now: now(services),
      partNumber: parsePartNumber(c.req.param('partNumber')),
      storage: media.storage,
      uploadId: c.req.param('id'),
    });
    c.header('cache-control', 'no-store');
    return c.json(part, 200);
  });

  app.get('/api/media/uploads/:id/parts', async (c) => {
    const actor = await requireMediaUploadActor(c);
    const services = c.get('services');
    const media = requireMediaServices(services);
    const result = await listMediaUploadParts({
      actor,
      storage: media.storage,
      uploadId: c.req.param('id'),
    });
    c.header('cache-control', 'no-store');
    return c.json(result, 200);
  });

  app.post('/api/media/uploads/:id/complete', async (c) => {
    const actor = await requireMediaUploadActor(c);
    const body = await c.req.json<CompleteMediaUploadBody>();
    const services = c.get('services');
    const media = requireMediaServices(services);
    const result = await completeMediaUpload({
      actor,
      assertedSha256: parseMediaString(body.sha256, 'sha256'),
      bucket: media.bucket,
      completedAt: now(services),
      objectStore: media.objectStore,
      storage: media.storage,
      uploadId: c.req.param('id'),
    });
    c.header('cache-control', 'no-store');
    return c.json(result, 200);
  });

  app.delete('/api/media/uploads/:id', async (c) => {
    const actor = await requireMediaUploadActor(c);
    const services = c.get('services');
    const media = requireMediaServices(services);
    await abortMediaUpload({
      abortedAt: now(services),
      actor,
      bucket: media.bucket,
      objectStore: media.objectStore,
      storage: media.storage,
      uploadId: c.req.param('id'),
    });
    return c.body(null, 204);
  });

  app.post('/api/media/session', async (c) => {
    const services = c.get('services');
    const actor = await resolveReportActor(c);
    if (actor.kind === 'public') {
      throw new MediaError(
        403,
        'media_session_forbidden',
        'media session is not available',
      );
    }
    const sessionCookie = actor.kind === 'identity'
      ? await createMediaSessionCookie(
          actor.identity.id,
          services.mediaSessionSecret,
          now(services),
        )
      : await createCollaboratorMediaSessionCookie(
          actor.session,
          services.mediaSessionSecret,
          now(services),
        );
    c.header('cache-control', 'no-store');
    c.header('set-cookie', sessionCookie);
    return c.body(null, 204);
  });

  const mediaPlayback = async (c: Context<WorkerAppEnv>) => {
    const services = c.get('services');
    const media = requireMediaServices(services);
    const readAt = now(services);
    const cookieHeader = c.req.header('cookie') ?? '';
    let actorId: string | null = null;
    let collaborationSession: CollaborationSession | null = null;
    let publicSession: PublicSession | null = null;
    if (c.req.header('authorization') !== undefined) {
      actorId = (await requireIdentity(c)).id;
    } else {
      let mediaSession: MediaSessionActor | null = null;
      let mediaSessionError: MediaError | null = null;
      try {
        mediaSession = await readMediaSessionActor(
          cookieHeader,
          services.mediaSessionSecret,
          readAt,
        );
      } catch (error) {
        if (!(error instanceof MediaError)) throw error;
        if (error.code !== 'media_session_invalid') throw error;
        mediaSessionError = error;
      }

      if (mediaSession?.kind === 'identity') {
        actorId = mediaSession.actorId;
      } else if (hasCookie(cookieHeader, COLLABORATION_COOKIE)) {
        collaborationSession = await resolveCollaborationSession(
          services.storage,
          cookieHeader,
          services.shareCookieSecret,
          readAt,
        );
        if (mediaSessionError) throw mediaSessionError;
        if (
          mediaSession?.kind === 'collaborator'
          && (mediaSession.linkId !== collaborationSession.linkId
            || mediaSession.sessionVersion !== collaborationSession.sessionVersion)
        ) {
          throw new MediaError(
            400,
            'media_authorization_conflict',
            'media authorization context is invalid',
          );
        }
      } else if (hasCookie(cookieHeader, PUBLIC_COOKIE)) {
        const actor = await resolveReportActor(c);
        if (actor.kind !== 'public') {
          throw new MediaError(
            400,
            'media_authorization_conflict',
            'media authorization context is invalid',
          );
        }
        publicSession = actor.session;
      } else {
        if (mediaSessionError) throw mediaSessionError;
        if (mediaSession?.kind === 'collaborator') {
          throw new MediaError(
            400,
            'media_authorization_conflict',
            'media authorization context is invalid',
          );
        }
        const actor = await resolveReportActor(c);
        if (actor.kind === 'public') {
          publicSession = actor.session;
        } else if (actor.kind === 'collaborator') {
          collaborationSession = actor.session;
        } else {
          actorId = actor.identity.id;
        }
      }
    }
    return readMedia({
      actorId,
      bucket: media.bucket,
      collaborationSession,
      mediaId: parseMediaString(c.req.param('id'), 'media id'),
      method: c.req.method === 'HEAD' ? 'HEAD' : 'GET',
      objectStore: media.objectStore,
      publicSession,
      rangeHeader: c.req.header('range') ?? null,
      readAt,
      storage: media.storage,
    });
  };

  app.get('/api/media/:id', mediaPlayback);
  app.on('HEAD', '/api/media/:id', mediaPlayback);

  app.onError((error, c) => {
    const requestId = crypto.randomUUID();
    if (
      error instanceof InvitationError
      || error instanceof CollaborationLinkError
      || error instanceof MediaError
      || error instanceof MobileInboxError
      || error instanceof ReportError
    ) {
      if (error instanceof MobileInboxError) {
        c.header('cache-control', 'no-store');
      }
      if (error instanceof MediaError && error.headers) {
        for (const [name, value] of new Headers(error.headers)) {
          c.header(name, value);
        }
      }
      return c.json(
        { code: error.code, message: error.message, requestId },
        error.status as ContentfulStatusCode,
      );
    }
    if (error instanceof HTTPException) {
      return c.json(
        { code: 'request_denied', message: error.message, requestId },
        error.status,
      );
    }
    return c.json(
      { code: 'internal_error', message: 'request failed', requestId },
      500,
    );
  });

  return app;
}

export function createWorkerApp(services: WorkerServices): Hono<WorkerAppEnv> {
  return buildWorkerApp(services);
}

const workerApp = buildWorkerApp();

export default Object.assign(workerApp, {
  scheduled: async (
    _controller: unknown,
    env: WorkerAppEnv['Bindings'],
  ): Promise<void> => {
    await runScheduledMediaCleanup(createConfiguredServices(env));
  },
});
