import { describe, expect, it, vi } from 'vitest';
import type {
  CollaborativeField,
  ReportItemDto,
  Role,
} from '../../../packages/shared/src/contracts';
import type {
  IdentityRole,
  IdentityRoleStore,
  IdentityVerifier,
  VerifiedIdentity,
} from '../src/auth';
import { createWorkerApp } from '../src/index';
import {
  createShareLink,
  exchangeShareToken,
  type InvitationRecord,
  type InvitationStorage,
  type MemberRole,
  type ShareLinkRecord,
  type StoredShareLink,
} from '../src/invitations';
import {
  applyReportMediaPolicy,
  getReportForCollaborator,
  getReportForIdentity,
  listReportsForCollaborator,
  listReportsForOwner,
  patchItemForCollaborator,
  ReportError,
  toShanghaiDate,
  type PublisherDraftReportInput,
  type PublisherPublishReportInput,
  type PublishReportInput,
  type ReadReportInput,
  type ReportRecord,
  type ReportStorage,
  type RestoreRevisionInput,
  type UpdateCollaborativeFieldInput,
} from '../src/reports';
import type { CollaborationSession } from '../src/collaboration-links';
import { SupabaseRestStorage } from '../src/storage';

const OWNER_ID = '00000000-0000-0000-0000-000000000001';
const EDITOR_ID = '00000000-0000-0000-0000-000000000002';
const VIEWER_ID = '00000000-0000-0000-0000-000000000003';
const CROSS_REPORT_ID = '00000000-0000-0000-0000-000000000004';
const REPORT_ID = '00000000-0000-0000-0000-000000000101';
const OTHER_REPORT_ID = '00000000-0000-0000-0000-000000000102';
const ITEM_ID = '00000000-0000-0000-0000-000000000201';
const OTHER_ITEM_ID = '00000000-0000-0000-0000-000000000202';
const MEDIA_ID = '00000000-0000-0000-0000-000000000301';
const SHARE_ID = '00000000-0000-0000-0000-000000000401';
const PUBLISHER_DEVICE_ID = '00000000-0000-0000-0000-000000000901';
const NOW = new Date('2026-07-10T09:00:00.000Z');
const SESSION_SECRET = 'test-only-public-session-secret-with-32-bytes';
const MEDIA_SESSION_SECRET = 'test-only-media-session-secret-with-32-bytes';
const APP_ORIGIN = 'https://daily.example.com';
const COLLABORATION_LINK_ID = '00000000-0000-0000-0000-000000000501';
const collaborationSession: CollaborationSession = {
  linkId: COLLABORATION_LINK_ID,
  role: 'collaborator',
  sessionVersion: 2,
};

interface MemoryRevision {
  actorId: string;
  fieldName: CollaborativeField;
  id: number;
  itemVersion: number;
  newValue: string;
  oldValue: string;
  reportItemId: string;
}

interface MemoryAudit {
  actorId: string;
  eventType: string;
  reportId: string;
}

class MemoryStorage
implements ReportStorage, InvitationStorage, IdentityRoleStore {
  afterMemberLookup: (() => void) | null = null;
  afterShareLookup: (() => void) | null = null;
  readonly audits: MemoryAudit[] = [];
  readonly invitations: InvitationRecord[] = [];
  readonly mediaReady = new Map<string, boolean>([
    [ITEM_ID, true],
    [OTHER_ITEM_ID, true],
  ]);
  readonly members = new Map<string, MemberRole>([
    [`${REPORT_ID}:${EDITOR_ID}`, 'editor'],
    [`${REPORT_ID}:${VIEWER_ID}`, 'viewer'],
    [`${OTHER_REPORT_ID}:${CROSS_REPORT_ID}`, 'editor'],
  ]);
  readonly reports = new Map<string, ReportRecord>([
    [REPORT_ID, {
      accessRole: 'owner',
      dailyDate: '2026-07-10',
      draftVersion: 1,
      id: REPORT_ID,
      items: [{
        caption: 'source caption',
        id: ITEM_ID,
        localRecordId: 'local-1',
        maxDailyCard: 'old card',
        maxFeedback: '',
        mediaId: MEDIA_ID,
        reviewStatus: 'pending',
        sourceUrl: 'https://example.com/source',
        title: 'source title',
        version: 1,
      }],
      publishedAt: null,
      publishedVersion: 0,
      status: 'draft',
    }],
    [OTHER_REPORT_ID, {
      accessRole: 'owner',
      dailyDate: '2026-07-09',
      draftVersion: 3,
      id: OTHER_REPORT_ID,
      items: [{
        caption: 'other caption',
        id: OTHER_ITEM_ID,
        localRecordId: 'local-2',
        maxDailyCard: 'other card',
        maxFeedback: '',
        mediaId: MEDIA_ID,
        reviewStatus: 'approved',
        sourceUrl: 'https://example.com/other',
        title: 'other title',
        version: 3,
      }],
      publishedAt: NOW.toISOString(),
      publishedVersion: 3,
      status: 'published',
    }],
  ]);
  readonly publications = new Map<string, ReportItemDto[]>([
    [`${OTHER_REPORT_ID}:3`, [{
      caption: 'other caption',
      id: OTHER_ITEM_ID,
      localRecordId: 'local-2',
      maxDailyCard: 'other card',
      maxFeedback: '',
      mediaId: MEDIA_ID,
      reviewStatus: 'approved',
      sourceUrl: 'https://example.com/other',
      title: 'other title',
      version: 3,
    }]],
  ]);
  readonly revisions: MemoryRevision[] = [];
  readonly roles = new Map<string, IdentityRole>([
    [OWNER_ID, 'owner'],
    [EDITOR_ID, 'viewer'],
    [VIEWER_ID, 'viewer'],
    [CROSS_REPORT_ID, 'editor'],
  ]);
  readonly shares: StoredShareLink[] = [];

  async getIdentityRole(
    userId: string,
  ): Promise<IdentityRole | null> {
    return this.roles.get(userId) ?? null;
  }

  async claimOwner(): Promise<'owner'> {
    return 'owner';
  }

  async readReportAtomic(input: ReadReportInput): Promise<ReportRecord> {
    const report = this.reports.get(input.reportId);
    if (!report) {
      throw new ReportError(404, 'report_not_found', 'report is unavailable');
    }

    if (input.actorId) {
      let role = this.roles.get(input.actorId) === 'owner'
        ? 'owner'
        : this.members.get(`${input.reportId}:${input.actorId}`) ?? null;
      this.afterMemberLookup?.();
      if (role !== 'owner') {
        role = this.members.get(`${input.reportId}:${input.actorId}`) ?? null;
      }
      if (!role || role === 'viewer' && report.status !== 'published') {
        throw new ReportError(404, 'report_not_found', 'report is unavailable');
      }
      return role === 'viewer'
        ? this.publishedReport(report, false, role)
        : { ...structuredClone(report), accessRole: role };
    }

    const share = this.shares.find(({ id }) => id === input.shareLinkId);
    this.afterShareLookup?.();
    const currentShare = this.shares.find(({ id }) => id === input.shareLinkId);
    if (
      !share
      || !currentShare
      || currentShare.revokedAt
      || (
        currentShare.expiresAt
        && currentShare.expiresAt.getTime() <= input.readAt.getTime()
      )
    ) {
      throw new ReportError(
        410,
        'share_unavailable',
        'share is expired or revoked',
      );
    }
    if (currentShare.reportId !== input.reportId) {
      throw new ReportError(404, 'report_not_found', 'report is unavailable');
    }
    if (report.status !== 'published') {
      throw new ReportError(
        410,
        'share_unavailable',
        'share is expired or revoked',
      );
    }
    return this.publishedReport(report, true, 'public_reader');
  }

  async updateCollaborativeFieldAtomic(
    input: UpdateCollaborativeFieldInput,
  ): Promise<ReportItemDto> {
    const found = this.findItem(input.itemId);
    if (!found) {
      throw new ReportError(404, 'item_not_found', 'item is unavailable');
    }
    const role = this.actorRole(found.report.id, input.actorId);
    if (role === 'viewer') {
      const isPublishedItem = (
        found.report.status === 'published'
        && this.publications
          .get(`${found.report.id}:${found.report.publishedVersion}`)
          ?.some(({ id }) => id === found.item.id)
      );
      if (isPublishedItem) {
        throw new ReportError(403, 'edit_forbidden', 'report is read-only');
      }
      throw new ReportError(404, 'item_not_found', 'item is unavailable');
    }
    if (role !== 'owner' && role !== 'editor') {
      throw new ReportError(404, 'item_not_found', 'item is unavailable');
    }
    if (found.item.version !== input.expectedVersion) {
      throw new ReportError(409, 'stale_version', 'item version is stale');
    }

    const property = this.fieldProperty(input.field);
    const oldValue = found.item[property];
    const itemVersion = found.item.version + 1;
    found.item[property] = input.value;
    found.item.version = itemVersion;
    found.report.draftVersion += 1;
    this.revisions.push({
      actorId: input.actorId,
      fieldName: input.field,
      id: this.revisions.length + 1,
      itemVersion,
      newValue: input.value,
      oldValue,
      reportItemId: input.itemId,
    });
    this.audits.push({
      actorId: input.actorId,
      eventType: 'report_item.updated',
      reportId: found.report.id,
    });
    return structuredClone(found.item);
  }

  async publishReportAtomic(input: PublishReportInput): Promise<ReportRecord> {
    if (this.roles.get(input.actorId) !== 'owner') {
      throw new ReportError(403, 'owner_required', 'owner access required');
    }
    const report = this.reports.get(input.reportId);
    if (!report) {
      throw new ReportError(404, 'report_not_found', 'report is unavailable');
    }
    if (
      report.items.length === 0
      || report.items.some((item) => !this.mediaReady.get(item.id))
    ) {
      throw new ReportError(409, 'report_not_ready', 'report media is incomplete');
    }
    const nextPublishedVersion = report.publishedVersion + 1;
    const publicationKey = `${report.id}:${nextPublishedVersion}`;
    this.publications.set(publicationKey, structuredClone(report.items));
    report.status = 'published';
    report.publishedAt = input.publishedAt.toISOString();
    report.publishedVersion = nextPublishedVersion;
    this.audits.push({
      actorId: input.actorId,
      eventType: 'report.published',
      reportId: report.id,
    });
    return structuredClone(report);
  }

  async upsertPublisherDraftAtomic(
    input: PublisherDraftReportInput,
  ): Promise<ReportRecord> {
    if (input.publisherDeviceId !== PUBLISHER_DEVICE_ID) {
      throw new ReportError(
        403,
        'publisher_device_unavailable',
        'publisher device is unavailable',
      );
    }
    let report = [...this.reports.values()].find((candidate) =>
      candidate.dailyDate === input.dailyDate
      && candidate.id === REPORT_ID
    );
    if (!report) {
      report = {
        accessRole: 'owner',
        dailyDate: input.dailyDate,
        draftVersion: 1,
        id: REPORT_ID,
        items: [],
        publishedAt: null,
        publishedVersion: 0,
        status: 'draft',
      };
      this.reports.set(report.id, report);
    }
    const existingByLocalId = new Map(
      report.items.map((item) => [item.localRecordId, item]),
    );
    for (const incoming of input.items) {
      const existing = existingByLocalId.get(incoming.localRecordId);
      if (existing) {
        existing.title = incoming.title;
        existing.caption = incoming.caption;
        existing.sourceUrl = incoming.sourceUrl;
        continue;
      }
      const index = report.items.length + 1;
      const itemId = index === 1
        ? ITEM_ID
        : `00000000-0000-0000-0000-${String(200 + index).padStart(12, '0')}`;
      const mediaId = index === 1
        ? MEDIA_ID
        : `00000000-0000-0000-0000-${String(300 + index).padStart(12, '0')}`;
      report.items.push({
        caption: incoming.caption,
        id: itemId,
        localRecordId: incoming.localRecordId,
        maxDailyCard: incoming.maxDailyCard,
        maxFeedback: incoming.maxFeedback,
        mediaId,
        reviewStatus: incoming.reviewStatus,
        sourceUrl: incoming.sourceUrl,
        title: incoming.title,
        version: 1,
      });
      this.mediaReady.set(itemId, true);
    }
    report.items.sort((left, right) =>
      input.items.findIndex((item) => item.localRecordId === left.localRecordId)
      - input.items.findIndex((item) => item.localRecordId === right.localRecordId)
    );
    report.draftVersion += 1;
    return structuredClone(report);
  }

  async publishPublisherReportAtomic(
    input: PublisherPublishReportInput,
  ): Promise<ReportRecord> {
    if (input.publisherDeviceId !== PUBLISHER_DEVICE_ID) {
      throw new ReportError(
        403,
        'publisher_device_unavailable',
        'publisher device is unavailable',
      );
    }
    const report = this.reports.get(input.reportId);
    if (!report) {
      throw new ReportError(404, 'report_not_found', 'report is unavailable');
    }
    if (report.draftVersion !== input.expectedDraftVersion) {
      throw new ReportError(409, 'stale_version', 'item version is stale');
    }
    return this.publishReportAtomic({
      actorId: OWNER_ID,
      publishedAt: input.publishedAt,
      reportId: input.reportId,
    });
  }

  async restoreRevisionAtomic(
    input: RestoreRevisionInput,
  ): Promise<ReportItemDto> {
    if (this.roles.get(input.actorId) !== 'owner') {
      throw new ReportError(403, 'owner_required', 'owner access required');
    }
    const revision = this.revisions.find(({ id }) => id === input.revisionId);
    if (!revision) {
      throw new ReportError(404, 'revision_not_found', 'revision is unavailable');
    }
    const found = this.findItem(revision.reportItemId);
    if (!found) {
      throw new ReportError(404, 'revision_not_found', 'revision is unavailable');
    }
    if (found.item.version !== input.expectedVersion) {
      throw new ReportError(409, 'stale_version', 'item version is stale');
    }

    const property = this.fieldProperty(revision.fieldName);
    const oldValue = found.item[property];
    const itemVersion = found.item.version + 1;
    found.item[property] = revision.oldValue;
    found.item.version = itemVersion;
    found.report.draftVersion += 1;
    this.revisions.push({
      actorId: input.actorId,
      fieldName: revision.fieldName,
      id: this.revisions.length + 1,
      itemVersion,
      newValue: revision.oldValue,
      oldValue,
      reportItemId: found.item.id,
    });
    this.audits.push({
      actorId: input.actorId,
      eventType: 'revision.restored',
      reportId: found.report.id,
    });
    return structuredClone(found.item);
  }

  async claimInvitationAtomic(): Promise<{ reportId: string; role: MemberRole }> {
    throw new Error('not used');
  }

  async findShareLinkById(id: string): Promise<ShareLinkRecord | null> {
    const share = this.liveShare(this.shares.find((candidate) => candidate.id === id));
    this.afterShareLookup?.();
    return share;
  }

  async findShareLinkByTokenHash(tokenHash: string): Promise<ShareLinkRecord | null> {
    return this.liveShare(this.shares.find((share) => share.tokenHash === tokenHash));
  }

  async insertInvitation(record: InvitationRecord): Promise<void> {
    this.invitations.push(structuredClone(record));
  }

  async insertShareLink(record: StoredShareLink): Promise<void> {
    this.shares.push(structuredClone(record));
  }

  async revokeInvitation(): Promise<boolean> {
    return false;
  }

  async revokeShareLink(): Promise<boolean> {
    return false;
  }

  private actorRole(reportId: string, actorId: string): Role | null {
    if (this.roles.get(actorId) === 'owner') return 'owner';
    return this.members.get(`${reportId}:${actorId}`) ?? null;
  }

  private fieldProperty(
    field: CollaborativeField,
  ): 'maxDailyCard' | 'maxFeedback' | 'reviewStatus' {
    if (field === 'max_daily_card') return 'maxDailyCard';
    if (field === 'max_feedback') return 'maxFeedback';
    return 'reviewStatus';
  }

  private findItem(
    itemId: string,
  ): { item: ReportItemDto; report: ReportRecord } | null {
    for (const report of this.reports.values()) {
      const item = report.items.find(({ id }) => id === itemId);
      if (item) return { item, report };
    }
    return null;
  }

  private publishedReport(
    report: ReportRecord,
    publicRead: boolean,
    accessRole: Role,
  ): ReportRecord {
    const items = this.publications.get(
      `${report.id}:${report.publishedVersion}`,
    );
    if (!items) {
      throw new ReportError(
        publicRead ? 410 : 404,
        publicRead ? 'share_unavailable' : 'report_not_found',
        publicRead ? 'share is expired or revoked' : 'report is unavailable',
      );
    }
    return {
      ...structuredClone(report),
      accessRole,
      items: structuredClone(items),
    };
  }

  private liveShare(
    share: StoredShareLink | undefined,
  ): ShareLinkRecord | null {
    if (!share) return null;
    return {
      ...structuredClone(share),
      reportStatus: this.reports.get(share.reportId)?.status ?? 'draft',
    };
  }
}

class TestIdentityVerifier implements IdentityVerifier {
  private readonly identities = new Map<string, VerifiedIdentity>([
    ['owner-token', { email: 'chen@example.com', id: OWNER_ID }],
    ['editor-token', { email: 'max@example.com', id: EDITOR_ID }],
    ['viewer-token', { email: 'viewer@example.com', id: VIEWER_ID }],
    ['cross-token', { email: 'cross@example.com', id: CROSS_REPORT_ID }],
  ]);

  async verify(request: Request): Promise<VerifiedIdentity | null> {
    const authorization = request.headers.get('authorization');
    const token = authorization?.startsWith('Bearer ') ? authorization.slice(7) : '';
    return this.identities.get(token) ?? null;
  }
}

class AllowRateLimiter {
  async limit(): Promise<{ success: boolean }> {
    return { success: true };
  }
}

function createHarness() {
  const storage = new MemoryStorage();
  const app = createWorkerApp({
    appOrigin: APP_ORIGIN,
    identityVerifier: new TestIdentityVerifier(),
    invitationClaimRateLimiter: new AllowRateLimiter(),
    mediaSessionSecret: MEDIA_SESSION_SECRET,
    now: () => NOW,
    ownerEmail: 'chen@example.com',
    publicShareRateLimiter: new AllowRateLimiter(),
    publisherDeviceAuthenticator: {
      authenticate: async (request) =>
        request.headers.get('x-publisher-token') === 'publisher-token'
          ? { deviceId: PUBLISHER_DEVICE_ID }
          : null,
    },
    shareCookieSecret: SESSION_SECRET,
    storage,
  });

  const request = (
    path: string,
    token?: string,
    init: RequestInit = {},
  ) => app.request(path, {
    ...init,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(init.body ? { 'content-type': 'application/json' } : {}),
      ...init.headers,
    },
  });

  return { app, request, storage };
}

describe('report visibility', () => {
  it('lists published reports for an Owner through the trusted owner path', async () => {
    const list = vi.fn(async () => [{
      dailyDate: '2026-07-10',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: NOW.toISOString(),
    }]);
    const storage = { listReportsForOwnerAtomic: list } as ReportStorage;

    await expect(listReportsForOwner(
      storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      NOW,
    )).resolves.toEqual([expect.objectContaining({ id: REPORT_ID })]);
    expect(list).toHaveBeenCalledWith({ actorId: OWNER_ID, readAt: NOW });
  });

  it('rejects a non-Owner before the trusted owner report path is called', async () => {
    const list = vi.fn();
    const storage = { listReportsForOwnerAtomic: list } as ReportStorage;

    await expect(listReportsForOwner(
      storage,
      { email: 'editor@example.com', id: EDITOR_ID, role: 'editor' },
      NOW,
    )).rejects.toMatchObject({
      code: 'owner_required',
      message: 'owner access required',
      status: 403,
    });
    expect(list).not.toHaveBeenCalled();
  });

  it('lists published reports through the collaboration link context', async () => {
    const list = vi.fn(async () => [{
      dailyDate: '2026-07-10',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: NOW.toISOString(),
    }]);
    const storage = { listReportsForCollaborationAtomic: list } as ReportStorage;

    await expect(listReportsForCollaborator(
      storage,
      collaborationSession,
      NOW,
    )).resolves.toEqual([expect.objectContaining({ id: REPORT_ID })]);
    expect(list).toHaveBeenCalledWith({
      linkId: COLLABORATION_LINK_ID,
      readAt: NOW,
    });
  });

  it('marks collaboration reports and applies Shanghai current-day media policy', async () => {
    const read = vi.fn(async ({ reportId }: { reportId: string }) => ({
      accessRole: 'owner' as const,
      dailyDate: reportId === REPORT_ID ? '2026-07-10' : '2026-07-09',
      draftVersion: 1,
      id: reportId,
      items: [{
        caption: 'caption',
        id: ITEM_ID,
        localRecordId: 'local-1',
        maxDailyCard: 'card',
        maxFeedback: '',
        mediaId: MEDIA_ID,
        reviewStatus: 'pending',
        sourceUrl: 'https://example.com/source',
        title: 'title',
        version: 1,
      }],
      publishedAt: NOW.toISOString(),
      publishedVersion: 1,
      status: 'published' as const,
    }));
    const storage = { readReportForCollaborationAtomic: read } as ReportStorage;

    const current = await getReportForCollaborator(
      storage,
      collaborationSession,
      REPORT_ID,
      NOW,
    );
    const historical = await getReportForCollaborator(
      storage,
      collaborationSession,
      OTHER_REPORT_ID,
      NOW,
    );

    expect(current).toMatchObject({
      accessRole: 'collaborator',
      mediaMode: 'current_day',
      items: [{ mediaId: MEDIA_ID }],
    });
    expect(historical).toMatchObject({
      accessRole: 'collaborator',
      mediaMode: 'historical',
      items: [{
        mediaId: null,
        sourceUrl: 'https://example.com/source',
      }],
    });
    expect(read).toHaveBeenNthCalledWith(1, {
      linkId: COLLABORATION_LINK_ID,
      readAt: NOW,
      reportId: REPORT_ID,
    });
  });

  it('validates collaboration patch fields and limits before RPC while allowing empty values', async () => {
    const update = vi.fn(async (input: { itemId: string; value: string }) => ({
      caption: 'caption',
      id: input.itemId,
      localRecordId: 'local-1',
      maxDailyCard: '',
      maxFeedback: input.value,
      mediaId: MEDIA_ID,
      reviewStatus: 'pending',
      sourceUrl: 'https://example.com/source',
      title: 'title',
      version: 2,
    }));
    const storage = {
      updateCollaborativeFieldForLinkAtomic: update,
    } as ReportStorage;

    for (const [field, value] of [
      ['max_daily_card', 'x'.repeat(50_000)],
      ['max_feedback', 'x'.repeat(20_000)],
      ['review_status', 'x'.repeat(128)],
      ['max_feedback', ''],
    ] as const) {
      await expect(patchItemForCollaborator(
        storage,
        collaborationSession,
        ITEM_ID,
        { expectedVersion: 1, field, value },
        NOW,
      )).resolves.toMatchObject({ id: ITEM_ID });
    }
    expect(update).toHaveBeenCalledTimes(4);
    expect(update).toHaveBeenLastCalledWith({
      changedAt: NOW,
      expectedVersion: 1,
      field: 'max_feedback',
      itemId: ITEM_ID,
      linkId: COLLABORATION_LINK_ID,
      value: '',
    });

    for (const [field, value, code] of [
      ['max_daily_card', 'x'.repeat(50_001), 'collaborative_value_too_long'],
      ['max_feedback', 'x'.repeat(20_001), 'collaborative_value_too_long'],
      ['review_status', 'x'.repeat(129), 'collaborative_value_too_long'],
      ['title', 'changed source', 'field_not_editable'],
    ] as const) {
      await expect(patchItemForCollaborator(
        storage,
        collaborationSession,
        ITEM_ID,
        { expectedVersion: 1, field, value },
        NOW,
      )).rejects.toMatchObject({ code });
    }
    expect(update).toHaveBeenCalledTimes(4);
  });

  it('formats report dates in Asia/Shanghai across the UTC day boundary', () => {
    expect(toShanghaiDate(new Date('2026-07-12T15:59:59Z'))).toBe(
      '2026-07-12',
    );
    expect(toShanghaiDate(new Date('2026-07-12T16:00:00Z'))).toBe(
      '2026-07-13',
    );
  });

  it('retains current media and redacts historical media without changing report content', () => {
    const report = new MemoryStorage().reports.get(REPORT_ID);
    if (!report) throw new Error('test report is missing');

    const current = applyReportMediaPolicy(
      report,
      new Date('2026-07-10T09:00:00Z'),
    );
    const historical = applyReportMediaPolicy(
      report,
      new Date('2026-07-11T09:00:00Z'),
    );

    expect(current).toMatchObject({
      mediaMode: 'current_day',
      items: [{ mediaId: MEDIA_ID }],
    });
    expect(historical).toMatchObject({
      mediaMode: 'historical',
      items: [{
        maxDailyCard: 'old card',
        mediaId: null,
        sourceUrl: 'https://example.com/source',
      }],
    });
  });

  it('applies the media policy to authenticated and public report reads', async () => {
    const harness = createHarness();
    const authenticated = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'owner-token',
    );
    const share = await createShareLink(
      harness.storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      { reportId: OTHER_REPORT_ID },
      NOW,
    );
    const exchange = await exchangeShareToken(
      harness.storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
    const publicResponse = await harness.request(
      `/api/reports/${OTHER_REPORT_ID}`,
      undefined,
      { headers: { cookie } },
    );

    expect(authenticated.status).toBe(200);
    expect(await authenticated.json()).toMatchObject({
      mediaMode: 'current_day',
      items: [{ mediaId: MEDIA_ID }],
    });
    expect(publicResponse.status).toBe(200);
    expect(await publicResponse.json()).toMatchObject({
      mediaMode: 'historical',
      items: [{
        mediaId: null,
        sourceUrl: 'https://example.com/other',
      }],
    });
  });

  it('returns the trusted effective role for every report reader', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);

    const ownerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'owner-token',
    );
    const editorResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'editor-token',
    );
    const viewerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    const share = await createShareLink(
      harness.storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      { reportId: REPORT_ID },
      NOW,
    );
    const exchange = await exchangeShareToken(
      harness.storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
    const publicResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      undefined,
      { headers: { cookie } },
    );

    expect(await ownerResponse.json()).toMatchObject({ accessRole: 'owner' });
    expect(await editorResponse.json()).toMatchObject({ accessRole: 'editor' });
    expect(await viewerResponse.json()).toMatchObject({ accessRole: 'viewer' });
    expect(await publicResponse.json()).toMatchObject({
      accessRole: 'public_reader',
    });
  });

  it('shows a draft only to the owner or an editor member', async () => {
    const harness = createHarness();

    const editorResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'editor-token',
    );
    const viewerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );

    expect(editorResponse.status).toBe(200);
    expect(await editorResponse.json()).toMatchObject({
      id: REPORT_ID,
      status: 'draft',
      items: [{ id: ITEM_ID, maxDailyCard: 'old card' }],
    });
    expect(viewerResponse.status).toBe(404);
    expect(await viewerResponse.json()).toMatchObject({
      code: 'report_not_found',
    });
  });

  it('shows a published report to its viewer member but not another report member', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);

    const viewerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    const crossReportResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'cross-token',
    );

    expect(viewerResponse.status).toBe(200);
    expect(crossReportResponse.status).toBe(404);
    expect(await crossReportResponse.json()).toMatchObject({
      code: 'report_not_found',
    });
  });

  it('binds a public reader to the published report in its signed share session', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    const share = await createShareLink(
      harness.storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      { reportId: REPORT_ID },
      NOW,
    );
    const exchange = await exchangeShareToken(
      harness.storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';

    const response = await harness.request(`/api/reports/${REPORT_ID}`, undefined, {
      headers: { cookie },
    });
    const crossReportResponse = await harness.request(
      `/api/reports/${OTHER_REPORT_ID}`,
      undefined,
      { headers: { cookie } },
    );

    expect(response.status).toBe(200);
    expect(crossReportResponse.status).toBe(404);
    expect(await crossReportResponse.json()).toMatchObject({
      code: 'report_not_found',
    });
  });

  it('denies a viewer when membership is revoked between authorization and report reads', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    harness.storage.afterMemberLookup = () => {
      harness.storage.members.delete(`${REPORT_ID}:${VIEWER_ID}`);
    };

    const response = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );

    expect(response.status).toBe(404);
    expect(await response.json()).toMatchObject({ code: 'report_not_found' });
  });

  it('denies a public read when the share is revoked during report resolution', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    const share = await createShareLink(
      harness.storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      { reportId: REPORT_ID },
      NOW,
    );
    const exchange = await exchangeShareToken(
      harness.storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
    harness.storage.afterShareLookup = () => {
      const stored = harness.storage.shares.find(({ id }) => id === share.id);
      if (stored) stored.revokedAt = NOW;
    };

    const response = await harness.request(`/api/reports/${REPORT_ID}`, undefined, {
      headers: { cookie },
    });

    expect(response.status).toBe(410);
    expect(await response.json()).toMatchObject({ code: 'share_unavailable' });
  });

  it('denies a public read when the report is withdrawn during report resolution', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    const share = await createShareLink(
      harness.storage,
      { email: 'chen@example.com', id: OWNER_ID, role: 'owner' },
      { reportId: REPORT_ID },
      NOW,
    );
    const exchange = await exchangeShareToken(
      harness.storage,
      share.token,
      SESSION_SECRET,
      APP_ORIGIN,
      NOW,
    );
    const cookie = exchange.headers.get('set-cookie')?.split(';', 1)[0] ?? '';
    harness.storage.afterShareLookup = () => {
      const report = harness.storage.reports.get(REPORT_ID);
      if (report) report.status = 'withdrawn';
    };

    const response = await harness.request(`/api/reports/${REPORT_ID}`, undefined, {
      headers: { cookie },
    });

    expect(response.status).toBe(410);
    expect(await response.json()).toMatchObject({ code: 'share_unavailable' });
  });
});

describe('versioned collaborative edits', () => {
  it('lets an editor update an allowed field and records revision and audit history', async () => {
    const harness = createHarness();

    const response = await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'max_daily_card',
        value: 'new card',
      }),
      method: 'PATCH',
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      id: ITEM_ID,
      maxDailyCard: 'new card',
      version: 2,
    });
    expect(harness.storage.revisions.at(-1)).toMatchObject({
      actorId: EDITOR_ID,
      fieldName: 'max_daily_card',
      itemVersion: 2,
      newValue: 'new card',
      oldValue: 'old card',
    });
    expect(harness.storage.audits.at(-1)).toMatchObject({
      actorId: EDITOR_ID,
      eventType: 'report_item.updated',
      reportId: REPORT_ID,
    });
  });

  it('rejects source snapshot fields even for an editor', async () => {
    const harness = createHarness();

    const response = await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'title',
        value: 'changed title',
      }),
      method: 'PATCH',
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({ code: 'field_not_editable' });
    expect(harness.storage.revisions).toHaveLength(0);
  });

  it('rejects a stale version without changing the item', async () => {
    const harness = createHarness();

    const response = await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 0,
        field: 'max_feedback',
        value: 'late feedback',
      }),
      headers: { origin: APP_ORIGIN },
      method: 'PATCH',
    });

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ code: 'stale_version' });
    expect(response.headers.get('access-control-allow-origin')).toBe(APP_ORIGIN);
    expect(response.headers.get('access-control-allow-credentials')).toBe('true');
    expect(harness.storage.revisions).toHaveLength(0);
  });

  it('hides a draft item from a viewer with a non-leaking 404', async () => {
    const harness = createHarness();
    const response = await harness.request(`/api/items/${ITEM_ID}`, 'viewer-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'review_status',
        value: 'approved',
      }),
      method: 'PATCH',
    });

    expect(response.status).toBe(404);
    expect(await response.json()).toMatchObject({ code: 'item_not_found' });
  });

  it('denies a viewer for a known published item and hides it from another report member', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    const body = JSON.stringify({
      expectedVersion: 1,
      field: 'review_status',
      value: 'approved',
    });

    const viewerResponse = await harness.request(
      `/api/items/${ITEM_ID}`,
      'viewer-token',
      { body, method: 'PATCH' },
    );
    const crossReportResponse = await harness.request(
      `/api/items/${ITEM_ID}`,
      'cross-token',
      { body, method: 'PATCH' },
    );

    expect(viewerResponse.status).toBe(403);
    expect(await viewerResponse.json()).toMatchObject({ code: 'edit_forbidden' });
    expect(crossReportResponse.status).toBe(404);
    expect(await crossReportResponse.json()).toMatchObject({
      code: 'item_not_found',
    });
  });
});

describe('owner publishing and restore', () => {
  it('publishes a media-ready report as the owner and records an audit event', async () => {
    const harness = createHarness();

    const response = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      id: REPORT_ID,
      publishedAt: NOW.toISOString(),
      publishedVersion: 1,
      status: 'published',
    });
    expect(harness.storage.audits.at(-1)).toMatchObject({
      actorId: OWNER_ID,
      eventType: 'report.published',
      reportId: REPORT_ID,
    });
  });

  it('returns a new publishedVersion for every successful publish', async () => {
    const harness = createHarness();

    const first = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );
    const second = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );

    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({
      draftVersion: 1,
      publishedVersion: 1,
    });
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({
      draftVersion: 1,
      publishedVersion: 2,
    });
    expect(harness.storage.publications.has(`${REPORT_ID}:1`)).toBe(true);
    expect(harness.storage.publications.has(`${REPORT_ID}:2`)).toBe(true);
  });

  it('keeps an incomplete report unpublished', async () => {
    const harness = createHarness();
    harness.storage.mediaReady.set(ITEM_ID, false);

    const response = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ code: 'report_not_ready' });
    expect(harness.storage.reports.get(REPORT_ID)?.status).toBe('draft');
    expect(harness.storage.audits).toHaveLength(0);
  });

  it('keeps post-publish edits invisible to viewers until a successful republish', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);

    expect((await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'max_daily_card',
        value: 'republished card',
      }),
      method: 'PATCH',
    })).status).toBe(200);

    const beforeRepublish = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    expect(await beforeRepublish.json()).toMatchObject({
      publishedVersion: 1,
      items: [{ id: ITEM_ID, maxDailyCard: 'old card', version: 1 }],
    });

    const republish = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );
    expect(republish.status).toBe(200);
    expect(await republish.json()).toMatchObject({ publishedVersion: 2 });

    const afterRepublish = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    expect(await afterRepublish.json()).toMatchObject({
      publishedVersion: 2,
      items: [{ id: ITEM_ID, maxDailyCard: 'republished card', version: 2 }],
    });
  });

  it('preserves the previous snapshot when a republish fails readiness checks', async () => {
    const harness = createHarness();
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);
    expect((await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'max_feedback',
        value: 'draft-only feedback',
      }),
      method: 'PATCH',
    })).status).toBe(200);
    harness.storage.mediaReady.set(ITEM_ID, false);

    const failedRepublish = await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    );
    expect(failedRepublish.status).toBe(409);

    const viewerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    expect(await viewerResponse.json()).toMatchObject({
      publishedVersion: 1,
      status: 'published',
      items: [{ id: ITEM_ID, maxFeedback: '', version: 1 }],
    });
  });

  it('restores an earlier collaborative value as an owner with a new revision', async () => {
    const harness = createHarness();
    const edit = await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'max_daily_card',
        value: 'new card',
      }),
      method: 'PATCH',
    });
    expect(edit.status).toBe(200);

    const response = await harness.request('/api/revisions/1/restore', 'owner-token', {
      body: JSON.stringify({ expectedVersion: 2 }),
      method: 'POST',
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      id: ITEM_ID,
      maxDailyCard: 'old card',
      version: 3,
    });
    expect(harness.storage.revisions).toHaveLength(2);
    expect(harness.storage.revisions.at(-1)).toMatchObject({
      actorId: OWNER_ID,
      fieldName: 'max_daily_card',
      itemVersion: 3,
      newValue: 'old card',
      oldValue: 'new card',
    });
    expect(harness.storage.audits.at(-1)).toMatchObject({
      actorId: OWNER_ID,
      eventType: 'revision.restored',
      reportId: REPORT_ID,
    });
  });

  it('keeps a post-publish restore invisible to viewers until republish', async () => {
    const harness = createHarness();
    expect((await harness.request(`/api/items/${ITEM_ID}`, 'editor-token', {
      body: JSON.stringify({
        expectedVersion: 1,
        field: 'max_daily_card',
        value: 'published card',
      }),
      method: 'PATCH',
    })).status).toBe(200);
    expect((await harness.request(
      `/api/reports/${REPORT_ID}/publish`,
      'owner-token',
      { method: 'POST' },
    )).status).toBe(200);

    expect((await harness.request('/api/revisions/1/restore', 'owner-token', {
      body: JSON.stringify({ expectedVersion: 2 }),
      method: 'POST',
    })).status).toBe(200);

    const viewerResponse = await harness.request(
      `/api/reports/${REPORT_ID}`,
      'viewer-token',
    );
    expect(await viewerResponse.json()).toMatchObject({
      publishedVersion: 1,
      items: [{ id: ITEM_ID, maxDailyCard: 'published card', version: 2 }],
    });
  });
});

describe('publisher report delivery', () => {
  const publisherDraftBody = {
    dailyDate: '2026-07-10',
    sourceTableId: 'source-table',
    items: [{
      caption: 'refreshed caption',
      itemOrder: 1,
      localRecordId: 'local-1',
      maxDailyCard: 'publisher card should not overwrite',
      maxFeedback: 'publisher feedback should not overwrite',
      reviewStatus: 'approved',
      sourceUrl: 'https://example.com/refreshed',
      title: 'refreshed title',
    }],
  };

  it('upserts a draft idempotently without overwriting collaborative fields', async () => {
    const harness = createHarness();
    const item = harness.storage.reports.get(REPORT_ID)?.items[0];
    if (item) {
      item.maxDailyCard = 'editor card';
      item.maxFeedback = 'editor feedback';
      item.reviewStatus = 'pending';
    }

    const first = await harness.request('/api/publisher/reports', undefined, {
      body: JSON.stringify(publisherDraftBody),
      headers: { 'x-publisher-token': 'publisher-token' },
      method: 'PUT',
    });
    const second = await harness.request('/api/publisher/reports', undefined, {
      body: JSON.stringify(publisherDraftBody),
      headers: { 'x-publisher-token': 'publisher-token' },
      method: 'PUT',
    });

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({
      id: REPORT_ID,
      items: [{
        id: ITEM_ID,
        localRecordId: 'local-1',
        maxDailyCard: 'editor card',
        maxFeedback: 'editor feedback',
        reviewStatus: 'pending',
        sourceUrl: 'https://example.com/refreshed',
        title: 'refreshed title',
      }],
    });
    expect(harness.storage.reports.get(REPORT_ID)?.items).toHaveLength(1);
  });

  it('publishes through a publisher device with expected draft version checks', async () => {
    const harness = createHarness();
    const draft = await harness.request('/api/publisher/reports', undefined, {
      body: JSON.stringify(publisherDraftBody),
      headers: { 'x-publisher-token': 'publisher-token' },
      method: 'PUT',
    });
    const draftBody = await draft.json() as { draftVersion: number; id: string };

    const stale = await harness.request(
      `/api/publisher/reports/${draftBody.id}/publish`,
      undefined,
      {
        body: JSON.stringify({ expectedDraftVersion: draftBody.draftVersion - 1 }),
        headers: { 'x-publisher-token': 'publisher-token' },
        method: 'POST',
      },
    );
    const published = await harness.request(
      `/api/publisher/reports/${draftBody.id}/publish`,
      undefined,
      {
        body: JSON.stringify({ expectedDraftVersion: draftBody.draftVersion }),
        headers: { 'x-publisher-token': 'publisher-token' },
        method: 'POST',
      },
    );

    expect(stale.status).toBe(409);
    expect(await stale.json()).toMatchObject({ code: 'stale_version' });
    expect(published.status).toBe(200);
    expect(await published.json()).toMatchObject({
      id: REPORT_ID,
      status: 'published',
    });
  });
});

describe('production report storage', () => {
  it('uses service-only RPCs for publisher draft upsert and publish', async () => {
    const serviceKey = 'test-service-role-key';
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/rest/v1/rpc/upsert_publisher_draft')) {
        return Response.json({
          daily_date: '2026-07-10',
          draft_version: 2,
          id: REPORT_ID,
          items: [{
            caption: 'source caption',
            id: ITEM_ID,
            local_record_id: 'local-1',
            max_daily_card: 'card',
            max_feedback: '',
            media_id: MEDIA_ID,
            review_status: 'pending',
            source_url: 'https://example.com/source',
            title: 'source title',
            version: 1,
          }],
          published_at: null,
          published_version: 0,
          status: 'draft',
        });
      }
      if (url.endsWith('/rest/v1/rpc/publish_publisher_report')) {
        return Response.json({
          daily_date: '2026-07-10',
          draft_version: 2,
          id: REPORT_ID,
          published_at: NOW.toISOString(),
          published_version: 1,
          status: 'published',
        });
      }
      return new Response('not found', { status: 404 });
    });
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });

    await storage.upsertPublisherDraftAtomic({
      dailyDate: '2026-07-10',
      items: [{
        caption: 'source caption',
        itemOrder: 1,
        localRecordId: 'local-1',
        maxDailyCard: 'card',
        maxFeedback: '',
        reviewStatus: 'pending',
        sourceUrl: 'https://example.com/source',
        title: 'source title',
      }],
      publisherDeviceId: PUBLISHER_DEVICE_ID,
      sourceTableId: 'source-table',
      upsertedAt: NOW,
    });
    await storage.publishPublisherReportAtomic({
      expectedDraftVersion: 2,
      publishedAt: NOW,
      publisherDeviceId: PUBLISHER_DEVICE_ID,
      reportId: REPORT_ID,
    });

    expect(fetcher.mock.calls.map(([input]) => String(input))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/upsert_publisher_draft',
      'https://project.supabase.co/rest/v1/rpc/publish_publisher_report',
    ]);
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toMatchObject({
      p_daily_date: '2026-07-10',
      p_publisher_device_id: PUBLISHER_DEVICE_ID,
      p_source_table_id: 'source-table',
    });
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toMatchObject({
      p_expected_draft_version: 2,
      p_publisher_device_id: PUBLISHER_DEVICE_ID,
      p_report_id: REPORT_ID,
    });
  });

  it('reads authorization and report rows through one trusted service RPC', async () => {
    const serviceKey = 'test-service-role-key';
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/rest/v1/rpc/read_report_with_access_role')) {
        return Response.json({
          access_role: 'viewer',
          daily_date: '2026-07-10',
          draft_version: 2,
          id: REPORT_ID,
          items: [{
            caption: 'source caption',
            id: ITEM_ID,
            local_record_id: 'local-1',
            max_daily_card: 'published card',
            max_feedback: '',
            media_id: MEDIA_ID,
            review_status: 'pending',
            source_url: 'https://example.com/source',
            title: 'source title',
            version: 2,
          }],
          published_at: NOW.toISOString(),
          published_version: 2,
          status: 'published',
        });
      }
      return new Response('not found', { status: 404 });
    });
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });

    const report = await getReportForIdentity(
      storage,
      { email: 'viewer@example.com', id: VIEWER_ID, role: 'viewer' },
      REPORT_ID,
      NOW,
    );

    expect(report).toMatchObject({
      accessRole: 'viewer',
      id: REPORT_ID,
      items: [{ id: ITEM_ID, maxDailyCard: 'published card' }],
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      'https://project.supabase.co/rest/v1/rpc/read_report_with_access_role',
    );
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toMatchObject({
      p_actor_id: VIEWER_ID,
      p_read_at: NOW.toISOString(),
      p_report_id: REPORT_ID,
      p_share_link_id: null,
    });
  });

  it('uses trusted service-only RPCs for edits, publish, and restore', async () => {
    const serviceKey = 'test-service-role-key';
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/rest/v1/rpc/update_collaborative_field')) {
        return Response.json([{
          caption: 'source caption',
          id: ITEM_ID,
          local_record_id: 'local-1',
          max_daily_card: 'new card',
          max_feedback: '',
          media_id: MEDIA_ID,
          review_status: 'pending',
          source_url: 'https://example.com/source',
          title: 'source title',
          version: 2,
        }]);
      }
      if (url.endsWith('/rest/v1/rpc/publish_report')) {
        return Response.json({
          daily_date: '2026-07-10',
          draft_version: 2,
          id: REPORT_ID,
          published_at: NOW.toISOString(),
          published_version: 2,
          status: 'published',
        });
      }
      if (url.endsWith('/rest/v1/rpc/restore_revision')) {
        return Response.json([{
          caption: 'source caption',
          id: ITEM_ID,
          local_record_id: 'local-1',
          max_daily_card: 'old card',
          max_feedback: '',
          media_id: MEDIA_ID,
          review_status: 'pending',
          source_url: 'https://example.com/source',
          title: 'source title',
          version: 3,
        }]);
      }
      return new Response('not found', { status: 404 });
    });
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: serviceKey,
      supabaseUrl: 'https://project.supabase.co',
    });

    await storage.updateCollaborativeFieldAtomic({
      actorId: EDITOR_ID,
      changedAt: NOW,
      expectedVersion: 1,
      field: 'max_daily_card',
      itemId: ITEM_ID,
      value: 'new card',
    });
    await storage.publishReportAtomic({
      actorId: OWNER_ID,
      publishedAt: NOW,
      reportId: REPORT_ID,
    });
    await storage.restoreRevisionAtomic({
      actorId: OWNER_ID,
      expectedVersion: 2,
      restoredAt: NOW,
      revisionId: 1,
    });

    const mutationRequests = fetcher.mock.calls.filter(([, init]) =>
      init?.method === 'POST');
    expect(mutationRequests.map(([input]) => String(input))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/update_collaborative_field',
      'https://project.supabase.co/rest/v1/rpc/publish_report',
      'https://project.supabase.co/rest/v1/rpc/restore_revision',
    ]);
    for (const [, init] of fetcher.mock.calls) {
      const headers = new Headers(init?.headers);
      expect(headers.get('apikey')).toBe(serviceKey);
      expect(headers.get('authorization')).toBe(`Bearer ${serviceKey}`);
    }
    expect(fetcher.mock.calls.some(([input, init]) =>
      String(input).includes('/rest/v1/report_items')
      && init?.method === 'PATCH')).toBe(false);
    expect(fetcher.mock.calls.some(([input, init]) =>
      String(input).includes('/rest/v1/reports')
      && init?.method === 'PATCH')).toBe(false);
  });
});
