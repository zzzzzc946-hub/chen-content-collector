import type {
  CollaborativeField,
  ReportDto,
  ReportIndexEntryDto,
  ReportItemDto,
  ReportItemField,
  Role,
} from '../../../packages/shared/src/contracts.js';
import type { Identity } from './auth.js';
import type { CollaborationSession } from './collaboration-links.js';
import type { ReportStatus } from './invitations.js';

export interface ReportSummary {
  dailyDate: string;
  draftVersion: number;
  id: string;
  publishedAt: string | null;
  publishedVersion: number;
  status: ReportStatus;
}

export interface ReportRecord extends ReportSummary {
  accessRole: Role;
  items: ReportItemDto[];
}

const shanghaiDateFormatter = new Intl.DateTimeFormat('en-CA', {
  day: '2-digit',
  month: '2-digit',
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
});

export interface UpdateCollaborativeFieldInput {
  actorId: string;
  changedAt: Date;
  expectedVersion: number;
  field: CollaborativeField;
  itemId: string;
  value: string;
}

export interface PublishReportInput {
  actorId: string;
  publishedAt: Date;
  reportId: string;
}

export interface PublisherDraftItemInput {
  caption: string;
  itemOrder: number;
  localRecordId: string;
  maxDailyCard: string;
  maxFeedback: string;
  reviewStatus: string;
  sourceUrl: string;
  title: string;
}

export interface PublisherDraftReportInput {
  dailyDate: string;
  items: PublisherDraftItemInput[];
  publisherDeviceId: string;
  sourceTableId: string;
  upsertedAt: Date;
}

export interface PublisherPublishReportInput {
  expectedDraftVersion: number;
  publishedAt: Date;
  publisherDeviceId: string;
  reportId: string;
}

export interface RestoreRevisionInput {
  actorId: string;
  expectedVersion: number;
  restoredAt: Date;
  revisionId: number;
}

export interface ReadReportInput {
  actorId: string | null;
  readAt: Date;
  reportId: string;
  shareLinkId: string | null;
}

export interface CollaborationReportReadInput {
  linkId: string;
  readAt: Date;
  reportId: string;
}

export interface CollaborationReportListInput {
  linkId: string;
  readAt: Date;
}

export interface OwnerReportListInput {
  actorId: string;
  readAt: Date;
}

export interface UpdateCollaborativeFieldForLinkInput {
  changedAt: Date;
  expectedVersion: number;
  field: CollaborativeField;
  itemId: string;
  linkId: string;
  value: string;
}

export interface ReportStorage {
  listReportsForCollaborationAtomic?(
    input: CollaborationReportListInput,
  ): Promise<ReportIndexEntryDto[]>;
  listReportsForOwnerAtomic?(
    input: OwnerReportListInput,
  ): Promise<ReportIndexEntryDto[]>;
  publishPublisherReportAtomic?(
    input: PublisherPublishReportInput,
  ): Promise<ReportSummary>;
  publishReportAtomic(input: PublishReportInput): Promise<ReportSummary>;
  readReportAtomic(input: ReadReportInput): Promise<ReportRecord>;
  readReportForCollaborationAtomic?(
    input: CollaborationReportReadInput,
  ): Promise<ReportRecord>;
  restoreRevisionAtomic(input: RestoreRevisionInput): Promise<ReportItemDto>;
  updateCollaborativeFieldAtomic(
    input: UpdateCollaborativeFieldInput,
  ): Promise<ReportItemDto>;
  updateCollaborativeFieldForLinkAtomic?(
    input: UpdateCollaborativeFieldForLinkInput,
  ): Promise<ReportItemDto>;
  upsertPublisherDraftAtomic?(
    input: PublisherDraftReportInput,
  ): Promise<ReportRecord>;
}

export class ReportError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'ReportError';
  }
}

export interface PatchItemInput {
  expectedVersion: number;
  field: ReportItemField;
  value: string;
}

export function toShanghaiDate(value: Date): string {
  return shanghaiDateFormatter.format(value);
}

export function applyReportMediaPolicy(
  report: ReportRecord,
  readAt: Date,
): ReportRecord & { mediaMode: 'current_day' | 'historical' } {
  const current = report.dailyDate === toShanghaiDate(readAt);
  return {
    ...report,
    items: current
      ? report.items
      : report.items.map((item) => ({ ...item, mediaId: null })),
    mediaMode: current ? 'current_day' : 'historical',
  };
}

function isCollaborativeField(
  field: ReportItemField,
): field is CollaborativeField {
  return (
    field === 'max_daily_card'
    || field === 'max_feedback'
    || field === 'review_status'
  );
}

const COLLABORATIVE_FIELD_LIMITS: Record<CollaborativeField, number> = {
  max_daily_card: 50_000,
  max_feedback: 20_000,
  review_status: 128,
};

function collaborationStorageMethod<T>(
  method: T | undefined,
): T {
  if (typeof method !== 'function') {
    throw new ReportError(500, 'storage_error', 'storage request failed');
  }
  return method;
}

export async function listReportsForCollaborator(
  storage: ReportStorage,
  session: CollaborationSession,
  readAt = new Date(),
): Promise<ReportIndexEntryDto[]> {
  const list = collaborationStorageMethod(
    storage.listReportsForCollaborationAtomic,
  );
  return list.call(storage, { linkId: session.linkId, readAt });
}

export async function listReportsForOwner(
  storage: ReportStorage,
  identity: Identity,
  readAt = new Date(),
): Promise<ReportIndexEntryDto[]> {
  if (identity.role !== 'owner') {
    throw new ReportError(403, 'owner_required', 'owner access required');
  }
  const list = collaborationStorageMethod(storage.listReportsForOwnerAtomic);
  return list.call(storage, { actorId: identity.id, readAt });
}

export async function getReportForCollaborator(
  storage: ReportStorage,
  session: CollaborationSession,
  reportId: string,
  readAt: Date,
): Promise<ReportDto> {
  const read = collaborationStorageMethod(
    storage.readReportForCollaborationAtomic,
  );
  const report = await read.call(storage, {
    linkId: session.linkId,
    readAt,
    reportId,
  });
  return applyReportMediaPolicy(
    { ...report, accessRole: 'collaborator' },
    readAt,
  );
}

export async function patchItemForCollaborator(
  storage: ReportStorage,
  session: CollaborationSession,
  itemId: string,
  input: PatchItemInput,
  changedAt: Date,
): Promise<ReportItemDto> {
  if (!isCollaborativeField(input.field)) {
    throw new ReportError(
      403,
      'field_not_editable',
      'field is not collaboratively editable',
    );
  }
  if (
    typeof input.value !== 'string'
    || input.value.length > COLLABORATIVE_FIELD_LIMITS[input.field]
  ) {
    throw new ReportError(
      400,
      'collaborative_value_too_long',
      'collaborative field value is too long',
    );
  }

  const update = collaborationStorageMethod(
    storage.updateCollaborativeFieldForLinkAtomic,
  );
  return update.call(storage, {
    changedAt,
    expectedVersion: input.expectedVersion,
    field: input.field,
    itemId,
    linkId: session.linkId,
    value: input.value,
  });
}

export async function getReportForIdentity(
  storage: ReportStorage,
  identity: Identity,
  reportId: string,
  readAt = new Date(),
): Promise<ReportRecord> {
  return storage.readReportAtomic({
    actorId: identity.id,
    readAt,
    reportId,
    shareLinkId: null,
  });
}

export async function getReportForPublicSession(
  storage: ReportStorage,
  reportId: string,
  shareLinkId: string,
  readAt = new Date(),
): Promise<ReportRecord> {
  return storage.readReportAtomic({
    actorId: null,
    readAt,
    reportId,
    shareLinkId,
  });
}

export async function patchItem(
  storage: ReportStorage,
  identity: Identity,
  itemId: string,
  input: PatchItemInput,
  changedAt: Date,
): Promise<ReportItemDto> {
  if (!isCollaborativeField(input.field)) {
    throw new ReportError(
      403,
      'field_not_editable',
      'field is not collaboratively editable',
    );
  }

  return storage.updateCollaborativeFieldAtomic({
    actorId: identity.id,
    changedAt,
    expectedVersion: input.expectedVersion,
    field: input.field,
    itemId,
    value: input.value,
  });
}

export async function publishReport(
  storage: ReportStorage,
  identity: Identity,
  reportId: string,
  publishedAt: Date,
): Promise<ReportSummary> {
  if (identity.role !== 'owner') {
    throw new ReportError(403, 'owner_required', 'owner access required');
  }
  return storage.publishReportAtomic({
    actorId: identity.id,
    publishedAt,
    reportId,
  });
}

export async function upsertPublisherDraftReport(
  storage: ReportStorage,
  input: PublisherDraftReportInput,
): Promise<ReportRecord> {
  if (typeof storage.upsertPublisherDraftAtomic !== 'function') {
    throw new ReportError(
      500,
      'publisher_report_service_unavailable',
      'publisher report service is not configured',
    );
  }
  return storage.upsertPublisherDraftAtomic(input);
}

export async function publishReportFromPublisher(
  storage: ReportStorage,
  input: PublisherPublishReportInput,
): Promise<ReportSummary> {
  if (typeof storage.publishPublisherReportAtomic !== 'function') {
    throw new ReportError(
      500,
      'publisher_report_service_unavailable',
      'publisher report service is not configured',
    );
  }
  return storage.publishPublisherReportAtomic(input);
}

export async function restoreRevision(
  storage: ReportStorage,
  identity: Identity,
  revisionId: number,
  expectedVersion: number,
  restoredAt: Date,
): Promise<ReportItemDto> {
  if (identity.role !== 'owner') {
    throw new ReportError(403, 'owner_required', 'owner access required');
  }
  return storage.restoreRevisionAtomic({
    actorId: identity.id,
    expectedVersion,
    restoredAt,
    revisionId,
  });
}
