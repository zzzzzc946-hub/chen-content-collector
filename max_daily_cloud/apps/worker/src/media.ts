import type { PublicSession } from './invitations.js';
import { toShanghaiDate } from './reports.js';

const MAX_MEDIA_BYTES = 5 * 1024 ** 4;
const MAX_PART_BYTES = 5 * 1024 ** 3;
const MIN_MULTIPART_PART_BYTES = 5 * 1024 ** 2;
const MAX_PART_NUMBER = 10_000;
export const SUPABASE_TUS_CHUNK_SIZE = 6 * 1024 * 1024;
const UPLOAD_LIFETIME_MS = 24 * 60 * 60 * 1000;
const RECOVERY_PAGE_SIZE = 100;
const MAX_RECOVERY_PAGES = 10_000;
const COMPLETION_RECOVERY_LEASE_MS = 5 * 60 * 1000;
const MEDIA_CACHE_CONTROL = 'private, no-store';
const MEDIA_VARY = 'Authorization, Cookie, Range';
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const ALLOWED_VIDEO_TYPES = new Set([
  'video/mp4',
  'video/quicktime',
  'video/webm',
  'video/x-m4v',
]);

export interface MediaBucketObject {
  httpMetadata?: {
    contentType?: string;
  };
  size: number;
}

export interface MediaBucketObjectBody extends MediaBucketObject {
  body: ReadableStream<Uint8Array>;
}

export interface MediaUploadedPart {
  etag: string;
  partNumber: number;
}

export interface MediaMultipartUpload {
  abort(): Promise<void>;
  complete(parts: MediaUploadedPart[]): Promise<MediaBucketObject>;
  key: string;
  uploadId: string;
  uploadPart(
    partNumber: number,
    body: ReadableStream<Uint8Array>,
  ): Promise<MediaUploadedPart>;
}

export interface MediaBucket {
  createMultipartUpload(
    key: string,
    options?: { httpMetadata?: { contentType?: string } },
  ): Promise<MediaMultipartUpload>;
  get(
    key: string,
    options?: { range?: { length: number; offset: number } },
  ): Promise<MediaBucketObjectBody | null>;
  head(key: string): Promise<MediaBucketObject | null>;
  resumeMultipartUpload(key: string, uploadId: string): MediaMultipartUpload;
}

export interface MediaObjectStore {
  createSignedUpload(input: {
    contentType: string;
    objectKey: string;
  }): Promise<{
    chunkSize: number;
    objectKey: string;
    signedUploadUrl: string;
    signedUploadToken: string;
    tusEndpoint: string;
  }>;
  deleteObject(key: string): Promise<void>;
  getObject(
    key: string,
    options?: { range?: string },
  ): Promise<{
    body: ReadableStream<Uint8Array>;
    contentType: string;
    size: number;
    status: 200 | 206;
  }>;
  headObject(key: string): Promise<{
    contentType: string;
    size: number;
  }>;
}

export interface MediaRecord {
  byteSize: number;
  contentType: string;
  id: string;
  objectKey: string;
  reportId: string;
}

export interface AuthorizedMediaRecord extends MediaRecord {
  dailyDate: string;
}

export interface ExpiredMediaObject {
  id: string;
  objectKey: string;
}

export interface MediaUploadPart {
  byteSize: number;
  etag: string;
  partNumber: number;
}

export type MediaUploadStatus =
  | 'creating'
  | 'uploading'
  | 'completing'
  | 'aborting'
  | 'completed'
  | 'aborted';

export interface MediaUploadRecord {
  abortCleanup: boolean | null;
  assertedSha256: string | null;
  contentType: string;
  createdAt: Date;
  expectedByteSize: number;
  expiresAt: Date;
  id: string;
  mediaId: string;
  objectKey: string;
  r2UploadId: string | null;
  reportId: string;
  reportItemId: string;
  sha256Source: 'trusted_uploader_assertion' | null;
  sha256VerificationStatus: 'not_server_verified' | null;
  status: MediaUploadStatus;
  transitionStartedAt: Date | null;
}

export interface MediaUploadActor {
  actorId: string | null;
  publisherDeviceId: string | null;
}

export interface PublisherDeviceIdentity {
  deviceId: string;
}

export interface PublisherDeviceAuthenticator {
  authenticate(request: Request): Promise<PublisherDeviceIdentity | null>;
}

export const disabledPublisherDeviceAuthenticator: PublisherDeviceAuthenticator = {
  async authenticate(): Promise<null> {
    return null;
  },
};

export interface MediaStorage {
  claimExpiredMediaObjects(input: {
    limit: number;
    now: Date;
  }): Promise<ExpiredMediaObject[]>;
  claimStaleMediaUploadCompletionAtomic(input: {
    claimedAt: Date;
    staleBefore: Date;
    uploadId: string;
  }): Promise<MediaUploadRecord>;
  claimMediaUploadAbortAtomic(
    input: MediaUploadActor & {
      claimedAt: Date;
      cleanup: boolean;
      uploadId: string;
    },
  ): Promise<MediaUploadRecord>;
  attachMediaUploadAtomic(
    input: MediaUploadActor & {
      attachedAt: Date;
      r2UploadId: string;
      uploadId: string;
    },
  ): Promise<MediaUploadRecord>;
  authorizeMediaReadAtomic(input: {
    actorId: string | null;
    mediaId: string;
    publicReportId: string | null;
    readAt: Date;
    shareLinkId: string | null;
  }): Promise<AuthorizedMediaRecord>;
  authorizeMediaForCollaborationAtomic?(input: {
    linkId: string;
    mediaId: string;
    readAt: Date;
  }): Promise<AuthorizedMediaRecord>;
  beginMediaUploadAtomic(
    input: MediaUploadActor & {
      contentType: string;
      expectedByteSize: number;
      reportId: string;
      reportItemId: string;
      startedAt: Date;
      uploadId: string;
    },
  ): Promise<MediaUploadRecord & { resumed: boolean }>;
  claimMediaUploadCompletionAtomic(
    input: MediaUploadActor & {
      assertedSha256: string;
      claimedAt: Date;
      uploadId: string;
    },
  ): Promise<MediaUploadRecord>;
  finalizeMediaUploadAbortAtomic(input: {
    abortedAt: Date;
    uploadId: string;
  }): Promise<MediaUploadRecord>;
  finalizeMediaUploadCompletionAtomic(
    input: {
      completedAt: Date;
      uploadId: string;
    },
  ): Promise<MediaRecord>;
  finishMediaObjectPurge(input: {
    error: string | null;
    mediaId: string;
    purgedAt: Date | null;
  }): Promise<void>;
  getMediaUploadAtomic(
    input: MediaUploadActor & {
      uploadId: string;
    },
  ): Promise<MediaUploadRecord>;
  listMediaUploadPartsAtomic(
    input: MediaUploadActor & {
      uploadId: string;
    },
  ): Promise<MediaUploadPart[]>;
  listMediaUploadRecoveryPartsAtomic(input: {
    uploadId: string;
  }): Promise<MediaUploadPart[]>;
  listMediaUploadRecoveryPage(input: {
    afterId: string | null;
    limit: number;
    staleAt: Date;
  }): Promise<MediaUploadRecord[]>;
  recordMediaUploadPartAtomic(
    input: MediaUploadActor & {
      byteSize: number;
      etag: string;
      partNumber: number;
      uploadId: string;
      uploadedAt: Date;
    },
  ): Promise<MediaUploadPart>;
  resetStaleMediaUploadCompletionAtomic(input: {
    expectedClaimedAt: Date;
    reason: 'r2_upload_missing';
    resetAt: Date;
    uploadId: string;
  }): Promise<MediaUploadRecord>;
}

export interface MediaCleanupResult {
  attempted: number;
  failed: number;
  succeeded: number;
}

export class MediaError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly headers?: HeadersInit,
  ) {
    super(message);
    this.name = 'MediaError';
  }
}

export interface ByteRange {
  end: number;
  length: number;
  offset: number;
}

export interface RangeResponseObject {
  body: ReadableStream<Uint8Array> | null;
  contentType: string;
  size: number;
}

function rangeNotSatisfiable(size: number): MediaError {
  return new MediaError(
    416,
    'range_not_satisfiable',
    'requested range is not satisfiable',
    {
      'accept-ranges': 'bytes',
      'cache-control': MEDIA_CACHE_CONTROL,
      'content-range': `bytes */${size}`,
      vary: MEDIA_VARY,
    },
  );
}

export function parseRange(value: string | null, size: number): ByteRange | null {
  if (!value) return null;
  if (!Number.isSafeInteger(size) || size <= 0) {
    throw rangeNotSatisfiable(Math.max(0, size));
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(value.trim());
  if (!match || (!match[1] && !match[2])) {
    throw rangeNotSatisfiable(size);
  }

  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) {
      throw rangeNotSatisfiable(size);
    }
    const length = Math.min(suffixLength, size);
    const offset = size - length;
    return { end: size - 1, length, offset };
  }

  const offset = Number(match[1]);
  const requestedEnd = match[2] ? Number(match[2]) : size - 1;
  if (
    !Number.isSafeInteger(offset)
    || !Number.isSafeInteger(requestedEnd)
    || offset < 0
    || requestedEnd < offset
    || offset >= size
  ) {
    throw rangeNotSatisfiable(size);
  }
  const end = Math.min(requestedEnd, size - 1);
  return { end, length: end - offset + 1, offset };
}

export function rangeResponse(
  object: RangeResponseObject,
  rangeHeader: string | null,
  method: 'GET' | 'HEAD' = 'GET',
): Response {
  const range = parseRange(rangeHeader, object.size);
  const headers = new Headers({
    'accept-ranges': 'bytes',
    'cache-control': MEDIA_CACHE_CONTROL,
    'content-length': String(range?.length ?? object.size),
    'content-type': object.contentType,
    vary: MEDIA_VARY,
  });
  if (range) {
    headers.set(
      'content-range',
      `bytes ${range.offset}-${range.end}/${object.size}`,
    );
  }
  return new Response(method === 'HEAD' ? null : object.body, {
    headers,
    status: range ? 206 : 200,
  });
}

function requireUuid(value: string, field: string): string {
  if (!UUID_PATTERN.test(value)) {
    throw new MediaError(400, 'invalid_request', `${field} must be a UUID`);
  }
  return value.toLowerCase();
}

function validateContentType(value: string): string {
  const contentType = value.trim().toLowerCase();
  if (!ALLOWED_VIDEO_TYPES.has(contentType)) {
    throw new MediaError(
      400,
      'invalid_content_type',
      'contentType must be an allowed video MIME type',
    );
  }
  return contentType;
}

function validateByteSize(value: number): number {
  if (
    !Number.isSafeInteger(value)
    || value <= 0
    || value > MAX_MEDIA_BYTES
  ) {
    throw new MediaError(
      400,
      'invalid_byte_size',
      'byteSize is outside the supported range',
    );
  }
  return value;
}

function validatePartNumber(value: number): number {
  if (
    !Number.isSafeInteger(value)
    || value < 1
    || value > MAX_PART_NUMBER
  ) {
    throw new MediaError(
      400,
      'invalid_part_number',
      'part number is outside the supported range',
    );
  }
  return value;
}

function validatePartSize(value: number, expectedByteSize: number): number {
  if (
    !Number.isSafeInteger(value)
    || value <= 0
    || value > MAX_PART_BYTES
    || value > expectedByteSize
  ) {
    throw new MediaError(
      400,
      'invalid_part_size',
      'part size is outside the supported range',
    );
  }
  return value;
}

function validateSha256(value: string): string {
  const sha256 = value.trim().toLowerCase();
  if (!SHA256_PATTERN.test(sha256)) {
    throw new MediaError(
      400,
      'invalid_sha256',
      'sha256 must be a 64-character hexadecimal digest',
    );
  }
  return sha256;
}

function validateUploadObjectKey(upload: MediaUploadRecord): void {
  const expected =
    `reports/${upload.reportId}/media/${upload.mediaId}/${upload.id}`;
  if (upload.objectKey !== expected) {
    throw new MediaError(500, 'storage_error', 'upload storage is invalid');
  }
}

function requireActiveMultipart(
  upload: MediaUploadRecord,
  activeAt?: Date,
): string {
  validateUploadObjectKey(upload);
  if (activeAt && upload.expiresAt.getTime() <= activeAt.getTime()) {
    throw new MediaError(409, 'upload_expired', 'upload has expired');
  }
  if (upload.status !== 'uploading' || !upload.r2UploadId) {
    throw new MediaError(409, 'upload_not_active', 'upload is not active');
  }
  return upload.r2UploadId;
}

function parseContentLength(value: string | null): number {
  const normalized = value?.trim() ?? '';
  if (!/^\d+$/.test(normalized)) {
    throw new MediaError(
      400,
      'invalid_part_size',
      'Content-Length must be a decimal byte count',
    );
  }
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed)) {
    throw new MediaError(
      400,
      'invalid_part_size',
      'Content-Length is outside the supported range',
    );
  }
  return parsed;
}

function isNoSuchUpload(error: unknown): boolean {
  const details = error && typeof error === 'object'
    ? error as { code?: unknown; message?: unknown; name?: unknown }
    : {};
  const code = typeof details.code === 'string' ? details.code : '';
  const name = typeof details.name === 'string' ? details.name : '';
  const message = typeof details.message === 'string'
    ? details.message.toLowerCase()
    : '';
  return (
    code === 'NoSuchUpload'
    || name === 'NoSuchUpload'
    || message.includes('no such upload')
    || message.includes('multipart upload does not exist')
  );
}

async function abortMultipartUpload(
  multipart: MediaMultipartUpload,
): Promise<void> {
  try {
    await multipart.abort();
  } catch (error) {
    if (isNoSuchUpload(error)) return;
    throw new MediaError(
      502,
      'r2_abort_failed',
      'multipart upload could not be aborted',
    );
  }
}

function publicUploadDto(
  upload: MediaUploadRecord,
  parts: MediaUploadPart[] = [],
  signedUpload?: {
    chunkSize: number;
    signedUploadUrl: string;
    signedUploadToken: string;
    tusEndpoint: string;
  },
) {
  return {
    byteSize: upload.expectedByteSize,
    contentType: upload.contentType,
    expiresAt: upload.expiresAt.toISOString(),
    mediaId: upload.mediaId,
    objectKey: upload.objectKey,
    parts,
    signedUploadToken: signedUpload?.signedUploadToken,
    signedUploadUrl: signedUpload?.signedUploadUrl,
    status: upload.status,
    tusEndpoint: signedUpload?.tusEndpoint,
    chunkSize: signedUpload?.chunkSize,
    uploadId: upload.id,
  };
}

export async function readMedia(input: {
  actorId: string | null;
  bucket?: MediaBucket;
  collaborationSession?: { linkId: string } | null;
  mediaId: string;
  method: 'GET' | 'HEAD';
  objectStore?: MediaObjectStore;
  publicSession: PublicSession | null;
  rangeHeader: string | null;
  readAt: Date;
  storage: MediaStorage;
}): Promise<Response> {
  const mediaId = requireUuid(input.mediaId, 'media id');
  const contextCount = Number(input.actorId !== null)
    + Number(input.publicSession !== null)
    + Number(Boolean(input.collaborationSession));
  if (contextCount > 1) {
    throw new MediaError(
      400,
      'media_authorization_conflict',
      'media authorization context is invalid',
    );
  }

  let media: AuthorizedMediaRecord;
  if (input.collaborationSession) {
    const authorize = input.storage.authorizeMediaForCollaborationAtomic;
    if (typeof authorize !== 'function') {
      throw new MediaError(500, 'storage_error', 'storage request failed');
    }
    media = await authorize.call(input.storage, {
      linkId: input.collaborationSession.linkId,
      mediaId,
      readAt: input.readAt,
    });
  } else {
    media = await input.storage.authorizeMediaReadAtomic({
      actorId: input.actorId,
      mediaId,
      publicReportId: input.publicSession?.reportId ?? null,
      readAt: input.readAt,
      shareLinkId: input.publicSession?.shareLinkId ?? null,
    });
  }
  if (media.dailyDate !== toShanghaiDate(input.readAt)) {
    throw new MediaError(404, 'media_not_found', 'media is unavailable');
  }

  const range = parseRange(input.rangeHeader, media.byteSize);
  if (input.objectStore) {
    if (input.method === 'HEAD') {
      const object = await input.objectStore.headObject(media.objectKey);
      if (object.size !== media.byteSize) {
        throw new MediaError(404, 'media_not_found', 'media is unavailable');
      }
      return rangeResponse(
        {
          body: null,
          contentType: media.contentType,
          size: media.byteSize,
        },
        input.rangeHeader,
        'HEAD',
      );
    }
    const object = await input.objectStore.getObject(
      media.objectKey,
      input.rangeHeader ? { range: input.rangeHeader } : undefined,
    );
    if (object.size !== media.byteSize) {
      throw new MediaError(404, 'media_not_found', 'media is unavailable');
    }
    return rangeResponse(
      {
        body: object.body,
        contentType: media.contentType,
        size: media.byteSize,
      },
      input.rangeHeader,
    );
  }
  if (!input.bucket) {
    throw new MediaError(
      500,
      'media_service_unavailable',
      'media service is not configured',
    );
  }
  if (input.method === 'HEAD') {
    const object = await input.bucket.head(media.objectKey);
    if (!object || object.size !== media.byteSize) {
      throw new MediaError(404, 'media_not_found', 'media is unavailable');
    }
    return rangeResponse(
      {
        body: null,
        contentType: media.contentType,
        size: media.byteSize,
      },
      input.rangeHeader,
      'HEAD',
    );
  }

  const object = await input.bucket.get(
    media.objectKey,
    range
      ? { range: { length: range.length, offset: range.offset } }
      : undefined,
  );
  if (!object || object.size !== media.byteSize) {
    throw new MediaError(404, 'media_not_found', 'media is unavailable');
  }
  return rangeResponse(
    {
      body: object.body,
      contentType: media.contentType,
      size: media.byteSize,
    },
    input.rangeHeader,
  );
}

async function attachCreatedMultipart(input: {
  actor: MediaUploadActor;
  attachedAt: Date;
  multipart: MediaMultipartUpload;
  storage: MediaStorage;
  upload: MediaUploadRecord;
}): Promise<MediaUploadRecord> {
  const attach = () => input.storage.attachMediaUploadAtomic({
    ...input.actor,
    attachedAt: input.attachedAt,
    r2UploadId: input.multipart.uploadId,
    uploadId: input.upload.id,
  });
  const reconcile = () => input.storage.getMediaUploadAtomic({
    ...input.actor,
    uploadId: input.upload.id,
  });

  let attachError: unknown;
  try {
    const attached = await attach();
    if (attached.r2UploadId !== input.multipart.uploadId) {
      await abortMultipartUpload(input.multipart);
    }
    return attached;
  } catch (error) {
    attachError = error;
  }

  let current: MediaUploadRecord;
  try {
    current = await reconcile();
  } catch {
    // The attach may have committed before its response was lost. Without a
    // definitive read, aborting could leave persisted resume state dangling.
    throw attachError;
  }
  if (current.r2UploadId === input.multipart.uploadId) return current;

  if (current.r2UploadId === null && current.status === 'creating') {
    try {
      current = await attach();
    } catch (retryError) {
      try {
        current = await reconcile();
      } catch {
        throw retryError;
      }
      if (current.r2UploadId !== input.multipart.uploadId) {
        await abortMultipartUpload(input.multipart);
        throw retryError;
      }
    }
  }

  if (current.r2UploadId !== input.multipart.uploadId) {
    await abortMultipartUpload(input.multipart);
  }
  return current;
}

export async function createMediaUpload(input: {
  actor: MediaUploadActor;
  bucket?: MediaBucket;
  byteSize: number;
  contentType: string;
  objectStore?: MediaObjectStore;
  reportId: string;
  reportItemId: string;
  startedAt: Date;
  storage: MediaStorage;
}): Promise<{ body: ReturnType<typeof publicUploadDto>; status: 200 | 201 }> {
  const reportId = requireUuid(input.reportId, 'reportId');
  const reportItemId = requireUuid(input.reportItemId, 'reportItemId');
  const contentType = validateContentType(input.contentType);
  const expectedByteSize = validateByteSize(input.byteSize);
  const upload = await input.storage.beginMediaUploadAtomic({
    ...input.actor,
    contentType,
    expectedByteSize,
    reportId,
    reportItemId,
    startedAt: input.startedAt,
    uploadId: crypto.randomUUID(),
  });
  validateUploadObjectKey(upload);

  let currentUpload: MediaUploadRecord = upload;
  let signedUpload: Awaited<
    ReturnType<MediaObjectStore['createSignedUpload']>
  > | undefined;
  if (input.objectStore) {
    if (!upload.r2UploadId) {
      currentUpload = await input.storage.attachMediaUploadAtomic({
        ...input.actor,
        attachedAt: input.startedAt,
        r2UploadId: `supabase:${upload.id}`,
        uploadId: upload.id,
      });
    }
    signedUpload = await input.objectStore.createSignedUpload({
      contentType,
      objectKey: upload.objectKey,
    });
    return {
      body: publicUploadDto(currentUpload, [], signedUpload),
      status: upload.resumed ? 200 : 201,
    };
  }
  if (!input.bucket) {
    throw new MediaError(
      500,
      'media_service_unavailable',
      'media service is not configured',
    );
  }
  if (!upload.r2UploadId) {
    const multipart = await input.bucket.createMultipartUpload(
      upload.objectKey,
      { httpMetadata: { contentType } },
    );
    currentUpload = await attachCreatedMultipart({
      actor: input.actor,
      attachedAt: input.startedAt,
      multipart,
      storage: input.storage,
      upload,
    });
  }

  return {
    body: publicUploadDto(currentUpload),
    status: upload.resumed ? 200 : 201,
  };
}

export async function uploadMediaPart(input: {
  actor: MediaUploadActor;
  body: ReadableStream<Uint8Array> | null;
  bucket: MediaBucket;
  contentLength: string | null;
  now: Date;
  partNumber: number;
  storage: MediaStorage;
  uploadId: string;
}): Promise<MediaUploadPart> {
  const uploadId = requireUuid(input.uploadId, 'upload id');
  const partNumber = validatePartNumber(input.partNumber);
  if (!input.body) {
    throw new MediaError(400, 'part_body_required', 'part body is required');
  }
  const upload = await input.storage.getMediaUploadAtomic({
    ...input.actor,
    uploadId,
  });
  const r2UploadId = requireActiveMultipart(upload, input.now);
  const contentLength = parseContentLength(input.contentLength);
  const byteSize = validatePartSize(contentLength, upload.expectedByteSize);
  const multipart = input.bucket.resumeMultipartUpload(
    upload.objectKey,
    r2UploadId,
  );
  const uploaded = await multipart.uploadPart(partNumber, input.body);
  return input.storage.recordMediaUploadPartAtomic({
    ...input.actor,
    byteSize,
    etag: uploaded.etag,
    partNumber: uploaded.partNumber,
    uploadId,
    uploadedAt: input.now,
  });
}

export async function listMediaUploadParts(input: {
  actor: MediaUploadActor;
  storage: MediaStorage;
  uploadId: string;
}): Promise<{ parts: MediaUploadPart[] }> {
  const uploadId = requireUuid(input.uploadId, 'upload id');
  await input.storage.getMediaUploadAtomic({ ...input.actor, uploadId });
  return {
    parts: await input.storage.listMediaUploadPartsAtomic({
      ...input.actor,
      uploadId,
    }),
  };
}

function validateCompletionParts(
  parts: MediaUploadPart[],
  expectedByteSize: number,
): MediaUploadPart[] {
  if (parts.length === 0) {
    throw new MediaError(409, 'upload_incomplete', 'upload has no parts');
  }
  const sorted = [...parts].sort(
    (left, right) => left.partNumber - right.partNumber,
  );
  let total = 0;
  let multipartPartSize: number | null = null;
  for (let index = 0; index < sorted.length; index += 1) {
    const part = sorted[index]!;
    if (part.partNumber !== index + 1) {
      throw new MediaError(
        409,
        'upload_incomplete',
        'upload parts must be contiguous',
      );
    }
    validatePartSize(part.byteSize, expectedByteSize);
    if (
      index < sorted.length - 1
      && part.byteSize < MIN_MULTIPART_PART_BYTES
    ) {
      throw new MediaError(
        409,
        'upload_incomplete',
        'non-final multipart chunks are too small',
      );
    }
    if (index < sorted.length - 1) {
      if (multipartPartSize === null) {
        multipartPartSize = part.byteSize;
      } else if (part.byteSize !== multipartPartSize) {
        throw new MediaError(
          409,
          'upload_incomplete',
          'non-final multipart chunks must use one size',
        );
      }
    } else if (
      multipartPartSize !== null
      && part.byteSize > multipartPartSize
    ) {
      throw new MediaError(
        409,
        'upload_incomplete',
        'final multipart chunk cannot exceed the other chunks',
      );
    }
    total += part.byteSize;
  }
  if (total !== expectedByteSize) {
    throw new MediaError(
      409,
      'upload_incomplete',
      'uploaded parts do not match byteSize',
    );
  }
  return sorted;
}

export async function completeMediaUpload(input: {
  actor: MediaUploadActor;
  bucket?: MediaBucket;
  completedAt: Date;
  assertedSha256: string;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
  uploadId: string;
}): Promise<{
  integrity: {
    algorithm: 'sha256';
    source: 'trusted_uploader_assertion';
    verificationStatus: 'not_server_verified';
  };
  mediaId: string;
  status: 'ready';
}> {
  const uploadId = requireUuid(input.uploadId, 'upload id');
  const assertedSha256 = validateSha256(input.assertedSha256);
  let upload = await input.storage.getMediaUploadAtomic({
    ...input.actor,
    uploadId,
  });
  validateUploadObjectKey(upload);
  if (upload.status === 'uploading') {
    requireActiveMultipart(upload, input.completedAt);
  } else if (
    upload.status === 'completing'
    && (
      !upload.r2UploadId
      || !upload.assertedSha256
    )
  ) {
    throw new MediaError(500, 'storage_error', 'upload storage is invalid');
  }

  let parts: MediaUploadPart[] = [];
  if (
    !input.objectStore
    && (upload.status === 'uploading' || upload.status === 'completing')
  ) {
    parts = validateCompletionParts(
      await input.storage.listMediaUploadPartsAtomic({
        ...input.actor,
        uploadId,
      }),
      upload.expectedByteSize,
    );
  }
  if (input.objectStore && upload.status === 'uploading') {
    const object = await input.objectStore.headObject(upload.objectKey);
    if (object.size !== upload.expectedByteSize) {
      throw new MediaError(
        502,
        'storage_complete_failed',
        'completed object size does not match upload',
      );
    }
    await input.storage.recordMediaUploadPartAtomic({
      ...input.actor,
      byteSize: upload.expectedByteSize,
      etag: 'supabase-signed-upload',
      partNumber: 1,
      uploadId,
      uploadedAt: input.completedAt,
    });
  }

  upload = await input.storage.claimMediaUploadCompletionAtomic({
    ...input.actor,
    assertedSha256,
    claimedAt: input.completedAt,
    uploadId,
  });
  if (upload.status !== 'completed') {
    if (input.objectStore) {
      const object = await input.objectStore.headObject(upload.objectKey);
      if (object.size !== upload.expectedByteSize) {
        throw new MediaError(
          502,
          'storage_complete_failed',
          'completed object size does not match upload',
        );
      }
      await input.storage.finalizeMediaUploadCompletionAtomic({
        completedAt: input.completedAt,
        uploadId,
      });
    } else {
    if (upload.status !== 'completing' || !upload.r2UploadId) {
      throw new MediaError(409, 'upload_not_active', 'upload is not active');
    }
    if (!input.bucket) {
      throw new MediaError(
        500,
        'media_service_unavailable',
        'media service is not configured',
      );
    }
    const multipart = input.bucket.resumeMultipartUpload(
      upload.objectKey,
      upload.r2UploadId,
    );

    let completed: MediaBucketObject | null = null;
    try {
      completed = await multipart.complete(parts.map(({
        etag,
        partNumber,
      }) => ({
        etag,
        partNumber,
      })));
    } catch {
      completed = await input.bucket.head(upload.objectKey);
    }
    if (!completed || completed.size !== upload.expectedByteSize) {
      throw new MediaError(
        502,
        'r2_complete_failed',
        'multipart upload could not be completed',
      );
    }

    await input.storage.finalizeMediaUploadCompletionAtomic({
      completedAt: input.completedAt,
      uploadId,
    });
    }
  }
  return {
    integrity: {
      algorithm: 'sha256',
      source: 'trusted_uploader_assertion',
      verificationStatus: 'not_server_verified',
    },
    mediaId: upload.mediaId,
    status: 'ready',
  };
}

async function completeRecoveredMediaUpload(input: {
  bucket?: MediaBucket;
  completedAt: Date;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
  upload: MediaUploadRecord;
}): Promise<void> {
  validateUploadObjectKey(input.upload);
  if (
    input.upload.status !== 'completing'
    || !input.upload.r2UploadId
    || !input.upload.assertedSha256
  ) {
    throw new MediaError(409, 'upload_not_active', 'upload is not completing');
  }
  let object = input.objectStore
    ? await input.objectStore.headObject(input.upload.objectKey).catch(() => null)
    : await input.bucket?.head(input.upload.objectKey);
  if (object) {
    if (object.size !== input.upload.expectedByteSize) {
      throw new MediaError(
        502,
        'r2_complete_failed',
        'completed object size does not match upload',
      );
    }
    await input.storage.finalizeMediaUploadCompletionAtomic({
      completedAt: input.completedAt,
      uploadId: input.upload.id,
    });
    return;
  }

  const staleBefore = new Date(
    input.completedAt.getTime() - COMPLETION_RECOVERY_LEASE_MS,
  );
  if (
    !input.upload.transitionStartedAt
    || input.upload.transitionStartedAt.getTime() > staleBefore.getTime()
  ) {
    return;
  }

  const upload = await input.storage.claimStaleMediaUploadCompletionAtomic({
    claimedAt: input.completedAt,
    staleBefore,
    uploadId: input.upload.id,
  });
  validateUploadObjectKey(upload);
  if (input.objectStore) {
    object = await input.objectStore.headObject(upload.objectKey).catch(() => null);
    if (!object) return;
    if (object.size !== upload.expectedByteSize) {
      throw new MediaError(
        502,
        'storage_complete_failed',
        'completed object size does not match upload',
      );
    }
    await input.storage.finalizeMediaUploadCompletionAtomic({
      completedAt: input.completedAt,
      uploadId: upload.id,
    });
    return;
  }
  if (
    upload.status !== 'completing'
    || !upload.r2UploadId
    || !upload.assertedSha256
  ) {
    throw new MediaError(409, 'upload_not_active', 'upload is not completing');
  }
  if (!input.bucket) {
    throw new MediaError(
      500,
      'media_service_unavailable',
      'media service is not configured',
    );
  }
  const parts = validateCompletionParts(
    await input.storage.listMediaUploadRecoveryPartsAtomic({
      uploadId: upload.id,
    }),
    upload.expectedByteSize,
  );
  const multipart = input.bucket.resumeMultipartUpload(
    upload.objectKey,
    upload.r2UploadId,
  );
  try {
    object = await multipart.complete(parts.map(({ etag, partNumber }) => ({
      etag,
      partNumber,
    })));
  } catch (error) {
    object = await input.bucket.head(upload.objectKey);
    if (!object && isNoSuchUpload(error)) {
      if (!upload.transitionStartedAt) {
        throw new MediaError(500, 'storage_error', 'upload lease is invalid');
      }
      await input.storage.resetStaleMediaUploadCompletionAtomic({
        expectedClaimedAt: upload.transitionStartedAt,
        reason: 'r2_upload_missing',
        resetAt: input.completedAt,
        uploadId: upload.id,
      });
      return;
    }
    if (!object) throw error;
  }
  if (object.size !== upload.expectedByteSize) {
    throw new MediaError(
      502,
      'r2_complete_failed',
      'completed object size does not match upload',
    );
  }
  await input.storage.finalizeMediaUploadCompletionAtomic({
    completedAt: input.completedAt,
    uploadId: upload.id,
  });
}

async function abortClaimedMediaUpload(input: {
  abortedAt: Date;
  bucket?: MediaBucket;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
  upload: MediaUploadRecord;
}): Promise<void> {
  validateUploadObjectKey(input.upload);
  if (input.upload.status === 'aborted') return;
  if (input.upload.status !== 'aborting') {
    throw new MediaError(409, 'upload_not_active', 'upload is not aborting');
  }
  if (input.objectStore) {
    await input.objectStore.deleteObject(input.upload.objectKey);
  } else if (input.upload.r2UploadId) {
    if (!input.bucket) {
      throw new MediaError(
        500,
        'media_service_unavailable',
        'media service is not configured',
      );
    }
    await abortMultipartUpload(
      input.bucket.resumeMultipartUpload(
        input.upload.objectKey,
        input.upload.r2UploadId,
      ),
    );
  }
  await input.storage.finalizeMediaUploadAbortAtomic({
    abortedAt: input.abortedAt,
    uploadId: input.upload.id,
  });
}

export async function abortMediaUpload(input: {
  actor: MediaUploadActor;
  abortedAt: Date;
  bucket?: MediaBucket;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
  uploadId: string;
}): Promise<void> {
  const uploadId = requireUuid(input.uploadId, 'upload id');
  const upload = await input.storage.claimMediaUploadAbortAtomic({
    ...input.actor,
    claimedAt: input.abortedAt,
    cleanup: false,
    uploadId,
  });
  await abortClaimedMediaUpload({
    abortedAt: input.abortedAt,
    bucket: input.bucket,
    objectStore: input.objectStore,
    storage: input.storage,
    upload,
  });
}

async function recoverMediaUpload(input: {
  bucket?: MediaBucket;
  now: Date;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
  upload: MediaUploadRecord;
}): Promise<void> {
  if (input.upload.status === 'completing') {
    await completeRecoveredMediaUpload({
      bucket: input.bucket,
      completedAt: input.now,
      objectStore: input.objectStore,
      storage: input.storage,
      upload: input.upload,
    });
    return;
  }
  const claimed = await input.storage.claimMediaUploadAbortAtomic({
    actorId: null,
    claimedAt: input.now,
    cleanup: true,
    publisherDeviceId: null,
    uploadId: input.upload.id,
  });
  await abortClaimedMediaUpload({
    abortedAt: input.now,
    bucket: input.bucket,
    objectStore: input.objectStore,
    storage: input.storage,
    upload: claimed,
  });
}

export async function cleanupStaleMediaUploads(input: {
  bucket?: MediaBucket;
  now: Date;
  objectStore?: MediaObjectStore;
  storage: MediaStorage;
}): Promise<MediaCleanupResult> {
  const result: MediaCleanupResult = {
    attempted: 0,
    failed: 0,
    succeeded: 0,
  };
  let afterId: string | null = null;

  for (let page = 0; page < MAX_RECOVERY_PAGES; page += 1) {
    const uploads = await input.storage.listMediaUploadRecoveryPage({
      afterId,
      limit: RECOVERY_PAGE_SIZE,
      staleAt: input.now,
    });
    if (uploads.length === 0) break;

    for (const upload of uploads) {
      result.attempted += 1;
      try {
        await recoverMediaUpload({
          bucket: input.bucket,
          now: input.now,
          objectStore: input.objectStore,
          storage: input.storage,
          upload,
        });
        result.succeeded += 1;
      } catch {
        result.failed += 1;
      }
    }

    const nextAfterId = uploads.at(-1)?.id ?? null;
    if (!nextAfterId || nextAfterId === afterId) break;
    afterId = nextAfterId;
    if (uploads.length < RECOVERY_PAGE_SIZE) break;
  }
  return result;
}

export async function purgeExpiredMediaObjects(input: {
  now: Date;
  objectStore: MediaObjectStore;
  storage: MediaStorage;
}): Promise<void> {
  const expired = await input.storage.claimExpiredMediaObjects({
    limit: 100,
    now: input.now,
  });
  for (const media of expired) {
    try {
      await input.objectStore.deleteObject(media.objectKey);
    } catch {
      await input.storage.finishMediaObjectPurge({
        error: 'storage_delete_failed',
        mediaId: media.id,
        purgedAt: null,
      });
      continue;
    }
    await input.storage.finishMediaObjectPurge({
      error: null,
      mediaId: media.id,
      purgedAt: input.now,
    });
  }
}

export function uploadExpiresAt(startedAt: Date): Date {
  return new Date(startedAt.getTime() + UPLOAD_LIFETIME_MS);
}
