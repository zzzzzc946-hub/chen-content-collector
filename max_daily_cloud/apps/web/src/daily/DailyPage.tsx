import { LogOut, RefreshCcw, Shield, UserRound } from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type {
  ApiError,
  CollaborativeField,
  ReportDto,
  ReportItemDto,
} from '../../../../packages/shared/src/contracts.js';
import { canEditField } from '../../../../packages/shared/src/contracts.js';
import { CollaborativePanel } from './CollaborativePanel.js';
import { DailyToolbar } from './DailyToolbar.js';
import { MaterialList } from './MaterialList.js';
import { TranscriptPanel } from './TranscriptPanel.js';
import { VideoPanel } from './VideoPanel.js';
import {
  filterAndSortItems,
  loadWorkbenchPreferences,
  saveWorkbenchPreferences,
} from './workbenchPreferences.js';
import {
  CollaborationConfigurationError,
  ShareDialog,
  type CollaborationLinkStatus,
} from '../share/ShareDialog.js';

const CONFLICT_MESSAGE = '内容已被其他人更新，请刷新后继续编辑';

interface DraftState {
  maxDailyCard: string;
  maxFeedback: string;
  reviewStatus: string;
}

interface InvitationCreateResponse {
  expiresAt: string;
  id: string;
  token: string;
}

interface ShareCreateResponse {
  expiresAt: string | null;
  id: string;
  token: string;
}

interface CollaborationCreateResponse {
  createdAt: string;
  id: string;
  token: string;
}

interface DailyApiClient {
  createCollaborationLink?(input: {
    accessToken: string;
  }): Promise<CollaborationCreateResponse>;
  createInvitation?(input: {
    accessToken: string;
    email: string;
    reportId: string;
    role: 'editor' | 'viewer';
  }): Promise<InvitationCreateResponse>;
  createShareLink?(input: {
    accessToken: string;
    expiresAt: string | null;
    reportId: string;
  }): Promise<ShareCreateResponse>;
  patchItem?(input: {
    accessToken: string | null;
    expectedVersion: number;
    field: CollaborativeField;
    itemId: string;
    mode: DailySession['mode'];
    value: string;
  }): Promise<ReportItemDto>;
  getCurrentCollaborationLink?(input: {
    accessToken: string;
  }): Promise<CollaborationLinkStatus>;
  revokeInvitation?(input: {
    accessToken: string;
    invitationId: string;
  }): Promise<void>;
  revokeCollaborationLink?(input: {
    accessToken: string;
    linkId: string;
  }): Promise<void>;
  revokeShareLink?(input: {
    accessToken: string;
    shareId: string;
  }): Promise<void>;
}

export interface DailySession {
  accessToken: string | null;
  email: string | null;
  mode: 'identity' | 'collaborator' | 'public';
}

interface DailyPageProps {
  apiBaseUrl?: string;
  apiClient?: DailyApiClient;
  dateNavigation?: ReactNode;
  onRefreshReport?(): Promise<void>;
  onSignOut?(): void;
  report: ReportDto;
  session: DailySession;
}

interface InvitationState {
  email: string;
  expiresAt: string;
  id: string;
  link: string;
  role: 'editor' | 'viewer';
}

interface ShareState {
  expiresAt: string | null;
  id: string;
  link: string;
}

function buildDraft(item: ReportItemDto): DraftState {
  return {
    maxDailyCard: item.maxDailyCard,
    maxFeedback: item.maxFeedback,
    reviewStatus: item.reviewStatus,
  };
}

function normalizeBaseUrl(value?: string): string {
  if (!value) return '';
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

function collaborationOrigin(apiBaseUrl: string): string {
  if (!apiBaseUrl) return window.location.origin;
  try {
    const parsed = new URL(apiBaseUrl);
    if (
      (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
      || parsed.username
      || parsed.password
      || parsed.pathname !== '/'
      || parsed.search
      || parsed.hash
    ) {
      throw new Error('invalid collaboration origin');
    }
    return parsed.origin;
  } catch {
    throw new CollaborationConfigurationError();
  }
}

function buildCollaborationUrl(origin: string, token: string): string {
  return new URL(`/c/${encodeURIComponent(token)}`, origin).toString();
}

function toApiError(error: unknown): (ApiError & { status?: number }) | null {
  if (!error || typeof error !== 'object') return null;
  const candidate = error as Partial<ApiError> & { status?: number };
  if (
    typeof candidate.code === 'string'
    && typeof candidate.message === 'string'
    && typeof candidate.requestId === 'string'
  ) {
    return {
      code: candidate.code,
      message: candidate.message,
      requestId: candidate.requestId,
      status: candidate.status,
    };
  }
  return null;
}

function updateItem(
  report: ReportDto,
  item: ReportItemDto,
): ReportDto {
  return {
    ...report,
    items: report.items.map((entry) => (entry.id === item.id ? item : entry)),
  };
}

export function DailyPage({
  apiBaseUrl,
  apiClient,
  dateNavigation,
  onRefreshReport,
  onSignOut,
  report,
  session,
}: DailyPageProps) {
  const [reportState, setReportState] = useState(report);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(report.items[0]?.id ?? null);
  const [drafts, setDrafts] = useState<Record<string, DraftState>>(() => Object.fromEntries(
    report.items.map((item) => [item.id, buildDraft(item)]),
  ));
  const [saving, setSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [showShareDialog, setShowShareDialog] = useState(false);
  const [dialogLoading, setDialogLoading] = useState(false);
  const [createdInvitations, setCreatedInvitations] = useState<InvitationState[]>([]);
  const [createdShares, setCreatedShares] = useState<ShareState[]>([]);
  const [preferences, setPreferences] = useState(() => loadWorkbenchPreferences(
    typeof window === 'undefined' ? null : window.localStorage,
  ));
  const [filterQuery, setFilterQuery] = useState('');
  const [reviewStatus, setReviewStatus] = useState('all');
  const reportGenerationRef = useRef(0);
  const normalizedBaseUrl = useMemo(() => normalizeBaseUrl(apiBaseUrl), [apiBaseUrl]);

  const visibleItems = useMemo(() => filterAndSortItems(reportState.items, {
    query: filterQuery,
    reviewStatus,
    sortDirection: preferences.sortDirection,
    sortField: preferences.sortField,
  }), [filterQuery, preferences.sortDirection, preferences.sortField, reportState.items, reviewStatus]);

  useLayoutEffect(() => {
    reportGenerationRef.current += 1;
    setReportState(report);
    setSelectedItemId((current) => {
      if (current && report.items.some((item) => item.id === current)) return current;
      return report.items[0]?.id ?? null;
    });
    setDrafts(Object.fromEntries(report.items.map((item) => [item.id, buildDraft(item)])));
    setSaving(false);
    setErrorMessage(null);
  }, [report]);

  useEffect(() => {
    saveWorkbenchPreferences(
      typeof window === 'undefined' ? null : window.localStorage,
      preferences,
    );
  }, [preferences]);

  useEffect(() => {
    setSelectedItemId((current) => {
      if (current && visibleItems.some((item) => item.id === current)) return current;
      return visibleItems[0]?.id ?? null;
    });
  }, [visibleItems]);

  const selectedItem = reportState.items.find((item) => item.id === selectedItemId) ?? null;
  const selectedDraft = selectedItem ? drafts[selectedItem.id] ?? buildDraft(selectedItem) : null;
  const canManagePermissions = Boolean(
    session.mode === 'identity'
      && session.accessToken
      && reportState.accessRole === 'owner',
  );
  const canEditCurrentItem = Boolean(
    selectedItem
      && canEditField(reportState.accessRole, 'max_daily_card')
      && canEditField(reportState.accessRole, 'max_feedback')
      && canEditField(reportState.accessRole, 'review_status'),
  );
  const layoutStyle = {
    '--left-column-width': `${preferences.leftWidth}px`,
    '--right-column-width': `${preferences.rightWidth}px`,
  } as CSSProperties;

  async function saveSelectedItem(): Promise<void> {
    if (!selectedItem || !selectedDraft || !apiClient?.patchItem || session.mode === 'public') return;
    if (session.mode === 'identity' && !session.accessToken) return;

    const fields: Array<[CollaborativeField, keyof DraftState]> = [
      ['max_daily_card', 'maxDailyCard'],
      ['max_feedback', 'maxFeedback'],
      ['review_status', 'reviewStatus'],
    ];
    const changedFields = fields.filter(([, key]) => selectedDraft[key] !== buildDraft(selectedItem)[key]);
    if (changedFields.length === 0) return;

    setSaving(true);
    setErrorMessage(null);
    const reportGeneration = reportGenerationRef.current;
    let currentVersion = selectedItem.version;
    let nextReport = reportState;
    try {
      for (const [field, key] of changedFields) {
        const updated = await apiClient.patchItem({
          accessToken: session.accessToken,
          expectedVersion: currentVersion,
          field,
          itemId: selectedItem.id,
          mode: session.mode,
          value: selectedDraft[key],
        });
        if (reportGenerationRef.current !== reportGeneration) return;
        currentVersion = updated.version;
        nextReport = updateItem(nextReport, updated);
        setReportState((current) => updateItem(current, updated));
        setDrafts((current) => ({
          ...current,
          [selectedItem.id]: {
            ...(current[selectedItem.id] ?? selectedDraft),
            [key]: buildDraft(updated)[key],
          },
        }));
      }
    } catch (error) {
      if (reportGenerationRef.current !== reportGeneration) return;
      const apiError = toApiError(error);
      if (apiError?.status === 409) {
        setErrorMessage(CONFLICT_MESSAGE);
      } else {
        setErrorMessage(apiError?.message ?? '保存失败');
      }
    } finally {
      if (reportGenerationRef.current === reportGeneration) setSaving(false);
    }
  }

  async function refreshReport(): Promise<void> {
    setErrorMessage(null);
    await onRefreshReport?.();
  }

  async function createInvitation(input: {
    email: string;
    role: 'editor' | 'viewer';
  }): Promise<void> {
    if (!apiClient?.createInvitation || !session.accessToken) return;
    setDialogLoading(true);
    try {
      const created = await apiClient.createInvitation({
        accessToken: session.accessToken,
        email: input.email,
        reportId: reportState.id,
        role: input.role,
      });
      const inviteUrl = new URL(window.location.href);
      inviteUrl.searchParams.set('invite', created.token);
      inviteUrl.pathname = `/r/${encodeURIComponent(reportState.id)}`;
      setCreatedInvitations((current) => [
        {
          email: input.email,
          expiresAt: created.expiresAt,
          id: created.id,
          link: inviteUrl.toString(),
          role: input.role,
        },
        ...current.filter((entry) => entry.id !== created.id),
      ]);
    } finally {
      setDialogLoading(false);
    }
  }

  async function revokeInvitation(invitationId: string): Promise<void> {
    if (!apiClient?.revokeInvitation || !session.accessToken) return;
    setDialogLoading(true);
    try {
      await apiClient.revokeInvitation({
        accessToken: session.accessToken,
        invitationId,
      });
      setCreatedInvitations((current) => current.filter((entry) => entry.id !== invitationId));
    } finally {
      setDialogLoading(false);
    }
  }

  async function createShareLink(expiresAt: string | null): Promise<void> {
    if (!apiClient?.createShareLink || !session.accessToken) return;
    setDialogLoading(true);
    try {
      const created = await apiClient.createShareLink({
        accessToken: session.accessToken,
        expiresAt,
        reportId: reportState.id,
      });
      const shareUrl = new URL(`${window.location.origin}/api/shares/exchange`);
      if (normalizedBaseUrl) {
        shareUrl.href = `${normalizedBaseUrl}/api/shares/exchange`;
      }
      shareUrl.searchParams.set('token', created.token);
      setCreatedShares((current) => [
        {
          expiresAt: created.expiresAt,
          id: created.id,
          link: shareUrl.toString(),
        },
        ...current.filter((entry) => entry.id !== created.id),
      ]);
    } finally {
      setDialogLoading(false);
    }
  }

  async function revokeShareLink(shareId: string): Promise<void> {
    if (!apiClient?.revokeShareLink || !session.accessToken) return;
    setDialogLoading(true);
    try {
      await apiClient.revokeShareLink({
        accessToken: session.accessToken,
        shareId,
      });
      setCreatedShares((current) => current.filter((entry) => entry.id !== shareId));
    } finally {
      setDialogLoading(false);
    }
  }

  async function getCurrentCollaborationLink(): Promise<CollaborationLinkStatus> {
    if (!apiClient?.getCurrentCollaborationLink || !session.accessToken) return { active: false };
    return apiClient.getCurrentCollaborationLink({
      accessToken: session.accessToken,
    });
  }

  async function createCollaborationLink(): Promise<{
    createdAt: string;
    id: string;
    url: string;
  }> {
    if (!apiClient?.createCollaborationLink || !session.accessToken) {
      throw new Error('固定协作入口接口未配置');
    }
    const origin = collaborationOrigin(normalizedBaseUrl);
    const created = await apiClient.createCollaborationLink({
      accessToken: session.accessToken,
    });
    return {
      createdAt: created.createdAt,
      id: created.id,
      url: buildCollaborationUrl(origin, created.token),
    };
  }

  async function revokeCollaborationLink(linkId: string): Promise<void> {
    if (!apiClient?.revokeCollaborationLink || !session.accessToken) {
      throw new Error('固定协作入口接口未配置');
    }
    await apiClient.revokeCollaborationLink({
      accessToken: session.accessToken,
      linkId,
    });
  }

  return (
    <>
      <main className="daily-shell">
        <header className="daily-topbar">
          <div className="daily-brand">
            <p className="eyebrow">MAX DAILY INTEL</p>
            <h1>外部情报口喷日报</h1>
            <p className="daily-date">{reportState.dailyDate}</p>
          </div>
          <div className="daily-account">
            <strong className="material-count">{reportState.items.length} 条素材</strong>
            <div className="topbar-actions">
              <span className="role-chip">
                <UserRound size={14} aria-hidden="true" />
                {session.email ?? reportState.accessRole}
              </span>
              <button
                aria-label="刷新日报"
                className="icon-button"
                onClick={() => {
                  void refreshReport();
                }}
                title="刷新日报"
                type="button"
              >
                <RefreshCcw size={16} aria-hidden="true" />
              </button>
              {canManagePermissions ? (
                <button
                  className="secondary-button"
                  onClick={() => setShowShareDialog(true)}
                  type="button"
                >
                  <Shield size={16} aria-hidden="true" />
                  权限设置
                </button>
              ) : null}
              {session.mode === 'identity' && onSignOut ? (
                <button
                  aria-label="退出登录"
                  className="icon-button"
                  onClick={onSignOut}
                  title="退出登录"
                  type="button"
                >
                  <LogOut size={16} aria-hidden="true" />
                </button>
              ) : null}
            </div>
          </div>
        </header>

        {dateNavigation}

        <DailyToolbar
          fields={preferences.fields}
          filterQuery={filterQuery}
          leftWidth={preferences.leftWidth}
          onFieldsChange={(fields) => setPreferences((current) => ({ ...current, fields }))}
          onFilterQueryChange={setFilterQuery}
          onLeftWidthChange={(leftWidth) => setPreferences((current) => ({ ...current, leftWidth }))}
          onReviewStatusChange={setReviewStatus}
          onRightWidthChange={(rightWidth) => setPreferences((current) => ({ ...current, rightWidth }))}
          onRowDensityChange={(rowDensity) => setPreferences((current) => ({ ...current, rowDensity }))}
          onSortDirectionChange={(sortDirection) => setPreferences((current) => ({ ...current, sortDirection }))}
          onSortFieldChange={(sortField) => setPreferences((current) => ({ ...current, sortField }))}
          reviewStatus={reviewStatus}
          rightWidth={preferences.rightWidth}
          rowDensity={preferences.rowDensity}
          sortDirection={preferences.sortDirection}
          sortField={preferences.sortField}
        />

        <section
          className="daily-layout"
          data-density={preferences.rowDensity}
          data-view="workbench"
          style={layoutStyle}
        >
          <MaterialList
            items={visibleItems}
            onSelect={setSelectedItemId}
            role={reportState.accessRole}
            rowDensity={preferences.rowDensity}
            selectedItemId={selectedItemId}
          />

          <VideoPanel
            accessToken={session.accessToken}
            apiBaseUrl={normalizedBaseUrl}
            item={selectedItem}
            mediaMode={reportState.mediaMode}
            role={reportState.accessRole}
            sessionMode={session.mode}
          />

          <div className="mobile-workbench-transcript">
            <TranscriptPanel item={selectedItem} />
          </div>

          <CollaborativePanel
            canEdit={canEditCurrentItem}
            conflict={errorMessage === CONFLICT_MESSAGE}
            draft={selectedDraft}
            errorMessage={errorMessage}
            itemTitle={selectedItem?.title ?? null}
            onChange={(field, value) => {
              if (!selectedItem) return;
              setDrafts((current) => ({
                ...current,
                [selectedItem.id]: {
                  ...(current[selectedItem.id] ?? buildDraft(selectedItem)),
                  [field]: value,
                },
              }));
              if (errorMessage === CONFLICT_MESSAGE) setErrorMessage(CONFLICT_MESSAGE);
            }}
            onRefresh={() => {
              void refreshReport();
            }}
            onSave={() => {
              void saveSelectedItem();
            }}
            saving={saving}
            version={selectedItem?.version ?? null}
          />
        </section>
      </main>

      {canManagePermissions ? (
        <ShareDialog
          invitations={createdInvitations}
          loading={dialogLoading}
          onClose={() => setShowShareDialog(false)}
          onCreateCollaborationLink={createCollaborationLink}
          onCreateInvitation={createInvitation}
          onCreateShareLink={createShareLink}
          onGetCurrentCollaborationLink={getCurrentCollaborationLink}
          onRevokeCollaborationLink={revokeCollaborationLink}
          onRevokeInvitation={revokeInvitation}
          onRevokeShare={revokeShareLink}
          open={showShareDialog}
          shares={createdShares}
        />
      ) : null}
    </>
  );
}
