export type Role = 'owner' | 'editor' | 'viewer' | 'collaborator' | 'public_reader';
export type CollaborativeField = 'max_daily_card' | 'max_feedback' | 'review_status';
export type ReportItemField = CollaborativeField | 'title' | 'caption' | 'source_url';

export interface ReportItemDto {
  id: string;
  localRecordId: string;
  title: string;
  caption: string;
  sourceUrl: string;
  maxDailyCard: string;
  maxFeedback: string;
  reviewStatus: string;
  version: number;
  mediaId: string | null;
}

export interface ReportDto {
  accessRole: Role;
  id: string;
  dailyDate: string;
  mediaMode: 'current_day' | 'historical';
  status: 'draft' | 'published' | 'withdrawn';
  publishedVersion: number;
  items: ReportItemDto[];
}

export interface ReportIndexEntryDto {
  dailyDate: string;
  id: string;
  itemCount: number;
  publishedAt: string;
}

export interface MobileInboxItemDto {
  id: string;
  note: string;
  platform: string;
  status: string;
  submittedAt: string;
  url: string;
}

export interface MobileInboxSnapshotDto {
  items: MobileInboxItemDto[];
  pendingCount: number;
}

export interface MobileInboxSubmitRequest {
  note: string;
  text: string;
}

export interface MobileInboxSubmitResult extends MobileInboxSnapshotDto {
  addedCount: number;
  existingCount: number;
  ignoredCount: number;
}

export interface ApiError {
  code: string;
  message: string;
  requestId: string;
}

const collaborativeFields = new Set<ReportItemField>([
  'max_daily_card',
  'max_feedback',
  'review_status',
]);

export function canEditField(role: Role, field: ReportItemField): boolean {
  return role === 'owner'
    || ((role === 'editor' || role === 'collaborator') && collaborativeFields.has(field));
}
