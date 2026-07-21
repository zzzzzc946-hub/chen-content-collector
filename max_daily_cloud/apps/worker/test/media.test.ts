import { describe, expect, it, vi } from 'vitest';
import type {
  IdentityRole,
  IdentityRoleStore,
  IdentityVerifier,
  VerifiedIdentity,
} from '../src/auth';
import type { WorkerServices } from '../src/env';
import { createWorkerApp, runScheduledMediaCleanup } from '../src/index';
import { createPublisherDeviceAuthenticator } from '../src/publisher-auth';
import {
  createShareLink,
  exchangeShareToken,
  type InvitationRecord,
  type InvitationStorage,
  type MemberRole,
  type ShareLinkRecord,
  type StoredShareLink,
} from '../src/invitations';
import { readMedia, type MediaObjectStore } from '../src/media';
import {
  ReportError,
  type PublishReportInput,
  type ReadReportInput,
  type ReportRecord,
  type ReportStorage,
  type RestoreRevisionInput,
  type UpdateCollaborativeFieldInput,
} from '../src/reports';
import { SupabaseRestStorage } from '../src/storage';

const OWNER_ID = '00000000-0000-0000-0000-000000000001';
const EDITOR_ID = '00000000-0000-0000-0000-000000000002';
const VIEWER_ID = '00000000-0000-0000-0000-000000000003';
const REPORT_ID = '00000000-0000-0000-0000-000000000101';
const OTHER_REPORT_ID = '00000000-0000-0000-0000-000000000102';
const ITEM_ID = '00000000-0000-0000-0000-000000000201';
const DRAFT_ITEM_ID = '00000000-0000-0000-0000-000000000202';
const OTHER_ITEM_ID = '00000000-0000-0000-0000-000000000203';
const MEDIA_ID = '00000000-0000-0000-0000-000000000301';
const DRAFT_MEDIA_ID = '00000000-0000-0000-0000-000000000302';
const OTHER_MEDIA_ID = '00000000-0000-0000-0000-000000000303';
const UNKNOWN_MEDIA_ID = '00000000-0000-0000-0000-000000000399';
const NOW = new Date('2026-07-10T09:00:00.000Z');
const SESSION_SECRET = 'test-only-public-session-secret-with-32-bytes';
const MEDIA_SESSION_SECRET = 'test-only-media-session-secret-with-32-bytes';
const PUBLISHER_TOKEN = 'publisher-token-with-at-least-32-characters';
const PUBLISHER_TOKEN_PEPPER = 'publisher-pepper-with-at-least-32-characters';
const PUBLISHER_DEVICE_ID = '00000000-0000-0000-0000-000000000901';
const APP_ORIGIN = 'https://daily.example.com';

interface TestMediaRecord {
  byteSize: number;
  contentType: string;
  id: string;
  objectKey: string;
  reportId: string;
}

interface TestExpiredMediaObject {
  id: string;
  objectKey: string;
}

interface TestUploadPart {
  byteSize: number;
  etag: string;
  partNumber: number;
}

interface TestUploadRecord {
  abortCleanup: boolean | null;
  assertedSha256: string | null;
  contentType: string;
  createdAt: Date;
  expectedByteSize: number;
  expiresAt: Date;
  id: string;
  mediaId: string;
  objectKey: string;
  parts: TestUploadPart[];
  publisherDeviceId: string | null;
  r2UploadId: string | null;
  reportId: string;
  reportItemId: string;
  sha256Source: 'trusted_uploader_assertion' | null;
  sha256VerificationStatus: 'not_server_verified' | null;
  status:
    | 'creating'
    | 'uploading'
    | 'completing'
    | 'aborting'
    | 'completed'
    | 'aborted';
  transitionStartedAt: Date | null;
}

interface TestUploadActor {
  actorId: string | null;
  publisherDeviceId: string | null;
}

class MemoryMediaStorage
implements InvitationStorage, IdentityRoleStore, ReportStorage {
  readonly media = new Map<string, TestMediaRecord>([
    [MEDIA_ID, {
      byteSize: 100,
      contentType: 'video/mp4',
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
      reportId: REPORT_ID,
    }],
    [DRAFT_MEDIA_ID, {
      byteSize: 50,
      contentType: 'video/webm',
      id: DRAFT_MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/draft.webm`,
      reportId: REPORT_ID,
    }],
    [OTHER_MEDIA_ID, {
      byteSize: 12,
      contentType: 'video/mp4',
      id: OTHER_MEDIA_ID,
      objectKey: `reports/${OTHER_REPORT_ID}/other.mp4`,
      reportId: OTHER_REPORT_ID,
    }],
  ]);
  readonly publishedMedia = new Map<string, Set<string>>([
    [REPORT_ID, new Set([MEDIA_ID])],
    [OTHER_REPORT_ID, new Set([OTHER_MEDIA_ID])],
  ]);
  readonly reportItems = new Map<string, { mediaId: string; reportId: string }>([
    [ITEM_ID, { mediaId: MEDIA_ID, reportId: REPORT_ID }],
    [DRAFT_ITEM_ID, { mediaId: DRAFT_MEDIA_ID, reportId: REPORT_ID }],
    [OTHER_ITEM_ID, { mediaId: OTHER_MEDIA_ID, reportId: OTHER_REPORT_ID }],
  ]);
  readonly roles = new Map<string, IdentityRole>([
    [OWNER_ID, 'owner'],
    [EDITOR_ID, 'editor'],
    [VIEWER_ID, 'viewer'],
  ]);
  readonly members = new Map<string, MemberRole>([
    [`${REPORT_ID}:${EDITOR_ID}`, 'editor'],
    [`${REPORT_ID}:${VIEWER_ID}`, 'viewer'],
  ]);
  readonly reports = new Map<string, 'draft' | 'published' | 'withdrawn'>([
    [REPORT_ID, 'published'],
    [OTHER_REPORT_ID, 'published'],
  ]);
  readonly reportDailyDates = new Map<string, string>([
    [REPORT_ID, '2026-07-10'],
    [OTHER_REPORT_ID, '2026-07-09'],
  ]);
  abortFinalizeFailuresRemaining = 0;
  attachCommitThenFailRemaining = 0;
  attachFailuresRemaining = 0;
  completeFinalizeFailuresRemaining = 0;
  listFailuresRemaining = 0;
  recoveryPageCalls = 0;
  recoveryPageSize = 100;
  recoveryWait: Promise<void> | null = null;
  recoveryWaitError: Error | null = null;
  readonly expiredMedia = new Map<string, TestExpiredMediaObject>();
  readonly mediaPurgeResults = new Map<string, {
    error: string | null;
    purgedAt: Date | null;
  }>();
  readonly mediaPurgeAttemptedAt = new Map<string, Date>();
  readonly mediaPurgeClaims: Array<{ limit: number; now: Date }> = [];
  finishMediaPurgeFailuresRemaining = 0;
  readonly shares: StoredShareLink[] = [];
  readonly uploads = new Map<string, TestUploadRecord>();
  publisherDeviceHash: string | null = null;
  publisherDeviceLastUsedAt: Date | null = null;

  async getIdentityRole(
    userId: string,
  ): Promise<IdentityRole | null> {
    return this.roles.get(userId) ?? null;
  }

  async claimOwner(): Promise<'owner'> {
    return 'owner';
  }

  async authenticatePublisherDeviceAtomic(input: {
    tokenHash: string;
    usedAt: Date;
  }): Promise<{ deviceId: string } | null> {
    if (input.tokenHash !== this.publisherDeviceHash) return null;
    this.publisherDeviceLastUsedAt = input.usedAt;
    return { deviceId: PUBLISHER_DEVICE_ID };
  }

  async claimExpiredMediaObjects(input: {
    limit: number;
    now: Date;
  }): Promise<TestExpiredMediaObject[]> {
    this.mediaPurgeClaims.push(structuredClone(input));
    return [...this.expiredMedia.values()]
      .filter(({ id }) => {
        const attemptedAt = this.mediaPurgeAttemptedAt.get(id);
        return (
          !this.mediaPurgeResults.get(id)?.purgedAt
          && (!attemptedAt
            || attemptedAt.getTime() <= input.now.getTime() - 60 * 60 * 1000)
        );
      })
      .slice(0, input.limit)
      .map((media) => {
        this.mediaPurgeAttemptedAt.set(media.id, input.now);
        return structuredClone(media);
      });
  }

  async finishMediaObjectPurge(input: {
    error: string | null;
    mediaId: string;
    purgedAt: Date | null;
  }): Promise<void> {
    if (this.finishMediaPurgeFailuresRemaining > 0) {
      this.finishMediaPurgeFailuresRemaining -= 1;
      throw new Error('purge persistence failed');
    }
    this.mediaPurgeResults.set(input.mediaId, {
      error: input.error,
      purgedAt: input.purgedAt,
    });
  }

  async authorizeMediaReadAtomic(input: {
    actorId: string | null;
    mediaId: string;
    publicReportId: string | null;
    readAt: Date;
    shareLinkId: string | null;
  }): Promise<TestMediaRecord & { dailyDate: string }> {
    const record = this.media.get(input.mediaId);
    if (input.actorId) {
      const role = this.roles.get(input.actorId) === 'owner'
        ? 'owner'
        : record
        ? this.members.get(`${record.reportId}:${input.actorId}`) ?? null
        : null;
      const canRead = (
        record
        && (
          role === 'owner'
          || role === 'editor'
          || (
            role === 'viewer'
            && this.reports.get(record.reportId) === 'published'
            && this.publishedMedia.get(record.reportId)?.has(record.id)
          )
        )
      );
      if (!canRead) {
        throw new ReportError(404, 'media_not_found', 'media is unavailable');
      }
      return {
        ...structuredClone(record),
        dailyDate: this.reportDailyDates.get(record.reportId) ?? '',
      };
    }

    const share = this.shares.find(({ id }) => id === input.shareLinkId);
    if (
      !share
      || share.revokedAt
      || (share.expiresAt && share.expiresAt.getTime() <= input.readAt.getTime())
    ) {
      throw new ReportError(
        410,
        'share_unavailable',
        'share is expired or revoked',
      );
    }
    if (
      !record
      || share.reportId !== input.publicReportId
      || record.reportId !== share.reportId
      || this.reports.get(record.reportId) !== 'published'
      || !this.publishedMedia.get(record.reportId)?.has(record.id)
    ) {
      throw new ReportError(404, 'media_not_found', 'media is unavailable');
    }
    return {
      ...structuredClone(record),
      dailyDate: this.reportDailyDates.get(record.reportId) ?? '',
    };
  }

  async authorizeMediaForCollaborationAtomic(input: {
    linkId: string;
    mediaId: string;
    readAt: Date;
  }): Promise<TestMediaRecord & { dailyDate: string }> {
    const record = this.media.get(input.mediaId);
    if (!record || !input.linkId) {
      throw new ReportError(404, 'media_not_found', 'media is unavailable');
    }
    return {
      ...structuredClone(record),
      dailyDate: this.reportDailyDates.get(record.reportId) ?? '',
    };
  }

  async beginMediaUploadAtomic(input: TestUploadActor & {
    contentType: string;
    expectedByteSize: number;
    reportId: string;
    reportItemId: string;
    startedAt: Date;
    uploadId: string;
  }): Promise<TestUploadRecord & { resumed: boolean }> {
    this.requireOwner(input);
    const reportItem = this.reportItems.get(input.reportItemId);
    if (!reportItem || reportItem.reportId !== input.reportId) {
      throw new ReportError(404, 'report_item_not_found', 'report item is unavailable');
    }
    const existing = [...this.uploads.values()].find((upload) =>
      upload.reportItemId === input.reportItemId
      && upload.status !== 'aborted'
      && upload.status !== 'completed'
      && upload.expiresAt.getTime() > input.startedAt.getTime()
    );
    if (existing) {
      if (
        existing.contentType !== input.contentType
        || existing.expectedByteSize !== input.expectedByteSize
      ) {
        throw new ReportError(409, 'upload_in_progress', 'another upload is active');
      }
      return { ...structuredClone(existing), resumed: true };
    }

    const mediaId = reportItem.mediaId;
    const objectKey =
      `reports/${input.reportId}/media/${mediaId}/${input.uploadId}`;
    const upload: TestUploadRecord = {
      abortCleanup: null,
      assertedSha256: null,
      contentType: input.contentType,
      createdAt: input.startedAt,
      expectedByteSize: input.expectedByteSize,
      expiresAt: new Date(input.startedAt.getTime() + 24 * 60 * 60 * 1000),
      id: input.uploadId,
      mediaId,
      objectKey,
      parts: [],
      publisherDeviceId: input.publisherDeviceId,
      r2UploadId: null,
      reportId: input.reportId,
      reportItemId: input.reportItemId,
      sha256Source: null,
      sha256VerificationStatus: null,
      status: 'creating',
      transitionStartedAt: null,
    };
    this.uploads.set(upload.id, upload);
    return { ...structuredClone(upload), resumed: false };
  }

  async attachMediaUploadAtomic(input: TestUploadActor & {
    attachedAt: Date;
    r2UploadId: string;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    const upload = this.authorizedUpload(input.uploadId, input);
    if (this.attachFailuresRemaining > 0) {
      this.attachFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    if (!upload.r2UploadId) {
      upload.r2UploadId = input.r2UploadId;
      upload.status = 'uploading';
    }
    if (this.attachCommitThenFailRemaining > 0) {
      this.attachCommitThenFailRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    return structuredClone(upload);
  }

  async getMediaUploadAtomic(input: TestUploadActor & {
    uploadId: string;
  }): Promise<TestUploadRecord> {
    return structuredClone(this.authorizedUpload(input.uploadId, input));
  }

  async recordMediaUploadPartAtomic(input: TestUploadActor & {
    byteSize: number;
    etag: string;
    partNumber: number;
    uploadId: string;
    uploadedAt: Date;
  }): Promise<TestUploadPart> {
    const upload = this.authorizedUpload(input.uploadId, input);
    if (upload.status !== 'uploading') {
      throw new ReportError(409, 'upload_not_active', 'upload is not active');
    }
    const part = {
      byteSize: input.byteSize,
      etag: input.etag,
      partNumber: input.partNumber,
    };
    upload.parts = [
      ...upload.parts.filter(({ partNumber }) => partNumber !== input.partNumber),
      part,
    ].sort((left, right) => left.partNumber - right.partNumber);
    return structuredClone(part);
  }

  async listMediaUploadPartsAtomic(input: TestUploadActor & {
    uploadId: string;
  }): Promise<TestUploadPart[]> {
    return structuredClone(this.authorizedUpload(input.uploadId, input).parts);
  }

  async listMediaUploadRecoveryPartsAtomic(input: {
    uploadId: string;
  }): Promise<TestUploadPart[]> {
    const upload = this.uploads.get(input.uploadId);
    if (!upload || upload.status !== 'completing') {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    return structuredClone(upload.parts);
  }

  async completeMediaUploadAtomic(input: TestUploadActor & {
    completedAt: Date;
    sha256: string;
    uploadId: string;
  }): Promise<TestMediaRecord> {
    if (this.completeFinalizeFailuresRemaining > 0) {
      this.completeFinalizeFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    const upload = this.authorizedUpload(input.uploadId, input);
    upload.status = 'completed';
    const record = {
      byteSize: upload.expectedByteSize,
      contentType: upload.contentType,
      id: upload.mediaId,
      objectKey: upload.objectKey,
      reportId: upload.reportId,
    };
    this.media.set(record.id, record);
    return structuredClone(record);
  }

  async abortMediaUploadAtomic(input: TestUploadActor & {
    abortedAt: Date;
    cleanup: boolean;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    if (this.abortFinalizeFailuresRemaining > 0) {
      this.abortFinalizeFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    const upload = input.cleanup
      ? this.uploads.get(input.uploadId)
      : this.authorizedUpload(input.uploadId, input);
    if (!upload) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    upload.status = 'aborted';
    return structuredClone(upload);
  }

  async claimMediaUploadCompletionAtomic(input: TestUploadActor & {
    assertedSha256: string;
    claimedAt: Date;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    const upload = this.authorizedUpload(input.uploadId, input);
    if (upload.status === 'completed' || upload.status === 'completing') {
      if (upload.assertedSha256 !== input.assertedSha256) {
        throw new ReportError(
          409,
          'integrity_assertion_mismatch',
          'integrity assertion does not match',
        );
      }
      return structuredClone(upload);
    }
    if (upload.status === 'aborting' || upload.status === 'aborted') {
      throw new ReportError(409, 'upload_aborting', 'upload is aborting');
    }
    if (upload.status !== 'uploading') {
      throw new ReportError(409, 'upload_not_active', 'upload is not active');
    }
    upload.assertedSha256 = input.assertedSha256;
    upload.sha256Source = 'trusted_uploader_assertion';
    upload.sha256VerificationStatus = 'not_server_verified';
    upload.status = 'completing';
    upload.transitionStartedAt = input.claimedAt;
    return structuredClone(upload);
  }

  async claimStaleMediaUploadCompletionAtomic(input: {
    claimedAt: Date;
    staleBefore: Date;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    const upload = this.uploads.get(input.uploadId);
    if (
      !upload
      || upload.status !== 'completing'
      || !upload.transitionStartedAt
      || upload.transitionStartedAt.getTime() > input.staleBefore.getTime()
    ) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    upload.transitionStartedAt = input.claimedAt;
    return structuredClone(upload);
  }

  async finalizeMediaUploadCompletionAtomic(input: {
    completedAt: Date;
    uploadId: string;
  }): Promise<TestMediaRecord> {
    if (this.completeFinalizeFailuresRemaining > 0) {
      this.completeFinalizeFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    const upload = this.uploads.get(input.uploadId);
    if (!upload) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    if (upload.status === 'completed') {
      const current = this.media.get(upload.mediaId);
      if (!current) throw new Error('missing completed media');
      return structuredClone(current);
    }
    if (upload.status !== 'completing') {
      throw new ReportError(409, 'upload_not_active', 'upload is not completing');
    }
    upload.status = 'completed';
    upload.transitionStartedAt = null;
    const record = {
      byteSize: upload.expectedByteSize,
      contentType: upload.contentType,
      id: upload.mediaId,
      objectKey: upload.objectKey,
      reportId: upload.reportId,
    };
    this.media.set(record.id, record);
    return structuredClone(record);
  }

  async claimMediaUploadAbortAtomic(input: TestUploadActor & {
    claimedAt: Date;
    cleanup: boolean;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    const upload = input.cleanup
      ? this.uploads.get(input.uploadId)
      : this.authorizedUpload(input.uploadId, input);
    if (!upload) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    if (upload.status === 'completed' || upload.status === 'completing') {
      throw new ReportError(409, 'upload_completing', 'upload is completing');
    }
    if (upload.status === 'aborted' || upload.status === 'aborting') {
      return structuredClone(upload);
    }
    if (
      input.cleanup
      && upload.expiresAt.getTime() > input.claimedAt.getTime()
    ) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    upload.abortCleanup = input.cleanup;
    upload.status = 'aborting';
    upload.transitionStartedAt = input.claimedAt;
    return structuredClone(upload);
  }

  async finalizeMediaUploadAbortAtomic(input: {
    abortedAt: Date;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    if (this.abortFinalizeFailuresRemaining > 0) {
      this.abortFinalizeFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    const upload = this.uploads.get(input.uploadId);
    if (!upload) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    if (upload.status === 'aborted') return structuredClone(upload);
    if (upload.status !== 'aborting') {
      throw new ReportError(409, 'upload_not_active', 'upload is not aborting');
    }
    upload.status = 'aborted';
    upload.transitionStartedAt = null;
    return structuredClone(upload);
  }

  async listStaleMediaUploads(input: {
    staleAt: Date;
  }): Promise<TestUploadRecord[]> {
    if (this.listFailuresRemaining > 0) {
      this.listFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    this.recoveryPageCalls += 1;
    return [...this.uploads.values()]
      .filter((upload) =>
        upload.status !== 'completed'
        && upload.status !== 'aborted'
        && upload.expiresAt.getTime() <= input.staleAt.getTime()
      )
      .sort((left, right) => left.id.localeCompare(right.id))
      .slice(0, this.recoveryPageSize)
      .map((upload) => structuredClone(upload));
  }

  async listMediaUploadRecoveryPage(input: {
    afterId: string | null;
    limit: number;
    staleAt: Date;
  }): Promise<TestUploadRecord[]> {
    if (this.recoveryWait) {
      await this.recoveryWait;
    }
    if (this.recoveryWaitError) {
      throw this.recoveryWaitError;
    }
    if (this.listFailuresRemaining > 0) {
      this.listFailuresRemaining -= 1;
      throw new ReportError(500, 'storage_error', 'storage request failed');
    }
    this.recoveryPageCalls += 1;
    const limit = Math.min(input.limit, this.recoveryPageSize);
    return [...this.uploads.values()]
      .filter((upload) =>
        (
          (upload.status === 'creating' || upload.status === 'uploading')
          && upload.expiresAt.getTime() <= input.staleAt.getTime()
        )
        || upload.status === 'aborting'
        || upload.status === 'completing'
      )
      .filter((upload) => input.afterId === null || upload.id > input.afterId)
      .sort((left, right) => left.id.localeCompare(right.id))
      .slice(0, limit)
      .map((upload) => structuredClone(upload));
  }

  async resetStaleMediaUploadCompletionAtomic(input: {
    expectedClaimedAt: Date;
    reason: 'r2_upload_missing';
    resetAt: Date;
    uploadId: string;
  }): Promise<TestUploadRecord> {
    const upload = this.uploads.get(input.uploadId);
    if (
      !upload
      || upload.status !== 'completing'
      || upload.transitionStartedAt?.getTime()
        !== input.expectedClaimedAt.getTime()
    ) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    upload.status = 'aborted';
    upload.transitionStartedAt = null;
    return structuredClone(upload);
  }

  async insertShareLink(record: StoredShareLink): Promise<void> {
    this.shares.push(structuredClone(record));
  }

  async findShareLinkByTokenHash(
    tokenHash: string,
  ): Promise<ShareLinkRecord | null> {
    return this.liveShare(this.shares.find((share) => share.tokenHash === tokenHash));
  }

  async findShareLinkById(id: string): Promise<ShareLinkRecord | null> {
    return this.liveShare(this.shares.find((share) => share.id === id));
  }

  async claimInvitationAtomic(): Promise<{ reportId: string; role: MemberRole }> {
    throw new Error('not used');
  }

  async insertInvitation(_record: InvitationRecord): Promise<void> {
    throw new Error('not used');
  }

  async revokeInvitation(): Promise<boolean> {
    return false;
  }

  async revokeShareLink(): Promise<boolean> {
    return false;
  }

  async publishReportAtomic(_input: PublishReportInput): Promise<ReportRecord> {
    throw new Error('not used');
  }

  async readReportAtomic(_input: ReadReportInput): Promise<ReportRecord> {
    throw new Error('not used');
  }

  async restoreRevisionAtomic(_input: RestoreRevisionInput) {
    throw new Error('not used');
  }

  async updateCollaborativeFieldAtomic(
    _input: UpdateCollaborativeFieldInput,
  ) {
    throw new Error('not used');
  }

  private authorizedUpload(
    uploadId: string,
    actor: TestUploadActor,
  ): TestUploadRecord {
    this.requireOwner(actor);
    const upload = this.uploads.get(uploadId);
    if (!upload) {
      throw new ReportError(404, 'upload_not_found', 'upload is unavailable');
    }
    return upload;
  }

  private liveShare(
    share: StoredShareLink | undefined,
  ): ShareLinkRecord | null {
    if (!share) return null;
    return {
      ...structuredClone(share),
      reportStatus: this.reports.get(share.reportId) ?? 'draft',
    };
  }

  private requireOwner(actor: TestUploadActor): void {
    if (actor.actorId && this.roles.get(actor.actorId) === 'owner') return;
    if (actor.publisherDeviceId === PUBLISHER_DEVICE_ID) return;
    throw new ReportError(403, 'owner_required', 'owner access required');
  }
}

class TestIdentityVerifier implements IdentityVerifier {
  private readonly identities = new Map<string, VerifiedIdentity>([
    ['owner-token', { email: 'chen@example.com', id: OWNER_ID }],
    ['editor-token', { email: 'max@example.com', id: EDITOR_ID }],
    ['viewer-token', { email: 'viewer@example.com', id: VIEWER_ID }],
  ]);

  async verify(request: Request): Promise<VerifiedIdentity | null> {
    const authorization = request.headers.get('authorization');
    const token = authorization?.startsWith('Bearer ')
      ? authorization.slice(7)
      : '';
    return this.identities.get(token) ?? null;
  }
}

class MemoryRateLimiter {
  async limit(): Promise<{ success: boolean }> {
    return { success: true };
  }
}

async function readBodyBytes(body: unknown): Promise<Uint8Array> {
  if (body instanceof ReadableStream) {
    return new Uint8Array(await new Response(body).arrayBuffer());
  }
  if (body instanceof ArrayBuffer) return new Uint8Array(body);
  if (ArrayBuffer.isView(body)) {
    return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
  }
  if (typeof body === 'string') return new TextEncoder().encode(body);
  throw new Error('unsupported test body');
}

class FakeR2Bucket {
  readonly abortCalls: string[] = [];
  readonly abortFailureUploadIds = new Set<string>();
  abortFailuresRemaining = 0;
  readonly completeCalls: string[] = [];
  completeResponseFailuresRemaining = 0;
  readonly createCalls: string[] = [];
  createFailuresRemaining = 0;
  readonly getCalls: Array<{
    key: string;
    range?: { length: number; offset: number };
  }> = [];
  readonly objects = new Map<string, {
    bytes: Uint8Array;
    contentType: string;
  }>();
  readonly uploads = new Map<string, {
    aborted: boolean;
    completed: boolean;
    contentType: string;
    key: string;
    parts: Map<number, { bytes: Uint8Array; etag: string }>;
  }>();
  private nextUpload = 1;

  async createMultipartUpload(
    key: string,
    options: { httpMetadata?: { contentType?: string } } = {},
  ) {
    if (this.createFailuresRemaining > 0) {
      this.createFailuresRemaining -= 1;
      throw new Error('temporary R2 create failure');
    }
    const uploadId = `r2-upload-${this.nextUpload++}`;
    this.createCalls.push(key);
    this.uploads.set(uploadId, {
      aborted: false,
      completed: false,
      contentType: options.httpMetadata?.contentType ?? 'application/octet-stream',
      key,
      parts: new Map(),
    });
    return this.multipart(key, uploadId);
  }

  resumeMultipartUpload(key: string, uploadId: string) {
    return this.multipart(key, uploadId);
  }

  async get(
    key: string,
    options?: { range?: { length: number; offset: number } },
  ) {
    this.getCalls.push({ key, range: options?.range });
    const object = this.objects.get(key);
    if (!object) return null;
    const bytes = options?.range
      ? object.bytes.slice(
        options.range.offset,
        options.range.offset + options.range.length,
      )
      : object.bytes;
    return {
      body: new Blob([bytes]).stream(),
      httpMetadata: { contentType: object.contentType },
      size: object.bytes.byteLength,
    };
  }

  async head(key: string) {
    const object = this.objects.get(key);
    if (!object) return null;
    return {
      httpMetadata: { contentType: object.contentType },
      size: object.bytes.byteLength,
    };
  }

  private multipart(key: string, uploadId: string) {
    return {
      abort: async () => {
        const upload = this.uploads.get(uploadId);
        if (!upload || upload.key !== key || upload.aborted || upload.completed) {
          throw Object.assign(new Error('multipart upload does not exist'), {
            code: 'NoSuchUpload',
          });
        }
        const failThisUpload = this.abortFailureUploadIds.delete(uploadId);
        if (failThisUpload || this.abortFailuresRemaining > 0) {
          if (this.abortFailuresRemaining > 0) {
            this.abortFailuresRemaining -= 1;
          }
          throw new Error('temporary R2 abort failure');
        }
        upload.aborted = true;
        this.abortCalls.push(uploadId);
      },
      complete: async (parts: Array<{ etag: string; partNumber: number }>) => {
        const upload = this.requiredUpload(key, uploadId);
        this.completeCalls.push(uploadId);
        const chunks = parts.map(({ etag, partNumber }) => {
          const part = upload.parts.get(partNumber);
          if (!part || part.etag !== etag) throw new Error('part mismatch');
          return part.bytes;
        });
        const length = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const chunk of chunks) {
          bytes.set(chunk, offset);
          offset += chunk.byteLength;
        }
        this.objects.set(key, { bytes, contentType: upload.contentType });
        upload.completed = true;
        if (this.completeResponseFailuresRemaining > 0) {
          this.completeResponseFailuresRemaining -= 1;
          throw new Error('lost R2 completion response');
        }
        return {
          httpMetadata: { contentType: upload.contentType },
          size: bytes.byteLength,
        };
      },
      key,
      uploadId,
      uploadPart: async (partNumber: number, body: unknown) => {
        const upload = this.requiredUpload(key, uploadId);
        const bytes = await readBodyBytes(body);
        const etag = `etag-${partNumber}-${bytes.byteLength}`;
        upload.parts.set(partNumber, { bytes, etag });
        return { etag, partNumber };
      },
    };
  }

  private requiredUpload(key: string, uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload || upload.key !== key || upload.aborted || upload.completed) {
      throw Object.assign(new Error('multipart upload unavailable'), {
        code: 'NoSuchUpload',
        name: 'NoSuchUpload',
      });
    }
    return upload;
  }
}

class FakeSupabaseObjectStore implements MediaObjectStore {
  readonly deletedKeys: string[] = [];
  readonly getCalls: Array<{ key: string; range?: string }> = [];
  readonly headCalls: string[] = [];
  readonly signedUploads: Array<{ contentType: string; objectKey: string }> = [];
  deleteFailuresRemaining = 0;
  readonly objects = new Map<string, {
    bytes: Uint8Array;
    contentType: string;
  }>();

  async createSignedUpload(input: {
    contentType: string;
    objectKey: string;
  }) {
    this.signedUploads.push(input);
    return {
      chunkSize: 6 * 1024 * 1024,
      objectKey: input.objectKey,
      signedUploadToken: `signed-token-for-${input.objectKey}`,
      signedUploadUrl:
        `https://project-ref.supabase.co/storage/v1/object/upload/sign/${input.objectKey}`,
      tusEndpoint: 'https://project-ref.storage.supabase.co/storage/v1/upload/resumable',
    };
  }

  async deleteObject(key: string): Promise<void> {
    this.deletedKeys.push(key);
    if (this.deleteFailuresRemaining > 0) {
      this.deleteFailuresRemaining -= 1;
      throw new Error('Supabase object deletion failed');
    }
    this.objects.delete(key);
  }

  async getObject(key: string, options: { range?: string } = {}) {
    this.getCalls.push({ key, range: options.range });
    const object = this.objects.get(key);
    if (!object) {
      throw Object.assign(new Error('not found'), {
        code: 'media_not_found',
        status: 404,
      });
    }
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.range ?? '');
    const bytes = match
      ? object.bytes.slice(Number(match[1]), Number(match[2]) + 1)
      : object.bytes;
    return {
      body: new Blob([bytes]).stream(),
      contentType: object.contentType,
      size: object.bytes.byteLength,
      status: match ? 206 as const : 200 as const,
    };
  }

  async headObject(key: string) {
    this.headCalls.push(key);
    const object = this.objects.get(key);
    if (!object) {
      throw Object.assign(new Error('not found'), {
        code: 'media_not_found',
        status: 404,
      });
    }
    return {
      contentType: object.contentType,
      size: object.bytes.byteLength,
    };
  }
}

function bytes(length: number): Uint8Array {
  return Uint8Array.from({ length }, (_, index) => index % 256);
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

function services(
  storage: MemoryMediaStorage,
  bucket: FakeR2Bucket,
  readNow: () => Date = () => NOW,
  objectStore?: FakeSupabaseObjectStore,
): WorkerServices {
  return {
    identityVerifier: new TestIdentityVerifier(),
    appOrigin: APP_ORIGIN,
    invitationClaimRateLimiter: new MemoryRateLimiter(),
    mediaBucket: bucket,
    mediaObjectStore: objectStore,
    mediaSessionSecret: MEDIA_SESSION_SECRET,
    now: readNow,
    ownerEmail: 'chen@example.com',
    publicShareRateLimiter: new MemoryRateLimiter(),
    publisherDeviceAuthenticator: createPublisherDeviceAuthenticator({
      now: readNow,
      pepper: PUBLISHER_TOKEN_PEPPER,
      storage,
    }),
    shareCookieSecret: SESSION_SECRET,
    storage,
  } as unknown as WorkerServices;
}

async function authenticatedMediaCookie(
  app: ReturnType<typeof createWorkerApp>,
  token: string,
  origin = APP_ORIGIN,
): Promise<string> {
  const response = await app.request('/api/media/session', {
    headers: {
      authorization: `Bearer ${token}`,
      origin,
    },
    method: 'POST',
  });
  expect(response.status).toBe(204);
  return response.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
}

function appFor(
  storage = new MemoryMediaStorage(),
  bucket = new FakeR2Bucket(),
  readNow: () => Date = () => NOW,
  objectStore?: FakeSupabaseObjectStore,
) {
  const app = createWorkerApp(services(storage, bucket, readNow, objectStore));
  return { app, bucket, objectStore, storage };
}

async function seedStaleUpload(
  storage: MemoryMediaStorage,
  bucket: FakeR2Bucket,
  sequence: number,
): Promise<TestUploadRecord> {
  const uploadId =
    `00000000-0000-0000-0000-${String(500 + sequence).padStart(12, '0')}`;
  const objectKey = `reports/${REPORT_ID}/media/${MEDIA_ID}/${uploadId}`;
  const multipart = await bucket.createMultipartUpload(objectKey, {
    httpMetadata: { contentType: 'video/mp4' },
  });
  const upload: TestUploadRecord = {
    abortCleanup: null,
    assertedSha256: null,
    contentType: 'video/mp4',
    createdAt: new Date('2026-07-09T08:00:00.000Z'),
    expectedByteSize: 8,
    expiresAt: new Date('2026-07-10T08:00:00.000Z'),
    id: uploadId,
    mediaId: MEDIA_ID,
    objectKey,
    parts: [],
    publisherDeviceId: null,
    r2UploadId: multipart.uploadId,
    reportId: REPORT_ID,
    reportItemId: ITEM_ID,
    sha256Source: null,
    sha256VerificationStatus: null,
    status: 'uploading',
    transitionStartedAt: null,
  };
  storage.uploads.set(upload.id, upload);
  return upload;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function publicCookie(storage: MemoryMediaStorage): Promise<string> {
  const share = await createShareLink(
    storage,
    { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
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
  return response.headers.get('set-cookie')?.split(';')[0] ?? '';
}

function expectPrivateMediaCacheHeaders(response: Response): void {
  expect(response.headers.get('cache-control')).toBe('private, no-store');
  const vary = new Set(
    (response.headers.get('vary') ?? '')
      .split(',')
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  expect(vary).toEqual(new Set(['authorization', 'cookie', 'range']));
}

describe('protected media playback', () => {
  it('uses only the collaboration authorization path for fixed-link media', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );
    const collaboration = vi.spyOn(
      storage,
      'authorizeMediaForCollaborationAtomic',
    );
    const ordinary = vi.spyOn(storage, 'authorizeMediaReadAtomic');

    const response = await readMedia({
      actorId: null,
      bucket,
      collaborationSession: { linkId: 'collaboration-link-id' },
      mediaId: MEDIA_ID,
      method: 'GET',
      publicSession: null,
      rangeHeader: null,
      readAt: NOW,
      storage,
    });

    expect(response.status).toBe(200);
    expect(collaboration).toHaveBeenCalledWith({
      linkId: 'collaboration-link-id',
      mediaId: MEDIA_ID,
      readAt: NOW,
    });
    expect(ordinary).not.toHaveBeenCalled();
  });

  it('rejects mixed media identities before any authorization RPC', async () => {
    const storage = new MemoryMediaStorage();
    const collaboration = vi.spyOn(
      storage,
      'authorizeMediaForCollaborationAtomic',
    );
    const ordinary = vi.spyOn(storage, 'authorizeMediaReadAtomic');

    await expect(readMedia({
      actorId: VIEWER_ID,
      bucket: new FakeR2Bucket(),
      collaborationSession: { linkId: 'collaboration-link-id' },
      mediaId: MEDIA_ID,
      method: 'GET',
      publicSession: null,
      rangeHeader: null,
      readAt: NOW,
      storage,
    })).rejects.toMatchObject({ code: 'media_authorization_conflict' });
    await expect(readMedia({
      actorId: null,
      bucket: new FakeR2Bucket(),
      collaborationSession: { linkId: 'collaboration-link-id' },
      mediaId: MEDIA_ID,
      method: 'GET',
      publicSession: { reportId: REPORT_ID, shareLinkId: 'share-id' },
      rangeHeader: null,
      readAt: NOW,
      storage,
    })).rejects.toMatchObject({ code: 'media_authorization_conflict' });

    expect(collaboration).not.toHaveBeenCalled();
    expect(ordinary).not.toHaveBeenCalled();
  });

  it('conceals historical collaboration media before object reads', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();

    await expect(readMedia({
      actorId: null,
      bucket,
      collaborationSession: { linkId: 'collaboration-link-id' },
      mediaId: MEDIA_ID,
      method: 'GET',
      publicSession: null,
      rangeHeader: null,
      readAt: new Date('2026-07-16T02:30:00.000Z'),
      storage,
    })).rejects.toMatchObject({ code: 'media_not_found', status: 404 });
    expect(bucket.getCalls).toEqual([]);
  });

  it.each(['', 'short-secret'])(
    'fails closed when the media-session secret is too short',
    async (mediaSessionSecret) => {
      const workerServices = services(
        new MemoryMediaStorage(),
        new FakeR2Bucket(),
      );
      workerServices.mediaSessionSecret = mediaSessionSecret;
      const app = createWorkerApp(workerServices);

      const response = await app.request('/api/media/session', {
        headers: {
          authorization: 'Bearer viewer-token',
          origin: APP_ORIGIN,
        },
        method: 'POST',
      });

      expect(response.status).toBe(500);
      expect(response.headers.get('set-cookie')).toBeNull();
      expect(await response.json()).toMatchObject({
        code: 'media_session_secret_invalid',
      });
    },
  );

  it('bootstraps an HttpOnly media session and uses it for GET, HEAD, and Range', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    const sessionResponse = await app.request('/api/media/session', {
      headers: {
        authorization: 'Bearer viewer-token',
        origin: APP_ORIGIN,
      },
      method: 'POST',
    });
    const setCookie = sessionResponse.headers.get('set-cookie') ?? '';
    const cookie = setCookie.split(';', 1)[0] ?? '';

    expect(sessionResponse.status).toBe(204);
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('Secure');
    expect(setCookie).toContain('SameSite=None');
    expect(setCookie).toContain('Path=/api/media');
    expect(sessionResponse.headers.get('access-control-allow-origin')).toBe(
      APP_ORIGIN,
    );
    expect(sessionResponse.headers.get('access-control-allow-credentials')).toBe(
      'true',
    );

    const full = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie, origin: APP_ORIGIN },
    });
    const head = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie, origin: APP_ORIGIN },
      method: 'HEAD',
    });
    const range = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        cookie,
        origin: APP_ORIGIN,
        range: 'bytes=10-19',
      },
    });

    expect(full.status).toBe(200);
    expect(head.status).toBe(200);
    expect((await head.arrayBuffer()).byteLength).toBe(0);
    expect(range.status).toBe(206);
    expect(range.headers.get('content-range')).toBe('bytes 10-19/100');
    expect(range.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
  });

  it('denies invalid and expired media sessions', async () => {
    let currentTime = NOW;
    const { app, bucket } = appFor(
      new MemoryMediaStorage(),
      new FakeR2Bucket(),
      () => currentTime,
    );
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );
    const cookie = await authenticatedMediaCookie(app, 'viewer-token');

    const invalid = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie: `${cookie}tampered`, origin: APP_ORIGIN },
    });
    currentTime = new Date(NOW.getTime() + 6 * 60 * 1000);
    const expired = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie, origin: APP_ORIGIN },
    });

    expect(invalid.status).toBe(401);
    expect(await invalid.json()).toMatchObject({ code: 'media_session_invalid' });
    expect(invalid.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
    expect(invalid.headers.get('access-control-allow-credentials')).toBe('true');
    expect(expired.status).toBe(401);
    expect(await expired.json()).toMatchObject({ code: 'media_session_invalid' });
  });

  it('rechecks report membership for every signed media-session request', async () => {
    const { app, bucket, storage } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );
    const cookie = await authenticatedMediaCookie(app, 'viewer-token');

    expect((await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie, origin: APP_ORIGIN },
    })).status).toBe(200);
    storage.members.delete(`${REPORT_ID}:${VIEWER_ID}`);
    const revoked = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie, origin: APP_ORIGIN },
    });

    expect(revoked.status).toBe(404);
    expect(await revoked.json()).toMatchObject({ code: 'media_not_found' });
  });

  it('rejects credentialed CORS from any origin other than APP_ORIGIN', async () => {
    const { app } = appFor();

    const preflight = await app.request('/api/media/session', {
      headers: {
        'access-control-request-headers': 'authorization',
        'access-control-request-method': 'POST',
        origin: APP_ORIGIN,
      },
      method: 'OPTIONS',
    });
    const denied = await app.request('/api/media/session', {
      headers: {
        authorization: 'Bearer viewer-token',
        origin: 'https://attacker.example',
      },
      method: 'POST',
    });

    expect(preflight.status).toBe(204);
    expect(preflight.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
    expect(preflight.headers.get('access-control-allow-credentials')).toBe('true');
    expect(denied.status).toBe(403);
    expect(denied.headers.get('access-control-allow-origin')).toBeNull();
  });

  it('returns 206 and the requested byte range for an authorized viewer', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        authorization: 'Bearer viewer-token',
        range: 'bytes=10-19',
      },
    });

    expect(response.status).toBe(206);
    expect(response.headers.get('accept-ranges')).toBe('bytes');
    expect(response.headers.get('content-range')).toBe('bytes 10-19/100');
    expect(response.headers.get('content-length')).toBe('10');
    expect(response.headers.get('content-type')).toBe('video/mp4');
    expectPrivateMediaCacheHeaders(response);
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual(
      [...bytes(100).slice(10, 20)],
    );
    expect(bucket.getCalls).toEqual([{
      key: `reports/${REPORT_ID}/published.mp4`,
      range: { length: 10, offset: 10 },
    }]);
  });

  it('proxies authorized playback through a NAS object store with Range support', async () => {
    const objectStore = new FakeSupabaseObjectStore();
    const { app } = appFor(
      new MemoryMediaStorage(),
      new FakeR2Bucket(),
      () => NOW,
      objectStore,
    );
    objectStore.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    const response = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        authorization: 'Bearer viewer-token',
        range: 'bytes=10-19',
      },
    });

    expect(response.status).toBe(206);
    expect(response.headers.get('content-range')).toBe('bytes 10-19/100');
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual(
      [...bytes(100).slice(10, 20)],
    );
    expect(objectStore.getCalls).toEqual([{
      key: `reports/${REPORT_ID}/published.mp4`,
      range: 'bytes=10-19',
    }]);
  });

  it('lets an editor play current draft media while viewers cannot bypass the snapshot', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/draft.webm`,
      { bytes: bytes(50), contentType: 'video/webm' },
    );

    expect((await app.request(`/api/media/${DRAFT_MEDIA_ID}`, {
      headers: { authorization: 'Bearer editor-token' },
    })).status).toBe(200);
    expect((await app.request(`/api/media/${DRAFT_MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
    })).status).toBe(404);
  });

  it('conceals inaccessible and nonexistent media before any R2 read', async () => {
    const { app, bucket } = appFor();

    const inaccessible = await app.request(`/api/media/${OTHER_MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
    });
    const nonexistent = await app.request(`/api/media/${UNKNOWN_MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
    });

    expect(inaccessible.status).toBe(404);
    expect(nonexistent.status).toBe(404);
    expect(await inaccessible.json()).toMatchObject({ code: 'media_not_found' });
    expect(await nonexistent.json()).toMatchObject({ code: 'media_not_found' });
    expect(bucket.getCalls).toEqual([]);
  });

  it('conceals historical GET and HEAD playback before Supabase object reads', async () => {
    const objectStore = new FakeSupabaseObjectStore();
    const { app } = appFor(
      new MemoryMediaStorage(),
      new FakeR2Bucket(),
      () => new Date('2026-07-11T09:00:00Z'),
      objectStore,
    );
    objectStore.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    const get = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
    });
    const head = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
      method: 'HEAD',
    });

    expect(get.status).toBe(404);
    expect(await get.json()).toMatchObject({ code: 'media_not_found' });
    expect(head.status).toBe(404);
    expect(objectStore.getCalls).toEqual([]);
    expect(objectStore.headCalls).toEqual([]);
  });

  it('binds public playback to its report and checks revocation on every request', async () => {
    const { app, bucket, storage } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );
    const cookie = await publicCookie(storage);

    expect((await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie },
    })).status).toBe(200);
    expect((await app.request(`/api/media/${OTHER_MEDIA_ID}`, {
      headers: { cookie },
    })).status).toBe(404);

    const share = storage.shares[0];
    if (share) share.revokedAt = new Date('2026-07-10T09:01:00.000Z');
    const revoked = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { cookie },
    });
    expect(revoked.status).toBe(410);
    expect(await revoked.json()).toMatchObject({ code: 'share_unavailable' });
  });

  it('returns full and open-ended responses with stored MIME and stable headers', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'application/octet-stream' },
    );

    const full = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
    });
    expect(full.status).toBe(200);
    expect(full.headers.get('accept-ranges')).toBe('bytes');
    expect(full.headers.get('content-length')).toBe('100');
    expect(full.headers.get('content-type')).toBe('video/mp4');
    expectPrivateMediaCacheHeaders(full);
    expect(new Uint8Array(await full.arrayBuffer())).toHaveLength(100);

    const open = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        authorization: 'Bearer viewer-token',
        range: 'bytes=95-',
      },
    });
    expect(open.status).toBe(206);
    expect(open.headers.get('content-range')).toBe('bytes 95-99/100');
    expect(open.headers.get('content-length')).toBe('5');
    expectPrivateMediaCacheHeaders(open);
  });

  it('supports suffix ranges and Range HEAD without loading the full object body', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    const suffix = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        authorization: 'Bearer viewer-token',
        range: 'bytes=-7',
      },
    });
    expect(suffix.status).toBe(206);
    expect(suffix.headers.get('content-range')).toBe('bytes 93-99/100');
    expect(new Uint8Array(await suffix.arrayBuffer())).toHaveLength(7);

    const head = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: { authorization: 'Bearer viewer-token' },
      method: 'HEAD',
    });
    expect(head.status).toBe(200);
    expect(head.headers.get('content-length')).toBe('100');
    expectPrivateMediaCacheHeaders(head);
    expect((await head.arrayBuffer()).byteLength).toBe(0);

    const rangeHead = await app.request(`/api/media/${MEDIA_ID}`, {
      headers: {
        authorization: 'Bearer viewer-token',
        range: 'bytes=10-19',
      },
      method: 'HEAD',
    });
    expect(rangeHead.status).toBe(206);
    expect(rangeHead.headers.get('content-range')).toBe('bytes 10-19/100');
    expect(rangeHead.headers.get('content-length')).toBe('10');
    expectPrivateMediaCacheHeaders(rangeHead);
    expect((await rangeHead.arrayBuffer()).byteLength).toBe(0);
    expect(bucket.getCalls).toHaveLength(1);
  });

  it('returns 416 for malformed and unsatisfiable single ranges', async () => {
    const { app, bucket } = appFor();
    bucket.objects.set(
      `reports/${REPORT_ID}/published.mp4`,
      { bytes: bytes(100), contentType: 'video/mp4' },
    );

    for (const range of ['items=0-1', 'bytes=20-10', 'bytes=100-', 'bytes=0-1,4-5']) {
      const response = await app.request(`/api/media/${MEDIA_ID}`, {
        headers: {
          authorization: 'Bearer viewer-token',
          range,
        },
      });
      expect(response.status).toBe(416);
      expect(response.headers.get('accept-ranges')).toBe('bytes');
      expect(response.headers.get('content-range')).toBe('bytes */100');
      expectPrivateMediaCacheHeaders(response);
    }
    expect(bucket.getCalls).toEqual([]);
  });
});

describe('persistent multipart uploads', () => {
  it('authenticates publisher devices without persisting the raw token', async () => {
    const storage = new MemoryMediaStorage();
    const { app } = appFor(storage);
    storage.publisherDeviceHash = await sha256Hex(
      `${PUBLISHER_TOKEN_PEPPER}:${PUBLISHER_TOKEN}`,
    );

    const response = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        'content-type': 'application/json',
        'x-publisher-token': PUBLISHER_TOKEN,
      },
      method: 'POST',
    });

    expect(response.status).toBe(201);
    expect(storage.publisherDeviceLastUsedAt?.toISOString()).toBe(
      NOW.toISOString(),
    );
    expect([...storage.uploads.values()][0]).toMatchObject({
      publisherDeviceId: PUBLISHER_DEVICE_ID,
    });
    expect(JSON.stringify([...storage.uploads.values()])).not.toContain(
      PUBLISHER_TOKEN,
    );
  });

  it.each([
    { header: '', expectedCode: 'missing_publisher_token' },
    { header: 'short-token', expectedCode: 'invalid_publisher_token' },
  ])('rejects invalid publisher token headers', async ({ expectedCode, header }) => {
    const { app } = appFor();

    const response = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        'content-type': 'application/json',
        'x-publisher-token': header,
      },
      method: 'POST',
    });

    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ code: expectedCode });
  });

  it('fails closed when the publisher token pepper is too short', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const workerServices = services(storage, bucket);
    workerServices.publisherDeviceAuthenticator = createPublisherDeviceAuthenticator({
      now: () => NOW,
      pepper: 'short-pepper',
      storage,
    });
    const app = createWorkerApp(workerServices);

    const response = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        'content-type': 'application/json',
        'x-publisher-token': PUBLISHER_TOKEN,
      },
      method: 'POST',
    });

    expect(response.status).toBe(500);
    expect(await response.json()).toMatchObject({
      code: 'publisher_token_pepper_invalid',
    });
  });

  it('rejects unavailable publisher devices', async () => {
    const { app } = appFor();

    const response = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        'content-type': 'application/json',
        'x-publisher-token': PUBLISHER_TOKEN,
      },
      method: 'POST',
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({
      code: 'publisher_device_unavailable',
    });
  });

  it('preserves ready media when replacement R2 creation or attachment fails', async () => {
    for (const failure of ['create', 'attach'] as const) {
      const storage = new MemoryMediaStorage();
      const bucket = new FakeR2Bucket();
      const original = structuredClone(storage.media.get(MEDIA_ID));
      if (failure === 'create') {
        bucket.createFailuresRemaining = 1;
      } else {
        storage.attachFailuresRemaining = 2;
      }
      const { app } = appFor(storage, bucket);

      const response = await app.request('/api/media/uploads', {
        body: JSON.stringify({
          byteSize: 8,
          contentType: 'video/mp4',
          reportId: REPORT_ID,
          reportItemId: ITEM_ID,
        }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      });

      expect(response.status).toBe(500);
      expect(storage.media.get(MEDIA_ID)).toEqual(original);
      if (failure === 'attach') {
        expect(bucket.abortCalls).toHaveLength(1);
        expect([...storage.uploads.values()][0]).toMatchObject({
          r2UploadId: null,
          status: 'creating',
        });
      }
    }
  });

  it('reconciles an ambiguous attach response without aborting persisted R2 state', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    storage.attachCommitThenFailRemaining = 1;
    const { app } = appFor(storage, bucket);

    const response = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });

    expect(response.status).toBe(201);
    const body = await response.json() as { uploadId: string };
    const upload = storage.uploads.get(body.uploadId);
    expect(upload).toMatchObject({
      r2UploadId: 'r2-upload-1',
      status: 'uploading',
    });
    expect(bucket.abortCalls).toEqual([]);

    const resumed = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    expect(resumed.status).toBe(200);
    expect(await resumed.json()).toMatchObject({ uploadId: body.uploadId });
    expect(bucket.createCalls).toHaveLength(1);
  });

  it('persists parts across app instances and completes the object idempotently', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const firstApp = appFor(storage, bucket).app;

    const created = await firstApp.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    expect(created.status).toBe(201);
    const upload = await created.json() as { uploadId: string };

    const partBytes = Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8]);
    const part = await firstApp.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: partBytes,
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': String(partBytes.byteLength),
        },
        method: 'PUT',
      },
    );
    expect(part.status).toBe(200);
    expect(await part.json()).toMatchObject({
      byteSize: 8,
      partNumber: 1,
    });

    const resumedApp = appFor(storage, bucket).app;
    const resumed = await resumedApp.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    expect(resumed.status).toBe(200);
    expect(await resumed.json()).toMatchObject({ uploadId: upload.uploadId });
    expect(bucket.createCalls).toHaveLength(1);

    const listed = await resumedApp.request(
      `/api/media/uploads/${upload.uploadId}/parts`,
      { headers: { authorization: 'Bearer owner-token' } },
    );
    expect(listed.status).toBe(200);
    expect(await listed.json()).toMatchObject({
      parts: [{ byteSize: 8, partNumber: 1 }],
    });

    const completed = await resumedApp.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(completed.status).toBe(200);
    expect(await completed.json()).toMatchObject({
      integrity: {
        algorithm: 'sha256',
        source: 'trusted_uploader_assertion',
        verificationStatus: 'not_server_verified',
      },
      mediaId: MEDIA_ID,
      status: 'ready',
    });
    expect([
      ...(bucket.objects.get(storage.uploads.get(upload.uploadId)?.objectKey ?? '')
        ?.bytes ?? []),
    ]).toEqual([...partBytes]);

    const repeated = await resumedApp.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(repeated.status).toBe(200);
  });

  it('recovers ambiguous R2 completion and DB finalization idempotently', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const { app } = appFor(storage, bucket);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    expect((await app.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8]),
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': '8',
        },
        method: 'PUT',
      },
    )).status).toBe(200);
    bucket.completeResponseFailuresRemaining = 1;
    storage.completeFinalizeFailuresRemaining = 1;

    const first = await app.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(first.status).toBe(500);
    expect(storage.uploads.get(upload.uploadId)).toMatchObject({
      assertedSha256: 'a'.repeat(64),
      status: 'completing',
    });
    expect(storage.media.get(MEDIA_ID)?.objectKey).toBe(
      `reports/${REPORT_ID}/published.mp4`,
    );

    const recovered = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(await recovered.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('completed');

    const retried = await app.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(retried.status).toBe(200);
    expect(await retried.json()).toMatchObject({
      integrity: {
        source: 'trusted_uploader_assertion',
        verificationStatus: 'not_server_verified',
      },
      status: 'ready',
    });
    expect(bucket.completeCalls).toHaveLength(1);
  });

  it('re-drives stale completing uploads from persisted parts during cleanup', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const { app } = appFor(storage, bucket);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    expect((await app.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8]),
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': '8',
        },
        method: 'PUT',
      },
    )).status).toBe(200);
    const persisted = storage.uploads.get(upload.uploadId);
    if (!persisted) throw new Error('missing upload');
    persisted.assertedSha256 = 'a'.repeat(64);
    persisted.sha256Source = 'trusted_uploader_assertion';
    persisted.sha256VerificationStatus = 'not_server_verified';
    persisted.status = 'completing';
    persisted.transitionStartedAt = new Date('2026-07-10T08:00:00.000Z');

    const recovered = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });

    expect(recovered.status).toBe(200);
    expect(await recovered.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('completed');
    expect(bucket.completeCalls).toEqual(['r2-upload-1']);
    expect(storage.media.get(MEDIA_ID)?.objectKey).toBe(
      `reports/${REPORT_ID}/media/${MEDIA_ID}/${upload.uploadId}`,
    );
  });

  it('observes fresh completing uploads without re-driving multipart completion', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const { app } = appFor(storage, bucket);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    const persisted = storage.uploads.get(upload.uploadId);
    if (!persisted) throw new Error('missing upload');
    persisted.assertedSha256 = 'a'.repeat(64);
    persisted.sha256Source = 'trusted_uploader_assertion';
    persisted.sha256VerificationStatus = 'not_server_verified';
    persisted.status = 'completing';
    persisted.transitionStartedAt = NOW;

    const recovered = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });

    expect(recovered.status).toBe(200);
    expect(await recovered.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('completing');
    expect(bucket.completeCalls).toEqual([]);
  });

  it('resets stale completing uploads when neither multipart state nor object exists', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const { app } = appFor(storage, bucket);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    expect((await app.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8]),
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': '8',
        },
        method: 'PUT',
      },
    )).status).toBe(200);
    const persisted = storage.uploads.get(upload.uploadId);
    if (!persisted?.r2UploadId) throw new Error('missing upload');
    persisted.assertedSha256 = 'b'.repeat(64);
    persisted.sha256Source = 'trusted_uploader_assertion';
    persisted.sha256VerificationStatus = 'not_server_verified';
    persisted.status = 'completing';
    persisted.transitionStartedAt = new Date('2026-07-10T08:00:00.000Z');
    bucket.uploads.delete(persisted.r2UploadId);

    const recovered = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });

    expect(recovered.status).toBe(200);
    expect(await recovered.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    const replacement = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    expect(replacement.status).toBe(201);
  });

  it('rejects invalid part numbers before touching R2', async () => {
    const { app, bucket } = appFor();
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };

    for (const partNumber of ['0', '10001', '1e2', '1.5', 'not-a-number']) {
      const response = await app.request(
        `/api/media/uploads/${upload.uploadId}/parts/${partNumber}`,
        {
          body: Uint8Array.from([1]),
          headers: {
            authorization: 'Bearer owner-token',
            'content-length': '1',
          },
          method: 'PUT',
        },
      );

      expect(response.status).toBe(400);
    }
    expect(bucket.uploads.values().next().value?.parts.size ?? 0).toBe(0);
  });

  it('rejects missing and non-decimal part lengths before touching R2', async () => {
    const { app, bucket } = appFor();
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };

    for (const contentLength of [undefined, '1e1', '1.5', '-1']) {
      const headers = new Headers({ authorization: 'Bearer owner-token' });
      if (contentLength !== undefined) {
        headers.set('content-length', contentLength);
      }
      const response = await app.request(
        `/api/media/uploads/${upload.uploadId}/parts/1`,
        {
          body: Uint8Array.from([1]),
          headers,
          method: 'PUT',
        },
      );

      expect(response.status).toBe(400);
    }
    expect(bucket.uploads.values().next().value?.parts.size ?? 0).toBe(0);
  });

  it('rejects expired uploads before sending another part to R2', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let clock = NOW;
    const { app } = appFor(storage, bucket, () => clock);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    clock = new Date(NOW.getTime() + 24 * 60 * 60 * 1000);

    const response = await app.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: Uint8Array.from([1]),
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': '1',
        },
        method: 'PUT',
      },
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ code: 'upload_expired' });
    expect(bucket.uploads.values().next().value?.parts.size ?? 0).toBe(0);
  });

  it('rejects inconsistent non-final multipart sizes before R2 completion', async () => {
    const { app, bucket, storage } = appFor();
    const fiveMiB = 5 * 1024 ** 2;
    const sixMiB = 6 * 1024 ** 2;
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: fiveMiB + sixMiB + 1,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    const storedUpload = storage.uploads.get(upload.uploadId);
    expect(storedUpload?.r2UploadId).toBeTruthy();
    if (!storedUpload?.r2UploadId) throw new Error('missing test upload');
    storedUpload.parts = [
      { byteSize: fiveMiB, etag: 'etag-1', partNumber: 1 },
      { byteSize: sixMiB, etag: 'etag-2', partNumber: 2 },
      { byteSize: 1, etag: 'etag-3', partNumber: 3 },
    ];
    const r2Upload = bucket.uploads.get(storedUpload.r2UploadId);
    if (!r2Upload) throw new Error('missing fake R2 upload');
    r2Upload.parts.set(1, { bytes: Uint8Array.from([1]), etag: 'etag-1' });
    r2Upload.parts.set(2, { bytes: Uint8Array.from([2]), etag: 'etag-2' });
    r2Upload.parts.set(3, { bytes: Uint8Array.from([3]), etag: 'etag-3' });

    const response = await app.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ code: 'upload_incomplete' });
    expect(bucket.completeCalls).toEqual([]);
  });

  it('validates completion SHA and expected total before R2 completion', async () => {
    const { app, bucket } = appFor();
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    const partBytes = Uint8Array.from([1, 2, 3, 4, 5, 6, 7]);
    expect((await app.request(
      `/api/media/uploads/${upload.uploadId}/parts/1`,
      {
        body: partBytes,
        headers: {
          authorization: 'Bearer owner-token',
          'content-length': String(partBytes.byteLength),
        },
        method: 'PUT',
      },
    )).status).toBe(200);

    const invalidSha = await app.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'not-a-sha256' }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(invalidSha.status).toBe(400);
    expect(await invalidSha.json()).toMatchObject({ code: 'invalid_sha256' });

    const wrongTotal = await app.request(
      `/api/media/uploads/${upload.uploadId}/complete`,
      {
        body: JSON.stringify({ sha256: 'a'.repeat(64) }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      },
    );
    expect(wrongTotal.status).toBe(409);
    expect(await wrongTotal.json()).toMatchObject({ code: 'upload_incomplete' });
    expect(bucket.completeCalls).toEqual([]);
  });

  it('aborts an owner upload idempotently', async () => {
    const { app, bucket, storage } = appFor();
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const response = await app.request(
        `/api/media/uploads/${upload.uploadId}`,
        {
          headers: { authorization: 'Bearer owner-token' },
          method: 'DELETE',
        },
      );
      expect(response.status).toBe(204);
    }

    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    expect(bucket.abortCalls).toHaveLength(1);
  });

  it('aborts uploads older than 24 hours and marks persisted state stale', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let clock = NOW;
    const { app } = appFor(storage, bucket, () => clock);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    clock = new Date(NOW.getTime() + 24 * 60 * 60 * 1000 + 1);

    const cleanup = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });

    expect(cleanup.status).toBe(200);
    expect(await cleanup.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    expect(bucket.abortCalls).toHaveLength(1);
  });

  it('leaves stale uploads retryable when R2 abort fails', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let clock = NOW;
    const { app } = appFor(storage, bucket, () => clock);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    clock = new Date(NOW.getTime() + 24 * 60 * 60 * 1000 + 1);
    bucket.abortFailuresRemaining = 1;

    const failed = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(failed.status).toBe(200);
    expect(await failed.json()).toEqual({
      attempted: 1,
      failed: 1,
      succeeded: 0,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborting');

    const retried = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(retried.status).toBe(200);
    expect(await retried.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    expect(bucket.abortCalls).toHaveLength(1);
  });

  it('finishes stale cleanup after an R2 abort succeeds but DB persistence fails', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let clock = NOW;
    const { app } = appFor(storage, bucket, () => clock);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    clock = new Date(NOW.getTime() + 24 * 60 * 60 * 1000 + 1);
    storage.abortFinalizeFailuresRemaining = 1;

    const failed = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(failed.status).toBe(200);
    expect(await failed.json()).toEqual({
      attempted: 1,
      failed: 1,
      succeeded: 0,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborting');
    expect(bucket.abortCalls).toHaveLength(1);

    const retried = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(retried.status).toBe(200);
    expect(await retried.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    expect(bucket.abortCalls).toHaveLength(1);
  });

  it('continues after poisoned uploads and drains recovery pages beyond 100 rows', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    const { app } = appFor(storage, bucket);
    const uploads = await Promise.all(
      Array.from({ length: 205 }, (_, index) =>
        seedStaleUpload(storage, bucket, index)),
    );
    const poisoned = uploads[0];
    if (!poisoned?.r2UploadId) throw new Error('missing poisoned R2 upload');
    bucket.abortFailureUploadIds.add(poisoned.r2UploadId);

    const response = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      attempted: 205,
      failed: 1,
      succeeded: 204,
    });
    expect(storage.recoveryPageCalls).toBeGreaterThan(2);
    expect(storage.uploads.get(poisoned.id)?.status).toBe('aborting');
    expect(
      [...storage.uploads.values()]
        .filter(({ status }) => status === 'aborted'),
    ).toHaveLength(204);

    const retried = await app.request('/api/media/uploads/cleanup', {
      headers: { authorization: 'Bearer owner-token' },
      method: 'POST',
    });
    expect(await retried.json()).toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
  });

  it('does not wait for slow or rejected opportunistic cleanup before creating uploads', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let releaseCleanup: (() => void) | null = null;
    storage.recoveryWait = new Promise<void>((resolve) => {
      releaseCleanup = resolve;
    });
    storage.recoveryWaitError = new ReportError(
      500,
      'storage_error',
      'storage request failed',
    );
    const { app } = appFor(storage, bucket);
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    try {
      const createdPromise = app.request('/api/media/uploads', {
        body: JSON.stringify({
          byteSize: 8,
          contentType: 'video/mp4',
          reportId: REPORT_ID,
          reportItemId: ITEM_ID,
        }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      });

      const raced = await Promise.race([
        createdPromise.then(() => 'response'),
        sleep(25).then(() => 'timeout'),
      ]);

      releaseCleanup?.();
      const created = await createdPromise;
      await sleep(0);

      expect(raced).toBe('response');
      expect(created.status).toBe(201);
      expect(await created.json()).toMatchObject({ status: 'uploading' });
      expect(bucket.createCalls).toHaveLength(1);
      expect(error).toHaveBeenCalledWith(
        'scheduled media cleanup failed',
        storage.recoveryWaitError,
      );
    } finally {
      error.mockRestore();
    }
  });

  it('runs scheduled stale cleanup through the same persisted service path', async () => {
    const storage = new MemoryMediaStorage();
    const bucket = new FakeR2Bucket();
    let clock = NOW;
    const workerServices = services(storage, bucket, () => clock);
    const app = createWorkerApp(workerServices);
    const created = await app.request('/api/media/uploads', {
      body: JSON.stringify({
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      }),
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    });
    const upload = await created.json() as { uploadId: string };
    clock = new Date(NOW.getTime() + 24 * 60 * 60 * 1000);

    await expect(runScheduledMediaCleanup(workerServices)).resolves.toEqual({
      attempted: 1,
      failed: 0,
      succeeded: 1,
    });
    expect(storage.uploads.get(upload.uploadId)?.status).toBe('aborted');
    expect(bucket.abortCalls).toHaveLength(1);
  });

  it('purges due Supabase objects and records successful deletion', async () => {
    const storage = new MemoryMediaStorage();
    const objectStore = new FakeSupabaseObjectStore();
    storage.expiredMedia.set(MEDIA_ID, {
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });
    const workerServices = services(
      storage,
      new FakeR2Bucket(),
      () => NOW,
      objectStore,
    );

    await runScheduledMediaCleanup(workerServices);

    expect(storage.mediaPurgeClaims).toEqual([{ limit: 100, now: NOW }]);
    expect(objectStore.deletedKeys).toEqual([
      `reports/${REPORT_ID}/published.mp4`,
    ]);
    expect(storage.mediaPurgeResults.get(MEDIA_ID)).toEqual({
      error: null,
      purgedAt: NOW,
    });
  });

  it('records failed Supabase deletion and retries it on the next cron', async () => {
    const storage = new MemoryMediaStorage();
    const objectStore = new FakeSupabaseObjectStore();
    storage.expiredMedia.set(MEDIA_ID, {
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });
    objectStore.deleteFailuresRemaining = 1;
    let clock = NOW;
    const workerServices = services(
      storage,
      new FakeR2Bucket(),
      () => clock,
      objectStore,
    );

    await runScheduledMediaCleanup(workerServices);
    expect(storage.mediaPurgeResults.get(MEDIA_ID)).toEqual({
      error: 'storage_delete_failed',
      purgedAt: null,
    });

    clock = new Date(NOW.getTime() + 60 * 60 * 1000);
    await runScheduledMediaCleanup(workerServices);

    expect(objectStore.deletedKeys).toEqual([
      `reports/${REPORT_ID}/published.mp4`,
      `reports/${REPORT_ID}/published.mp4`,
    ]);
    expect(storage.mediaPurgeResults.get(MEDIA_ID)).toEqual({
      error: null,
      purgedAt: clock,
    });
  });

  it('surfaces purge persistence failure separately after object deletion succeeds', async () => {
    const storage = new MemoryMediaStorage();
    const objectStore = new FakeSupabaseObjectStore();
    storage.expiredMedia.set(MEDIA_ID, {
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });
    storage.finishMediaPurgeFailuresRemaining = 1;
    const workerServices = services(
      storage,
      new FakeR2Bucket(),
      () => NOW,
      objectStore,
    );

    await expect(runScheduledMediaCleanup(workerServices)).rejects.toThrow(
      'purge persistence failed',
    );
    expect(objectStore.deletedKeys).toEqual([
      `reports/${REPORT_ID}/published.mp4`,
    ]);
    expect(storage.mediaPurgeResults.has(MEDIA_ID)).toBe(false);
  });

  it('logs deferred purge persistence failures instead of relabeling them', async () => {
    const storage = new MemoryMediaStorage();
    const objectStore = new FakeSupabaseObjectStore();
    storage.expiredMedia.set(MEDIA_ID, {
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });
    storage.finishMediaPurgeFailuresRemaining = 1;
    const { app } = appFor(
      storage,
      new FakeR2Bucket(),
      () => NOW,
      objectStore,
    );
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    try {
      const response = await app.request('/api/media/uploads', {
        body: JSON.stringify({
          byteSize: 8,
          contentType: 'video/mp4',
          reportId: REPORT_ID,
          reportItemId: ITEM_ID,
        }),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      });
      expect(response.status).toBe(201);
      await sleep(0);
      expect(error).toHaveBeenCalledWith(
        'scheduled media cleanup failed',
        expect.objectContaining({ message: 'purge persistence failed' }),
      );
    } finally {
      error.mockRestore();
    }
  });

  it('does not claim a purge again until its one-hour lease expires', async () => {
    const storage = new MemoryMediaStorage();
    storage.expiredMedia.set(MEDIA_ID, {
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });
    await expect(storage.claimExpiredMediaObjects({
      limit: 100,
      now: NOW,
    })).resolves.toEqual([{
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    }]);
    expect(storage.mediaPurgeAttemptedAt.get(MEDIA_ID)).toEqual(NOW);
    await expect(storage.claimExpiredMediaObjects({
      limit: 100,
      now: new Date(NOW.getTime() + 59 * 60 * 1000),
    })).resolves.toEqual([]);
    const clock = new Date(NOW.getTime() + 60 * 60 * 1000 + 1);
    await expect(storage.claimExpiredMediaObjects({
      limit: 100,
      now: clock,
    })).resolves.toEqual([{
      id: MEDIA_ID,
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    }]);
    expect(storage.mediaPurgeAttemptedAt.get(MEDIA_ID)).toEqual(clock);
  });

  it('allows only the owner until the publisher-device authenticator is implemented', async () => {
    const { app, bucket } = appFor();
    const body = JSON.stringify({
      byteSize: 8,
      contentType: 'video/mp4',
      reportId: REPORT_ID,
      reportItemId: ITEM_ID,
    });

    expect((await app.request('/api/media/uploads', {
      body,
      headers: { 'content-type': 'application/json' },
      method: 'POST',
    })).status).toBe(401);
    expect((await app.request('/api/media/uploads', {
      body,
      headers: {
        authorization: 'Bearer editor-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    })).status).toBe(403);
    expect((await app.request('/api/media/uploads', {
      body,
      headers: {
        authorization: 'Bearer owner-token',
        'content-type': 'application/json',
      },
      method: 'POST',
    })).status).toBe(201);
    expect(bucket.createCalls).toHaveLength(1);
  });

  it('validates content type and byte size before creating object keys', async () => {
    const { app, bucket } = appFor();

    for (const body of [
      {
        byteSize: 0,
        contentType: 'video/mp4',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      },
      {
        byteSize: 8,
        contentType: 'text/html',
        reportId: REPORT_ID,
        reportItemId: ITEM_ID,
      },
      {
        byteSize: 8,
        contentType: 'video/mp4',
        reportId: OTHER_REPORT_ID,
        reportItemId: ITEM_ID,
      },
    ]) {
      const response = await app.request('/api/media/uploads', {
        body: JSON.stringify(body),
        headers: {
          authorization: 'Bearer owner-token',
          'content-type': 'application/json',
        },
        method: 'POST',
      });
      expect([400, 404]).toContain(response.status);
    }
    expect(bucket.createCalls).toEqual([]);
  });
});

describe('production media adapters', () => {
  it('persists media purge claims and outcomes through service-role RPCs', async () => {
    const serviceKey = 'test-service-role-key';
    const objectKey = `reports/${REPORT_ID}/published.mp4`;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/rest/v1/rpc/claim_expired_media_objects')) {
        return Response.json([{ id: MEDIA_ID, object_key: objectKey }]);
      }
      if (url.endsWith('/rest/v1/rpc/finish_media_object_purge')) {
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    });
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.claimExpiredMediaObjects({
      limit: 100,
      now: NOW,
    })).resolves.toEqual([{ id: MEDIA_ID, objectKey }]);
    await storage.finishMediaObjectPurge({
      error: null,
      mediaId: MEDIA_ID,
      purgedAt: NOW,
    });

    expect(fetcher.mock.calls.map(([input]) => String(input))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/claim_expired_media_objects',
      'https://project.supabase.co/rest/v1/rpc/finish_media_object_purge',
    ]);
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      p_limit: 100,
      p_now: NOW.toISOString(),
    });
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      p_error: null,
      p_media_id: MEDIA_ID,
      p_purged_at: NOW.toISOString(),
    });
  });

  it('authenticates publisher devices through a service-role Supabase RPC', async () => {
    const serviceKey = 'test-service-role-key';
    const tokenHash = 'a'.repeat(64);
    const fetcher = vi.fn(async () => Response.json({
      device_id: PUBLISHER_DEVICE_ID,
    }));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.authenticatePublisherDeviceAtomic({
      tokenHash,
      usedAt: NOW,
    })).resolves.toEqual({ deviceId: PUBLISHER_DEVICE_ID });

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      'https://project.supabase.co/rest/v1/rpc/authenticate_publisher_device',
    );
    const request = fetcher.mock.calls[0]?.[1];
    const headers = new Headers(request?.headers);
    expect(headers.get('apikey')).toBe(serviceKey);
    expect(headers.get('authorization')).toBe(`Bearer ${serviceKey}`);
    expect(JSON.parse(String(request?.body))).toEqual({
      p_token_hash: tokenHash,
      p_used_at: NOW.toISOString(),
    });
  });

  it('authorizes playback through one service-role Supabase RPC', async () => {
    const serviceKey = 'test-service-role-key';
    const fetcher = vi.fn(async () => new Response(JSON.stringify([{
      byte_size: 100,
      content_type: 'video/mp4',
      daily_date: '2026-07-10',
      id: MEDIA_ID,
      object_key: `reports/${REPORT_ID}/published.mp4`,
      report_id: REPORT_ID,
    }]), {
      headers: { 'content-type': 'application/json' },
      status: 200,
    }));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    }) as SupabaseRestStorage & {
      authorizeMediaReadAtomic(input: {
        actorId: string | null;
        mediaId: string;
        publicReportId: string | null;
        readAt: Date;
        shareLinkId: string | null;
      }): Promise<TestMediaRecord>;
    };

    await expect(storage.authorizeMediaReadAtomic({
      actorId: VIEWER_ID,
      mediaId: MEDIA_ID,
      publicReportId: null,
      readAt: NOW,
      shareLinkId: null,
    })).resolves.toMatchObject({
      byteSize: 100,
      contentType: 'video/mp4',
      dailyDate: '2026-07-10',
      objectKey: `reports/${REPORT_ID}/published.mp4`,
    });

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      'https://project.supabase.co/rest/v1/rpc/authorize_media_read',
    );
    const request = fetcher.mock.calls[0]?.[1];
    const headers = new Headers(request?.headers);
    expect(headers.get('apikey')).toBe(serviceKey);
    expect(headers.get('authorization')).toBe(`Bearer ${serviceKey}`);
    expect(JSON.parse(String(request?.body))).toMatchObject({
      p_actor_id: VIEWER_ID,
      p_media_id: MEDIA_ID,
      p_public_report_id: null,
      p_share_link_id: null,
    });
  });

  it('persists multipart operations through service-role Supabase RPCs', async () => {
    const serviceKey = 'test-service-role-key';
    const uploadId = '00000000-0000-0000-0000-000000000501';
    const objectKey =
      `reports/${REPORT_ID}/media/${MEDIA_ID}/${uploadId}`;
    const uploadRow = {
      abort_cleanup: null,
      asserted_sha256: null,
      content_type: 'video/mp4',
      created_at: NOW.toISOString(),
      expected_byte_size: 8,
      expires_at: '2026-07-11T09:00:00.000Z',
      id: uploadId,
      media_id: MEDIA_ID,
      object_key: objectKey,
      parts: [{ byte_size: 8, etag: 'etag-1', part_number: 1 }],
      r2_upload_id: 'r2-upload-1',
      report_id: REPORT_ID,
      report_item_id: ITEM_ID,
      resumed: false,
      sha256_source: null,
      sha256_verification_status: null,
      status: 'uploading',
      transition_started_at: null,
    };
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/rest/v1/rpc/begin_media_upload')) {
        return Response.json({ ...uploadRow, r2_upload_id: null, status: 'creating' });
      }
      if (url.endsWith('/rest/v1/rpc/attach_media_upload')) {
        return Response.json(uploadRow);
      }
      if (url.endsWith('/rest/v1/rpc/record_media_upload_part')) {
        return Response.json({
          byte_size: 8,
          etag: 'etag-1',
          part_number: 1,
        });
      }
      if (url.endsWith('/rest/v1/rpc/read_media_upload')) {
        return Response.json(uploadRow);
      }
      if (url.endsWith('/rest/v1/rpc/claim_media_upload_completion')) {
        return Response.json({
          ...uploadRow,
          asserted_sha256: 'a'.repeat(64),
          sha256_source: 'trusted_uploader_assertion',
          sha256_verification_status: 'not_server_verified',
          status: 'completing',
          transition_started_at: NOW.toISOString(),
        });
      }
      if (url.endsWith('/rest/v1/rpc/finalize_media_upload_completion')) {
        return Response.json({
          byte_size: 8,
          content_type: 'video/mp4',
          id: MEDIA_ID,
          object_key: objectKey,
          report_id: REPORT_ID,
        });
      }
      if (url.endsWith('/rest/v1/rpc/claim_stale_media_upload_completion')) {
        return Response.json({
          ...uploadRow,
          asserted_sha256: 'a'.repeat(64),
          sha256_source: 'trusted_uploader_assertion',
          sha256_verification_status: 'not_server_verified',
          status: 'completing',
          transition_started_at: NOW.toISOString(),
        });
      }
      if (url.endsWith('/rest/v1/rpc/list_media_upload_recovery_parts')) {
        return Response.json(uploadRow.parts);
      }
      if (url.endsWith('/rest/v1/rpc/reset_stale_media_upload_completion')) {
        return Response.json({
          ...uploadRow,
          abort_cleanup: true,
          status: 'aborted',
          transition_started_at: null,
        });
      }
      if (url.endsWith('/rest/v1/rpc/list_media_upload_recovery_page')) {
        return Response.json([uploadRow]);
      }
      if (url.endsWith('/rest/v1/rpc/claim_media_upload_abort')) {
        return Response.json({
          ...uploadRow,
          abort_cleanup: false,
          status: 'aborting',
          transition_started_at: NOW.toISOString(),
        });
      }
      if (url.endsWith('/rest/v1/rpc/finalize_media_upload_abort')) {
        return Response.json({
          ...uploadRow,
          abort_cleanup: false,
          status: 'aborted',
          transition_started_at: NOW.toISOString(),
        });
      }
      return new Response(null, { status: 404 });
    });
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });
    const actor = { actorId: OWNER_ID, publisherDeviceId: null };

    await storage.beginMediaUploadAtomic({
      ...actor,
      contentType: 'video/mp4',
      expectedByteSize: 8,
      reportId: REPORT_ID,
      reportItemId: ITEM_ID,
      startedAt: NOW,
      uploadId,
    });
    await storage.attachMediaUploadAtomic({
      ...actor,
      attachedAt: NOW,
      r2UploadId: 'r2-upload-1',
      uploadId,
    });
    await storage.recordMediaUploadPartAtomic({
      ...actor,
      byteSize: 8,
      etag: 'etag-1',
      partNumber: 1,
      uploadedAt: NOW,
      uploadId,
    });
    await storage.getMediaUploadAtomic({ ...actor, uploadId });
    await storage.listMediaUploadPartsAtomic({ ...actor, uploadId });
    await storage.claimMediaUploadCompletionAtomic({
      ...actor,
      assertedSha256: 'a'.repeat(64),
      claimedAt: NOW,
      uploadId,
    });
    await storage.finalizeMediaUploadCompletionAtomic({
      completedAt: NOW,
      uploadId,
    });
    await storage.claimStaleMediaUploadCompletionAtomic({
      claimedAt: NOW,
      staleBefore: new Date(NOW.getTime() - 5 * 60 * 1000),
      uploadId,
    });
    await storage.listMediaUploadRecoveryPartsAtomic({ uploadId });
    await storage.resetStaleMediaUploadCompletionAtomic({
      expectedClaimedAt: NOW,
      reason: 'r2_upload_missing',
      resetAt: NOW,
      uploadId,
    });
    await storage.listMediaUploadRecoveryPage({
      afterId: null,
      limit: 100,
      staleAt: NOW,
    });
    await storage.claimMediaUploadAbortAtomic({
      ...actor,
      claimedAt: NOW,
      cleanup: false,
      uploadId,
    });
    await storage.finalizeMediaUploadAbortAtomic({
      abortedAt: NOW,
      uploadId,
    });

    expect(fetcher.mock.calls.map(([input]) => String(input))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/begin_media_upload',
      'https://project.supabase.co/rest/v1/rpc/attach_media_upload',
      'https://project.supabase.co/rest/v1/rpc/record_media_upload_part',
      'https://project.supabase.co/rest/v1/rpc/read_media_upload',
      'https://project.supabase.co/rest/v1/rpc/read_media_upload',
      'https://project.supabase.co/rest/v1/rpc/claim_media_upload_completion',
      'https://project.supabase.co/rest/v1/rpc/finalize_media_upload_completion',
      'https://project.supabase.co/rest/v1/rpc/claim_stale_media_upload_completion',
      'https://project.supabase.co/rest/v1/rpc/list_media_upload_recovery_parts',
      'https://project.supabase.co/rest/v1/rpc/reset_stale_media_upload_completion',
      'https://project.supabase.co/rest/v1/rpc/list_media_upload_recovery_page',
      'https://project.supabase.co/rest/v1/rpc/claim_media_upload_abort',
      'https://project.supabase.co/rest/v1/rpc/finalize_media_upload_abort',
    ]);
    const resetRequest = fetcher.mock.calls[9]?.[1];
    expect(JSON.parse(String(resetRequest?.body))).toMatchObject({
      p_expected_claimed_at: NOW.toISOString(),
      p_reason: 'r2_upload_missing',
      p_reset_at: NOW.toISOString(),
      p_upload_id: uploadId,
    });
    for (const [, init] of fetcher.mock.calls) {
      const headers = new Headers(init?.headers);
      expect(headers.get('apikey')).toBe(serviceKey);
      expect(headers.get('authorization')).toBe(`Bearer ${serviceKey}`);
    }
  });
});
