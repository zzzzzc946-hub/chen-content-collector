import type {
  ReportIndexEntryDto,
  Role,
} from '../../../packages/shared/src/contracts.js';
import { InvitationError } from './invitations.js';
import type {
  InvitationRecord,
  InvitationStorage,
  MemberRole,
  ReportStatus,
  ShareLinkRecord,
  StoredShareLink,
} from './invitations.js';
import type { IdentityRole, IdentityRoleStore } from './auth.js';
import type {
  CollaborationLinkStorage,
  PortalCollaborationLink,
} from './collaboration-links.js';
import {
  MediaError,
  type AuthorizedMediaRecord,
  type MediaRecord,
  type MediaStorage,
  type MediaUploadPart,
  type MediaUploadRecord,
  type MediaUploadStatus,
} from './media.js';
import {
  ReportError,
  type PublishReportInput,
  type ReadReportInput,
  type CollaborationReportListInput,
  type CollaborationReportReadInput,
  type OwnerReportListInput,
  type ReportRecord,
  type ReportStorage,
  type ReportSummary,
  type RestoreRevisionInput,
  type UpdateCollaborativeFieldInput,
  type UpdateCollaborativeFieldForLinkInput,
} from './reports.js';
import type { ReportItemDto } from '../../../packages/shared/src/contracts.js';
import type { PublisherDeviceAuthStorage } from './publisher-auth.js';

interface SupabaseErrorBody {
  code?: unknown;
  message?: unknown;
}

interface ProfileRow {
  global_role?: unknown;
}

interface CollaborationLinkRow {
  created_at?: unknown;
  created_by?: unknown;
  id?: unknown;
  last_used_at?: unknown;
  revoked_at?: unknown;
  session_version?: unknown;
  token_hash?: unknown;
}

interface CollaborationReportIndexRow {
  daily_date?: unknown;
  id?: unknown;
  item_count?: unknown;
  published_at?: unknown;
}

interface InvitationClaimRow {
  member_role?: unknown;
  report_id?: unknown;
}

interface ShareRow {
  created_at?: unknown;
  created_by?: unknown;
  expires_at?: unknown;
  id?: unknown;
  report_id?: unknown;
  reports?: { status?: unknown } | Array<{ status?: unknown }>;
  revoked_at?: unknown;
  token_hash?: unknown;
}

interface ReportRow {
  access_role?: unknown;
  daily_date?: unknown;
  draft_version?: unknown;
  id?: unknown;
  items?: unknown;
  published_at?: unknown;
  published_version?: unknown;
  status?: unknown;
}

interface MediaRelationRow {
  id?: unknown;
}

interface MediaRow {
  byte_size?: unknown;
  content_type?: unknown;
  daily_date?: unknown;
  id?: unknown;
  object_key?: unknown;
  report_id?: unknown;
}

interface ExpiredMediaRow {
  id?: unknown;
  object_key?: unknown;
}

interface MediaUploadPartRow {
  byte_size?: unknown;
  etag?: unknown;
  part_number?: unknown;
}

interface MediaUploadRow {
  abort_cleanup?: unknown;
  asserted_sha256?: unknown;
  content_type?: unknown;
  created_at?: unknown;
  expected_byte_size?: unknown;
  expires_at?: unknown;
  id?: unknown;
  media_id?: unknown;
  object_key?: unknown;
  parts?: unknown;
  r2_upload_id?: unknown;
  report_id?: unknown;
  report_item_id?: unknown;
  resumed?: unknown;
  sha256_source?: unknown;
  sha256_verification_status?: unknown;
  status?: unknown;
  transition_started_at?: unknown;
}

interface PublisherDeviceAuthRow {
  device_id?: unknown;
}

interface ReportItemRow {
  caption?: unknown;
  id?: unknown;
  local_record_id?: unknown;
  max_daily_card?: unknown;
  max_feedback?: unknown;
  media_id?: unknown;
  media_objects?: MediaRelationRow | MediaRelationRow[];
  report_id?: unknown;
  review_status?: unknown;
  source_url?: unknown;
  title?: unknown;
  version?: unknown;
}

class SupabaseRequestError extends Error {
  constructor(
    readonly status: number,
    readonly responseCode: string,
    readonly responseMessage: string,
  ) {
    super('Supabase storage request failed');
    this.name = 'SupabaseRequestError';
  }
}

function isMemberRole(value: unknown): value is MemberRole {
  return value === 'editor' || value === 'viewer';
}

function isIdentityRole(
  value: unknown,
): value is IdentityRole {
  return value === 'owner' || isMemberRole(value);
}

function isReportStatus(value: unknown): value is ReportStatus {
  return value === 'draft' || value === 'published' || value === 'withdrawn';
}

function requiredString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function nullableDate(value: unknown): Date | null | undefined {
  if (value === null) return null;
  if (typeof value !== 'string') return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function requiredDate(value: unknown): Date | null {
  const parsed = nullableDate(value);
  return parsed instanceof Date ? parsed : null;
}

function safeInteger(value: unknown): number | null {
  const parsed = typeof value === 'string' && /^\d+$/.test(value)
    ? Number(value)
    : value;
  return Number.isSafeInteger(parsed) ? parsed as number : null;
}

function isMediaUploadStatus(value: unknown): value is MediaUploadStatus {
  return (
    value === 'creating'
    || value === 'uploading'
    || value === 'completing'
    || value === 'aborting'
    || value === 'completed'
    || value === 'aborted'
  );
}

function isSha256Source(
  value: unknown,
): value is 'trusted_uploader_assertion' {
  return value === 'trusted_uploader_assertion';
}

function isSha256VerificationStatus(
  value: unknown,
): value is 'not_server_verified' {
  return value === 'not_server_verified';
}

function firstRow<T>(value: T | T[]): T | null {
  if (Array.isArray(value)) return value[0] ?? null;
  return value ?? null;
}

export class SupabaseRestStorage
implements
  CollaborationLinkStorage,
  InvitationStorage,
  IdentityRoleStore,
  MediaStorage,
  PublisherDeviceAuthStorage,
  ReportStorage {
  private readonly baseUrl: string;
  private readonly fetcher: typeof fetch;
  private readonly serviceRoleKey: string;

  constructor(input: {
    fetch?: typeof fetch;
    serviceRoleKey: string;
    supabaseUrl: string;
  }) {
    this.baseUrl = input.supabaseUrl.replace(/\/+$/, '');
    this.fetcher = input.fetch ?? ((request, init) =>
      globalThis.fetch(request, init));
    this.serviceRoleKey = input.serviceRoleKey;
  }

  async getIdentityRole(
    userId: string,
  ): Promise<IdentityRole | null> {
    const rows = await this.request<ProfileRow[]>(
      this.restUrl('profiles', {
        id: `eq.${userId}`,
        limit: '1',
        select: 'global_role',
      }),
    );
    const role = rows[0]?.global_role;
    if (role === undefined) return null;
    if (!isIdentityRole(role)) throw this.storageFailure();
    return role;
  }

  async findPortalLinkById(id: string): Promise<PortalCollaborationLink | null> {
    return this.findPortalCollaborationLink({ id: `eq.${id}` });
  }

  async findPortalLinkByTokenHash(
    tokenHash: string,
  ): Promise<PortalCollaborationLink | null> {
    return this.findPortalCollaborationLink({ token_hash: `eq.${tokenHash}` });
  }

  async replaceActivePortalLink(record: PortalCollaborationLink): Promise<void> {
    try {
      const value = await this.request<
        CollaborationLinkRow | CollaborationLinkRow[]
      >(this.rpcUrl('replace_active_portal_collaboration_link'), {
        body: JSON.stringify({
          p_created_at: record.createdAt.toISOString(),
          p_created_by: record.createdBy,
          p_id: record.id,
          p_token_hash: record.tokenHash,
        }),
        method: 'POST',
      });
      const row = firstRow(value);
      if (!row) throw this.storageFailure();
      this.parsePortalCollaborationLink(row);
    } catch (error) {
      if (error instanceof InvitationError) throw error;
      throw this.storageFailure();
    }
  }

  async revokePortalLink(id: string, revokedAt: Date): Promise<boolean> {
    return this.collaborationBooleanRpc('revoke_portal_collaboration_link', {
      p_id: id,
      p_revoked_at: revokedAt.toISOString(),
    });
  }

  async touchPortalLink(
    id: string,
    expectedSessionVersion: number,
    usedAt: Date,
  ): Promise<boolean> {
    return this.collaborationBooleanRpc('touch_portal_collaboration_link', {
      p_expected_session_version: expectedSessionVersion,
      p_id: id,
      p_used_at: usedAt.toISOString(),
    });
  }

  async claimOwner(input: {
    email: string;
    userId: string;
  }): Promise<'owner'> {
    try {
      await this.request<ProfileRow | ProfileRow[]>(
        this.rpcUrl('claim_owner'),
        {
          body: JSON.stringify({
            claimant_email: input.email,
            claimant_id: input.userId,
          }),
          method: 'POST',
        },
      );
      return 'owner';
    } catch (error) {
      if (
        error instanceof SupabaseRequestError
        && (
          error.responseMessage === 'owner_already_claimed'
          || error.responseMessage === 'owner_email_mismatch'
          || error.responseMessage === 'confirmed_email_required'
        )
      ) {
        throw new InvitationError(
          403,
          error.responseMessage,
          'owner bootstrap denied',
        );
      }
      throw this.storageFailure();
    }
  }

  async authenticatePublisherDeviceAtomic(input: {
    tokenHash: string;
    usedAt: Date;
  }): Promise<{ deviceId: string } | null> {
    try {
      const value = await this.request<
        PublisherDeviceAuthRow | PublisherDeviceAuthRow[]
      >(
        this.rpcUrl('authenticate_publisher_device'),
        {
          body: JSON.stringify({
            p_token_hash: input.tokenHash,
            p_used_at: input.usedAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || row.device_id === null) return null;
      if (typeof row.device_id !== 'string') throw this.storageFailure();
      return { deviceId: row.device_id };
    } catch (error) {
      if (
        error instanceof SupabaseRequestError
        && error.responseMessage === 'publisher_device_unavailable'
      ) {
        return null;
      }
      if (error instanceof SupabaseRequestError) {
        console.error('publisher device authentication storage failure', {
          responseCode: error.responseCode,
          responseMessage: error.responseMessage,
          status: error.status,
        });
      } else {
        console.error('publisher device authentication request failure', error);
      }
      throw this.mediaStorageFailure();
    }
  }

  async insertInvitation(record: InvitationRecord): Promise<void> {
    await this.request<void>(this.restUrl('invitations'), {
      body: JSON.stringify({
        claimed_by: record.claimedBy,
        created_at: record.createdAt.toISOString(),
        created_by: record.createdBy,
        email: record.email,
        expires_at: record.expiresAt.toISOString(),
        id: record.id,
        report_id: record.reportId,
        revoked_at: record.revokedAt?.toISOString() ?? null,
        role: record.role,
        token_hash: record.tokenHash,
        used_at: record.usedAt?.toISOString() ?? null,
      }),
      method: 'POST',
      responseType: 'none',
    });
  }

  async claimInvitationAtomic(input: {
    claimedAt: Date;
    claimantEmail: string;
    claimantId: string;
    tokenHash: string;
  }): Promise<{ reportId: string; role: MemberRole }> {
    try {
      const value = await this.request<InvitationClaimRow | InvitationClaimRow[]>(
        this.rpcUrl('claim_invitation'),
        {
          body: JSON.stringify({
            p_claimant_email: input.claimantEmail,
            p_claimant_id: input.claimantId,
            p_claimed_at: input.claimedAt.toISOString(),
            p_token_hash: input.tokenHash,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (
        !row
        || typeof row.report_id !== 'string'
        || !isMemberRole(row.member_role)
      ) {
        throw this.storageFailure();
      }
      return { reportId: row.report_id, role: row.member_role };
    } catch (error) {
      if (error instanceof SupabaseRequestError) {
        if (error.responseMessage === 'invitation_unavailable') {
          throw new InvitationError(
            410,
            'invitation_unavailable',
            'invitation is expired, used, or revoked',
          );
        }
        if (
          error.responseMessage === 'invitation_email_mismatch'
          || error.responseMessage === 'identity_email_mismatch'
          || error.responseMessage === 'confirmed_email_required'
        ) {
          throw new InvitationError(
            403,
            'invitation_email_mismatch',
            'authenticated email does not match invitation',
          );
        }
      }
      if (error instanceof InvitationError) throw error;
      throw this.storageFailure();
    }
  }

  async revokeInvitation(invitationId: string, revokedAt: Date): Promise<boolean> {
    const rows = await this.request<Array<{ id?: unknown }>>(
      this.restUrl('invitations', {
        id: `eq.${invitationId}`,
        revoked_at: 'is.null',
        select: 'id',
        used_at: 'is.null',
      }),
      {
        body: JSON.stringify({ revoked_at: revokedAt.toISOString() }),
        headers: { prefer: 'return=representation' },
        method: 'PATCH',
      },
    );
    return typeof rows[0]?.id === 'string';
  }

  async insertShareLink(record: StoredShareLink): Promise<void> {
    await this.request<void>(this.restUrl('share_links'), {
      body: JSON.stringify({
        created_at: record.createdAt.toISOString(),
        created_by: record.createdBy,
        expires_at: record.expiresAt?.toISOString() ?? null,
        id: record.id,
        report_id: record.reportId,
        revoked_at: record.revokedAt?.toISOString() ?? null,
        token_hash: record.tokenHash,
      }),
      method: 'POST',
      responseType: 'none',
    });
  }

  async findShareLinkByTokenHash(
    tokenHash: string,
  ): Promise<ShareLinkRecord | null> {
    return this.findShareLink({ token_hash: `eq.${tokenHash}` });
  }

  async findShareLinkById(id: string): Promise<ShareLinkRecord | null> {
    return this.findShareLink({ id: `eq.${id}` });
  }

  async revokeShareLink(shareLinkId: string, revokedAt: Date): Promise<boolean> {
    const rows = await this.request<Array<{ id?: unknown }>>(
      this.restUrl('share_links', {
        id: `eq.${shareLinkId}`,
        revoked_at: 'is.null',
        select: 'id',
      }),
      {
        body: JSON.stringify({ revoked_at: revokedAt.toISOString() }),
        headers: { prefer: 'return=representation' },
        method: 'PATCH',
      },
    );
    return typeof rows[0]?.id === 'string';
  }

  async readReportAtomic(input: ReadReportInput): Promise<ReportRecord> {
    try {
      const value = await this.request<ReportRow | ReportRow[]>(
        this.rpcUrl('read_report_with_access_role'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_read_at: input.readAt.toISOString(),
            p_report_id: input.reportId,
            p_share_link_id: input.shareLinkId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || !Array.isArray(row.items)) {
        throw this.reportStorageFailure();
      }
      if (!isIdentityRole(row.access_role) && row.access_role !== 'public_reader') {
        throw this.reportStorageFailure();
      }
      return {
        accessRole: row.access_role,
        ...this.parseReportSummary(row),
        items: row.items.map((item) =>
          this.parseReportItem(item as ReportItemRow)),
      };
    } catch (error) {
      if (error instanceof SupabaseRequestError) {
        if (error.responseMessage === 'report_not_found') {
          throw new ReportError(
            404,
            'report_not_found',
            'report is unavailable',
          );
        }
        if (error.responseMessage === 'share_unavailable') {
          throw new ReportError(
            410,
            'share_unavailable',
            'share is expired or revoked',
          );
        }
      }
      if (error instanceof ReportError) throw error;
      throw this.reportStorageFailure();
    }
  }

  async listReportsForCollaborationAtomic(
    input: CollaborationReportListInput,
  ): Promise<ReportIndexEntryDto[]> {
    try {
      const value = await this.request<CollaborationReportIndexRow[]>(
        this.rpcUrl('list_published_reports_for_collaboration'),
        {
          body: JSON.stringify({
            p_link_id: input.linkId,
            p_read_at: input.readAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      if (!Array.isArray(value)) throw this.reportStorageFailure();
      return value.map((row) => this.parseCollaborationReportIndex(row));
    } catch (error) {
      throw this.mapCollaborationReportError(error);
    }
  }

  async listReportsForOwnerAtomic(
    input: OwnerReportListInput,
  ): Promise<ReportIndexEntryDto[]> {
    try {
      const value = await this.request<CollaborationReportIndexRow[]>(
        this.rpcUrl('list_published_reports_for_owner'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_read_at: input.readAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      if (!Array.isArray(value)) throw this.reportStorageFailure();
      return value.map((row) => this.parseCollaborationReportIndex(row));
    } catch (error) {
      throw this.mapOwnerReportError(error);
    }
  }

  async readReportForCollaborationAtomic(
    input: CollaborationReportReadInput,
  ): Promise<ReportRecord> {
    try {
      const value = await this.request<ReportRow | ReportRow[]>(
        this.rpcUrl('read_report_for_collaboration'),
        {
          body: JSON.stringify({
            p_link_id: input.linkId,
            p_read_at: input.readAt.toISOString(),
            p_report_id: input.reportId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || !Array.isArray(row.items)) {
        throw this.reportStorageFailure();
      }
      return {
        accessRole: 'collaborator',
        ...this.parseReportSummary(row),
        items: row.items.map((item) =>
          this.parseReportItem(item as ReportItemRow)),
      };
    } catch (error) {
      throw this.mapCollaborationReportError(error);
    }
  }

  async updateCollaborativeFieldForLinkAtomic(
    input: UpdateCollaborativeFieldForLinkInput,
  ): Promise<ReportItemDto> {
    try {
      const value = await this.request<ReportItemRow | ReportItemRow[]>(
        this.rpcUrl('update_collaborative_field_for_link'),
        {
          body: JSON.stringify({
            p_changed_at: input.changedAt.toISOString(),
            p_expected_version: input.expectedVersion,
            p_field_name: input.field,
            p_item_id: input.itemId,
            p_link_id: input.linkId,
            p_value: input.value,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.reportStorageFailure();
      return this.parseReportItem(row);
    } catch (error) {
      throw this.mapCollaborationReportError(error);
    }
  }

  async upsertPublisherDraftAtomic(
    input: Parameters<NonNullable<ReportStorage['upsertPublisherDraftAtomic']>>[0],
  ): ReturnType<NonNullable<ReportStorage['upsertPublisherDraftAtomic']>> {
    try {
      const value = await this.request<ReportRow | ReportRow[]>(
        this.rpcUrl('upsert_publisher_draft'),
        {
          body: JSON.stringify({
            p_daily_date: input.dailyDate,
            p_items: input.items.map((item) => ({
              caption: item.caption,
              item_order: item.itemOrder,
              local_record_id: item.localRecordId,
              max_daily_card: item.maxDailyCard,
              max_feedback: item.maxFeedback,
              review_status: item.reviewStatus,
              source_url: item.sourceUrl,
              title: item.title,
            })),
            p_publisher_device_id: input.publisherDeviceId,
            p_source_table_id: input.sourceTableId,
            p_upserted_at: input.upsertedAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || !Array.isArray(row.items)) {
        throw this.reportStorageFailure();
      }
      return {
        accessRole: 'owner',
        ...this.parseReportSummary(row),
        items: row.items.map((item) =>
          this.parseReportItem(item as ReportItemRow)),
      };
    } catch (error) {
      throw this.mapPublisherReportError(error);
    }
  }

  async authorizeMediaReadAtomic(input: {
    actorId: string | null;
    mediaId: string;
    publicReportId: string | null;
    readAt: Date;
    shareLinkId: string | null;
  }): Promise<AuthorizedMediaRecord> {
    try {
      const value = await this.request<MediaRow | MediaRow[]>(
        this.rpcUrl('authorize_media_read'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_media_id: input.mediaId,
            p_public_report_id: input.publicReportId,
            p_read_at: input.readAt.toISOString(),
            p_share_link_id: input.shareLinkId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.mediaStorageFailure();
      const dailyDate = requiredString(row.daily_date);
      if (!dailyDate) throw this.mediaStorageFailure();
      return { ...this.parseMedia(row), dailyDate };
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async authorizeMediaForCollaborationAtomic(input: {
    linkId: string;
    mediaId: string;
    readAt: Date;
  }): Promise<AuthorizedMediaRecord> {
    try {
      const value = await this.request<MediaRow | MediaRow[]>(
        this.rpcUrl('authorize_media_for_collaboration'),
        {
          body: JSON.stringify({
            p_link_id: input.linkId,
            p_media_id: input.mediaId,
            p_read_at: input.readAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.mediaStorageFailure();
      const dailyDate = requiredString(row.daily_date);
      if (!dailyDate) throw this.mediaStorageFailure();
      return { ...this.parseMedia(row), dailyDate };
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async beginMediaUploadAtomic(input: Parameters<
    MediaStorage['beginMediaUploadAtomic']
  >[0]): ReturnType<MediaStorage['beginMediaUploadAtomic']> {
    try {
      const value = await this.request<MediaUploadRow | MediaUploadRow[]>(
        this.rpcUrl('begin_media_upload'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_content_type: input.contentType,
            p_expected_byte_size: input.expectedByteSize,
            p_publisher_device_id: input.publisherDeviceId,
            p_report_id: input.reportId,
            p_report_item_id: input.reportItemId,
            p_started_at: input.startedAt.toISOString(),
            p_upload_id: input.uploadId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || typeof row.resumed !== 'boolean') {
        throw this.mediaStorageFailure();
      }
      return { ...this.parseMediaUpload(row), resumed: row.resumed };
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async claimExpiredMediaObjects(input: Parameters<
    MediaStorage['claimExpiredMediaObjects']
  >[0]): ReturnType<MediaStorage['claimExpiredMediaObjects']> {
    try {
      const rows = await this.request<ExpiredMediaRow[]>(
        this.rpcUrl('claim_expired_media_objects'),
        {
          body: JSON.stringify({
            p_limit: input.limit,
            p_now: input.now.toISOString(),
          }),
          method: 'POST',
        },
      );
      return rows.map((row) => {
        const id = requiredString(row.id);
        const objectKey = requiredString(row.object_key);
        if (!id || !objectKey) throw this.mediaStorageFailure();
        return { id, objectKey };
      });
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async attachMediaUploadAtomic(input: Parameters<
    MediaStorage['attachMediaUploadAtomic']
  >[0]): ReturnType<MediaStorage['attachMediaUploadAtomic']> {
    return this.mutateMediaUpload('attach_media_upload', {
      p_actor_id: input.actorId,
      p_attached_at: input.attachedAt.toISOString(),
      p_publisher_device_id: input.publisherDeviceId,
      p_r2_upload_id: input.r2UploadId,
      p_upload_id: input.uploadId,
    });
  }

  async getMediaUploadAtomic(input: Parameters<
    MediaStorage['getMediaUploadAtomic']
  >[0]): ReturnType<MediaStorage['getMediaUploadAtomic']> {
    return (await this.readMediaUpload(input)).upload;
  }

  async listMediaUploadPartsAtomic(input: Parameters<
    MediaStorage['listMediaUploadPartsAtomic']
  >[0]): ReturnType<MediaStorage['listMediaUploadPartsAtomic']> {
    return (await this.readMediaUpload(input)).parts;
  }

  async listMediaUploadRecoveryPartsAtomic(input: Parameters<
    MediaStorage['listMediaUploadRecoveryPartsAtomic']
  >[0]): ReturnType<MediaStorage['listMediaUploadRecoveryPartsAtomic']> {
    try {
      const rows = await this.request<MediaUploadPartRow[]>(
        this.rpcUrl('list_media_upload_recovery_parts'),
        {
          body: JSON.stringify({ p_upload_id: input.uploadId }),
          method: 'POST',
        },
      );
      return rows.map((row) => this.parseMediaUploadPart(row));
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async recordMediaUploadPartAtomic(input: Parameters<
    MediaStorage['recordMediaUploadPartAtomic']
  >[0]): ReturnType<MediaStorage['recordMediaUploadPartAtomic']> {
    try {
      const value = await this.request<
        MediaUploadPartRow | MediaUploadPartRow[]
      >(
        this.rpcUrl('record_media_upload_part'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_byte_size: input.byteSize,
            p_etag: input.etag,
            p_part_number: input.partNumber,
            p_publisher_device_id: input.publisherDeviceId,
            p_upload_id: input.uploadId,
            p_uploaded_at: input.uploadedAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.mediaStorageFailure();
      return this.parseMediaUploadPart(row);
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async claimMediaUploadCompletionAtomic(input: Parameters<
    MediaStorage['claimMediaUploadCompletionAtomic']
  >[0]): ReturnType<MediaStorage['claimMediaUploadCompletionAtomic']> {
    return this.mutateMediaUpload('claim_media_upload_completion', {
      p_actor_id: input.actorId,
      p_asserted_sha256: input.assertedSha256,
      p_claimed_at: input.claimedAt.toISOString(),
      p_publisher_device_id: input.publisherDeviceId,
      p_upload_id: input.uploadId,
    });
  }

  async claimStaleMediaUploadCompletionAtomic(input: Parameters<
    MediaStorage['claimStaleMediaUploadCompletionAtomic']
  >[0]): ReturnType<MediaStorage['claimStaleMediaUploadCompletionAtomic']> {
    return this.mutateMediaUpload('claim_stale_media_upload_completion', {
      p_claimed_at: input.claimedAt.toISOString(),
      p_stale_before: input.staleBefore.toISOString(),
      p_upload_id: input.uploadId,
    });
  }

  async finalizeMediaUploadCompletionAtomic(input: Parameters<
    MediaStorage['finalizeMediaUploadCompletionAtomic']
  >[0]): ReturnType<MediaStorage['finalizeMediaUploadCompletionAtomic']> {
    try {
      const value = await this.request<MediaRow | MediaRow[]>(
        this.rpcUrl('finalize_media_upload_completion'),
        {
          body: JSON.stringify({
            p_completed_at: input.completedAt.toISOString(),
            p_upload_id: input.uploadId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.mediaStorageFailure();
      return this.parseMedia(row);
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async finishMediaObjectPurge(input: Parameters<
    MediaStorage['finishMediaObjectPurge']
  >[0]): ReturnType<MediaStorage['finishMediaObjectPurge']> {
    try {
      await this.request<void>(this.rpcUrl('finish_media_object_purge'), {
        body: JSON.stringify({
          p_error: input.error,
          p_media_id: input.mediaId,
          p_purged_at: input.purgedAt?.toISOString() ?? null,
        }),
        method: 'POST',
        responseType: 'none',
      });
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async claimMediaUploadAbortAtomic(input: Parameters<
    MediaStorage['claimMediaUploadAbortAtomic']
  >[0]): ReturnType<MediaStorage['claimMediaUploadAbortAtomic']> {
    return this.mutateMediaUpload('claim_media_upload_abort', {
      p_actor_id: input.actorId,
      p_claimed_at: input.claimedAt.toISOString(),
      p_cleanup: input.cleanup,
      p_publisher_device_id: input.publisherDeviceId,
      p_upload_id: input.uploadId,
    });
  }

  async finalizeMediaUploadAbortAtomic(input: Parameters<
    MediaStorage['finalizeMediaUploadAbortAtomic']
  >[0]): ReturnType<MediaStorage['finalizeMediaUploadAbortAtomic']> {
    return this.mutateMediaUpload('finalize_media_upload_abort', {
      p_aborted_at: input.abortedAt.toISOString(),
      p_upload_id: input.uploadId,
    });
  }

  async listMediaUploadRecoveryPage(input: Parameters<
    MediaStorage['listMediaUploadRecoveryPage']
  >[0]): ReturnType<MediaStorage['listMediaUploadRecoveryPage']> {
    try {
      const rows = await this.request<MediaUploadRow[]>(
        this.rpcUrl('list_media_upload_recovery_page'),
        {
          body: JSON.stringify({
            p_after_id: input.afterId,
            p_limit: input.limit,
            p_stale_at: input.staleAt.toISOString(),
          }),
          method: 'POST',
        },
      );
      return rows.map((row) => this.parseMediaUpload(row));
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  async resetStaleMediaUploadCompletionAtomic(input: Parameters<
    MediaStorage['resetStaleMediaUploadCompletionAtomic']
  >[0]): ReturnType<MediaStorage['resetStaleMediaUploadCompletionAtomic']> {
    return this.mutateMediaUpload('reset_stale_media_upload_completion', {
      p_expected_claimed_at: input.expectedClaimedAt.toISOString(),
      p_reason: input.reason,
      p_reset_at: input.resetAt.toISOString(),
      p_upload_id: input.uploadId,
    });
  }

  async updateCollaborativeFieldAtomic(
    input: UpdateCollaborativeFieldInput,
  ): Promise<ReportItemDto> {
    try {
      const value = await this.request<ReportItemRow | ReportItemRow[]>(
        this.rpcUrl('update_collaborative_field'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_changed_at: input.changedAt.toISOString(),
            p_expected_version: input.expectedVersion,
            p_field_name: input.field,
            p_item_id: input.itemId,
            p_value: input.value,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.reportStorageFailure();
      return this.parseReportItem(row);
    } catch (error) {
      if (error instanceof SupabaseRequestError) {
        if (error.responseMessage === 'stale_version') {
          throw new ReportError(409, 'stale_version', 'item version is stale');
        }
        if (error.responseMessage === 'field_not_editable') {
          throw new ReportError(
            403,
            'field_not_editable',
            'field is not collaboratively editable',
          );
        }
        if (error.responseMessage === 'edit_forbidden') {
          throw new ReportError(403, 'edit_forbidden', 'report is read-only');
        }
        if (
          error.responseMessage === 'item_not_found'
          || error.responseMessage === 'access_denied'
        ) {
          throw new ReportError(404, 'item_not_found', 'item is unavailable');
        }
      }
      if (error instanceof ReportError) throw error;
      throw this.reportStorageFailure();
    }
  }

  async publishReportAtomic(
    input: PublishReportInput,
  ): Promise<ReportSummary> {
    try {
      const value = await this.request<ReportRow | ReportRow[]>(
        this.rpcUrl('publish_report'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_published_at: input.publishedAt.toISOString(),
            p_report_id: input.reportId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.reportStorageFailure();
      return this.parseReportSummary(row);
    } catch (error) {
      if (error instanceof SupabaseRequestError) {
        if (error.responseMessage === 'owner_required') {
          throw new ReportError(403, 'owner_required', 'owner access required');
        }
        if (error.responseMessage === 'report_not_found') {
          throw new ReportError(404, 'report_not_found', 'report is unavailable');
        }
        if (error.responseMessage === 'report_not_ready') {
          throw new ReportError(
            409,
            'report_not_ready',
            'report media is incomplete',
          );
        }
      }
      if (error instanceof ReportError) throw error;
      throw this.reportStorageFailure();
    }
  }

  async publishPublisherReportAtomic(
    input: Parameters<NonNullable<ReportStorage['publishPublisherReportAtomic']>>[0],
  ): ReturnType<NonNullable<ReportStorage['publishPublisherReportAtomic']>> {
    try {
      const value = await this.request<ReportRow | ReportRow[]>(
        this.rpcUrl('publish_publisher_report'),
        {
          body: JSON.stringify({
            p_expected_draft_version: input.expectedDraftVersion,
            p_published_at: input.publishedAt.toISOString(),
            p_publisher_device_id: input.publisherDeviceId,
            p_report_id: input.reportId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.reportStorageFailure();
      return this.parseReportSummary(row);
    } catch (error) {
      throw this.mapPublisherReportError(error);
    }
  }

  async restoreRevisionAtomic(
    input: RestoreRevisionInput,
  ): Promise<ReportItemDto> {
    try {
      const value = await this.request<ReportItemRow | ReportItemRow[]>(
        this.rpcUrl('restore_revision'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_expected_version: input.expectedVersion,
            p_restored_at: input.restoredAt.toISOString(),
            p_revision_id: input.revisionId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.reportStorageFailure();
      return this.parseReportItem(row);
    } catch (error) {
      if (error instanceof SupabaseRequestError) {
        if (error.responseMessage === 'owner_required') {
          throw new ReportError(403, 'owner_required', 'owner access required');
        }
        if (error.responseMessage === 'stale_version') {
          throw new ReportError(409, 'stale_version', 'item version is stale');
        }
        if (
          error.responseMessage === 'revision_not_found'
          || error.responseMessage === 'item_not_found'
        ) {
          throw new ReportError(
            404,
            'revision_not_found',
            'revision is unavailable',
          );
        }
      }
      if (error instanceof ReportError) throw error;
      throw this.reportStorageFailure();
    }
  }

  private async findShareLink(
    filter: Record<string, string>,
  ): Promise<ShareLinkRecord | null> {
    const rows = await this.request<ShareRow[]>(
      this.restUrl('share_links', {
        ...filter,
        limit: '1',
        select:
          'id,report_id,token_hash,expires_at,revoked_at,created_by,created_at,reports!inner(status)',
      }),
    );
    if (!rows[0]) return null;
    return this.parseShare(rows[0]);
  }

  private async findPortalCollaborationLink(
    filter: Record<string, string>,
  ): Promise<PortalCollaborationLink | null> {
    try {
      const rows = await this.request<CollaborationLinkRow[]>(
        this.restUrl('portal_collaboration_links', {
          ...filter,
          limit: '1',
          select:
            'id,token_hash,session_version,created_by,created_at,last_used_at,revoked_at',
        }),
      );
      if (!Array.isArray(rows)) throw this.storageFailure();
      return rows[0] ? this.parsePortalCollaborationLink(rows[0]) : null;
    } catch (error) {
      if (error instanceof InvitationError) throw error;
      throw this.storageFailure();
    }
  }

  private parsePortalCollaborationLink(
    row: CollaborationLinkRow,
  ): PortalCollaborationLink {
    const id = requiredString(row.id);
    const tokenHash = requiredString(row.token_hash);
    const createdBy = requiredString(row.created_by);
    const createdAt = nullableDate(row.created_at);
    const lastUsedAt = nullableDate(row.last_used_at);
    const revokedAt = nullableDate(row.revoked_at);
    const sessionVersion = safeInteger(row.session_version);
    if (
      !id
      || !tokenHash
      || !createdBy
      || !(createdAt instanceof Date)
      || lastUsedAt === undefined
      || revokedAt === undefined
      || sessionVersion === null
      || sessionVersion <= 0
    ) {
      throw this.storageFailure();
    }
    return {
      createdAt,
      createdBy,
      id,
      lastUsedAt,
      revokedAt,
      sessionVersion,
      tokenHash,
    };
  }

  private parseCollaborationReportIndex(
    row: CollaborationReportIndexRow,
  ): ReportIndexEntryDto {
    const id = requiredString(row.id);
    const dailyDate = requiredString(row.daily_date);
    const publishedAt = requiredDate(row.published_at);
    const itemCount = safeInteger(row.item_count);
    if (
      !id
      || !dailyDate
      || !publishedAt
      || itemCount === null
      || itemCount < 0
    ) {
      throw this.reportStorageFailure();
    }
    return {
      dailyDate,
      id,
      itemCount,
      publishedAt: publishedAt.toISOString(),
    };
  }

  private parseShare(row: ShareRow): ShareLinkRecord {
    const report = Array.isArray(row.reports) ? row.reports[0] : row.reports;
    const id = requiredString(row.id);
    const reportId = requiredString(row.report_id);
    const tokenHash = requiredString(row.token_hash);
    const createdBy = requiredString(row.created_by);
    const createdAt = nullableDate(row.created_at);
    const expiresAt = nullableDate(row.expires_at);
    const revokedAt = nullableDate(row.revoked_at);
    const reportStatus = report?.status;
    if (
      !id
      || !reportId
      || !tokenHash
      || !createdBy
      || !(createdAt instanceof Date)
      || expiresAt === undefined
      || revokedAt === undefined
      || !isReportStatus(reportStatus)
    ) {
      throw this.storageFailure();
    }
    return {
      createdAt,
      createdBy,
      expiresAt,
      id,
      reportId,
      reportStatus,
      revokedAt,
      tokenHash,
    };
  }

  private parseReportSummary(row: ReportRow): ReportSummary {
    const id = requiredString(row.id);
    const dailyDate = requiredString(row.daily_date);
    const publishedAt = nullableDate(row.published_at);
    if (
      !id
      || !dailyDate
      || !isReportStatus(row.status)
      || !Number.isInteger(row.draft_version)
      || !Number.isInteger(row.published_version)
      || publishedAt === undefined
    ) {
      throw this.reportStorageFailure();
    }
    return {
      dailyDate,
      draftVersion: row.draft_version as number,
      id,
      publishedAt: publishedAt?.toISOString() ?? null,
      publishedVersion: row.published_version as number,
      status: row.status,
    };
  }

  private parseReportItem(row: ReportItemRow): ReportItemDto {
    const mediaRelation = Array.isArray(row.media_objects)
      ? row.media_objects[0]
      : row.media_objects;
    const mediaIdValue = row.media_id ?? mediaRelation?.id ?? null;
    if (
      typeof row.id !== 'string'
      || typeof row.local_record_id !== 'string'
      || typeof row.title !== 'string'
      || typeof row.caption !== 'string'
      || typeof row.source_url !== 'string'
      || typeof row.max_daily_card !== 'string'
      || typeof row.max_feedback !== 'string'
      || typeof row.review_status !== 'string'
      || !Number.isInteger(row.version)
      || (mediaIdValue !== null && typeof mediaIdValue !== 'string')
    ) {
      throw this.reportStorageFailure();
    }
    return {
      caption: row.caption,
      id: row.id,
      localRecordId: row.local_record_id,
      maxDailyCard: row.max_daily_card,
      maxFeedback: row.max_feedback,
      mediaId: mediaIdValue,
      reviewStatus: row.review_status,
      sourceUrl: row.source_url,
      title: row.title,
      version: row.version as number,
    };
  }

  private parseMedia(row: MediaRow): MediaRecord {
    const id = requiredString(row.id);
    const reportId = requiredString(row.report_id);
    const objectKey = requiredString(row.object_key);
    const contentType = requiredString(row.content_type);
    const byteSize = safeInteger(row.byte_size);
    if (!id || !reportId || !objectKey || !contentType || byteSize === null) {
      throw this.mediaStorageFailure();
    }
    return { byteSize, contentType, id, objectKey, reportId };
  }

  private parseMediaUpload(row: MediaUploadRow): MediaUploadRecord {
    const id = requiredString(row.id);
    const reportId = requiredString(row.report_id);
    const reportItemId = requiredString(row.report_item_id);
    const mediaId = requiredString(row.media_id);
    const objectKey = requiredString(row.object_key);
    const contentType = requiredString(row.content_type);
    const expectedByteSize = safeInteger(row.expected_byte_size);
    const createdAt = requiredDate(row.created_at);
    const expiresAt = requiredDate(row.expires_at);
    const assertedSha256 = row.asserted_sha256 === null
      ? null
      : requiredString(row.asserted_sha256);
    const sha256Source = row.sha256_source === null
      ? null
      : isSha256Source(row.sha256_source)
      ? row.sha256_source
      : undefined;
    const sha256VerificationStatus = row.sha256_verification_status === null
      ? null
      : isSha256VerificationStatus(row.sha256_verification_status)
      ? row.sha256_verification_status
      : undefined;
    const transitionStartedAt = nullableDate(row.transition_started_at);
    const abortCleanup = row.abort_cleanup === null
      ? null
      : typeof row.abort_cleanup === 'boolean'
      ? row.abort_cleanup
      : undefined;
    const r2UploadId = row.r2_upload_id === null
      ? null
      : requiredString(row.r2_upload_id);
    if (
      !id
      || !reportId
      || !reportItemId
      || !mediaId
      || !objectKey
      || !contentType
      || expectedByteSize === null
      || !createdAt
      || !expiresAt
      || assertedSha256 === undefined
      || sha256Source === undefined
      || sha256VerificationStatus === undefined
      || transitionStartedAt === undefined
      || abortCleanup === undefined
      || r2UploadId === undefined
      || !isMediaUploadStatus(row.status)
    ) {
      throw this.mediaStorageFailure();
    }
    return {
      abortCleanup,
      assertedSha256,
      contentType,
      createdAt,
      expectedByteSize,
      expiresAt,
      id,
      mediaId,
      objectKey,
      r2UploadId,
      reportId,
      reportItemId,
      sha256Source,
      sha256VerificationStatus,
      status: row.status,
      transitionStartedAt,
    };
  }

  private parseMediaUploadPart(row: MediaUploadPartRow): MediaUploadPart {
    const partNumber = safeInteger(row.part_number);
    const byteSize = safeInteger(row.byte_size);
    const etag = requiredString(row.etag);
    if (partNumber === null || byteSize === null || !etag) {
      throw this.mediaStorageFailure();
    }
    return { byteSize, etag, partNumber };
  }

  private async readMediaUpload(input: {
    actorId: string | null;
    publisherDeviceId: string | null;
    uploadId: string;
  }): Promise<{ parts: MediaUploadPart[]; upload: MediaUploadRecord }> {
    try {
      const value = await this.request<MediaUploadRow | MediaUploadRow[]>(
        this.rpcUrl('read_media_upload'),
        {
          body: JSON.stringify({
            p_actor_id: input.actorId,
            p_publisher_device_id: input.publisherDeviceId,
            p_upload_id: input.uploadId,
          }),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row || !Array.isArray(row.parts)) {
        throw this.mediaStorageFailure();
      }
      return {
        parts: row.parts.map((part) =>
          this.parseMediaUploadPart(part as MediaUploadPartRow)),
        upload: this.parseMediaUpload(row),
      };
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  private async mutateMediaUpload(
    functionName:
      | 'attach_media_upload'
      | 'claim_media_upload_abort'
      | 'claim_media_upload_completion'
      | 'claim_stale_media_upload_completion'
      | 'finalize_media_upload_abort'
      | 'reset_stale_media_upload_completion',
    body: Record<string, unknown>,
  ): Promise<MediaUploadRecord> {
    try {
      const value = await this.request<MediaUploadRow | MediaUploadRow[]>(
        this.rpcUrl(functionName),
        {
          body: JSON.stringify(body),
          method: 'POST',
        },
      );
      const row = firstRow(value);
      if (!row) throw this.mediaStorageFailure();
      return this.parseMediaUpload(row);
    } catch (error) {
      throw this.mapMediaError(error);
    }
  }

  private restUrl(
    resource: string,
    query: Record<string, string> = {},
  ): string {
    const url = new URL(`/rest/v1/${resource}`, this.baseUrl);
    for (const [key, value] of Object.entries(query)) {
      url.searchParams.set(key, value);
    }
    return url.toString();
  }

  private rpcUrl(functionName: string): string {
    return new URL(`/rest/v1/rpc/${functionName}`, this.baseUrl).toString();
  }

  private async request<T>(
    url: string,
    input: RequestInit & { responseType?: 'json' | 'none' } = {},
  ): Promise<T> {
    const { responseType, ...requestInit } = input;
    const headers = new Headers(input.headers);
    headers.set('apikey', this.serviceRoleKey);
    headers.set('authorization', `Bearer ${this.serviceRoleKey}`);
    if (input.body) headers.set('content-type', 'application/json');

    const response = await this.fetcher(url, {
      ...requestInit,
      headers,
    });
    if (!response.ok) {
      let body: SupabaseErrorBody = {};
      try {
        body = await response.json() as SupabaseErrorBody;
      } catch {
        // Keep provider details out of public errors.
      }
      throw new SupabaseRequestError(
        response.status,
        typeof body.code === 'string' ? body.code : '',
        typeof body.message === 'string' ? body.message : '',
      );
    }
    if (responseType === 'none' || response.status === 204) {
      return undefined as T;
    }
    return await response.json() as T;
  }

  private async collaborationBooleanRpc(
    functionName: string,
    body: Record<string, unknown>,
  ): Promise<boolean> {
    try {
      const value = await this.request<unknown>(this.rpcUrl(functionName), {
        body: JSON.stringify(body),
        method: 'POST',
      });
      if (typeof value !== 'boolean') throw this.storageFailure();
      return value;
    } catch (error) {
      if (error instanceof InvitationError) throw error;
      throw this.storageFailure();
    }
  }

  private storageFailure(): InvitationError {
    return new InvitationError(500, 'storage_error', 'storage request failed');
  }

  private reportStorageFailure(): ReportError {
    return new ReportError(500, 'storage_error', 'storage request failed');
  }

  private mediaStorageFailure(): MediaError {
    return new MediaError(500, 'storage_error', 'storage request failed');
  }

  private mapPublisherReportError(error: unknown): ReportError {
    if (error instanceof ReportError) return error;
    if (error instanceof SupabaseRequestError) {
      if (error.responseMessage === 'stale_version') {
        return new ReportError(409, 'stale_version', 'item version is stale');
      }
      if (error.responseMessage === 'report_not_ready') {
        return new ReportError(
          409,
          'report_not_ready',
          'report media is incomplete',
        );
      }
      if (error.responseMessage === 'publisher_device_unavailable') {
        return new ReportError(
          403,
          'publisher_device_unavailable',
          'publisher device is unavailable',
        );
      }
      if (error.responseMessage === 'report_not_found') {
        return new ReportError(404, 'report_not_found', 'report is unavailable');
      }
    }
    return this.reportStorageFailure();
  }

  private mapCollaborationReportError(error: unknown): ReportError {
    if (error instanceof ReportError) return error;
    if (error instanceof SupabaseRequestError) {
      if (error.responseMessage === 'collaboration_link_unavailable') {
        return new ReportError(
          410,
          'collaboration_link_unavailable',
          'collaboration link is unavailable',
        );
      }
      if (error.responseMessage === 'report_not_found') {
        return new ReportError(404, 'report_not_found', 'report is unavailable');
      }
      if (error.responseMessage === 'item_not_found') {
        return new ReportError(404, 'item_not_found', 'item is unavailable');
      }
      if (error.responseMessage === 'stale_version') {
        return new ReportError(409, 'stale_version', 'item version is stale');
      }
      if (error.responseMessage === 'field_not_editable') {
        return new ReportError(
          403,
          'field_not_editable',
          'field is not collaboratively editable',
        );
      }
      if (error.responseMessage === 'collaborative_value_too_long') {
        return new ReportError(
          400,
          'collaborative_value_too_long',
          'collaborative field value is too long',
        );
      }
      if (error.responseMessage === 'invalid_collaboration_input') {
        return new ReportError(
          400,
          'invalid_collaboration_input',
          'collaboration input is invalid',
        );
      }
    }
    return this.reportStorageFailure();
  }

  private mapOwnerReportError(error: unknown): ReportError {
    if (error instanceof ReportError) return error;
    if (error instanceof SupabaseRequestError) {
      if (error.responseMessage === 'owner_required') {
        return new ReportError(403, 'owner_required', 'owner access required');
      }
      if (error.responseMessage === 'invalid_owner_report_input') {
        return new ReportError(
          400,
          'invalid_owner_report_input',
          'owner report input is invalid',
        );
      }
    }
    return this.reportStorageFailure();
  }

  private mapMediaError(error: unknown): MediaError {
    if (error instanceof MediaError) return error;
    if (error instanceof SupabaseRequestError) {
      if (error.responseMessage === 'collaboration_link_unavailable') {
        return new MediaError(
          410,
          'collaboration_link_unavailable',
          'collaboration link is unavailable',
        );
      }
      if (error.responseMessage === 'share_unavailable') {
        return new MediaError(
          410,
          'share_unavailable',
          'share is expired or revoked',
        );
      }
      if (
        error.responseMessage === 'media_not_found'
        || error.responseMessage === 'report_item_not_found'
        || error.responseMessage === 'upload_not_found'
        || error.responseMessage === 'access_denied'
      ) {
        return new MediaError(404, 'media_not_found', 'media is unavailable');
      }
      if (
        error.responseMessage === 'owner_required'
        || error.responseMessage === 'publisher_device_unavailable'
      ) {
        return new MediaError(403, 'owner_required', 'owner access required');
      }
      if (
        error.responseMessage === 'upload_in_progress'
        || error.responseMessage === 'upload_not_active'
        || error.responseMessage === 'upload_completed'
        || error.responseMessage === 'upload_completing'
        || error.responseMessage === 'upload_aborting'
        || error.responseMessage === 'upload_expired'
        || error.responseMessage === 'upload_incomplete'
        || error.responseMessage === 'integrity_assertion_mismatch'
      ) {
        return new MediaError(
          409,
          error.responseMessage,
          'media upload state is incompatible',
        );
      }
      if (
        error.responseMessage === 'invalid_upload'
        || error.responseMessage === 'invalid_upload_part'
        || error.responseMessage === 'invalid_sha256'
      ) {
        return new MediaError(
          400,
          error.responseMessage,
          'media upload request is invalid',
        );
      }
    }
    return this.mediaStorageFailure();
  }
}

export function createSupabaseRestStorage(input: {
  fetch?: typeof fetch;
  serviceRoleKey: string;
  supabaseUrl: string;
}): SupabaseRestStorage {
  return new SupabaseRestStorage(input);
}
