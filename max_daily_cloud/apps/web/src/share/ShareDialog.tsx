import {
  CheckCircle2,
  Copy,
  Eye,
  KeyRound,
  Link2,
  LoaderCircle,
  MailPlus,
  Shield,
  Trash2,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

interface InvitationRecord {
  email: string;
  expiresAt: string;
  id: string;
  link: string;
  role: 'editor' | 'viewer';
}

interface ShareRecord {
  expiresAt: string | null;
  id: string;
  link: string;
}

export type CollaborationLinkStatus =
  | { active: false }
  | {
    active: true;
    createdAt: string;
    id: string;
    lastUsedAt: string | null;
  };

export const COLLABORATION_CONFIG_ERROR_MESSAGE = '固定协作入口地址配置无效，请联系管理员。';

export class CollaborationConfigurationError extends Error {
  constructor() {
    super(COLLABORATION_CONFIG_ERROR_MESSAGE);
    this.name = 'CollaborationConfigurationError';
  }
}

type CollaborationStatusState =
  | { phase: 'loading' }
  | { phase: 'ready'; status: CollaborationLinkStatus }
  | { phase: 'error' };

interface CollaborationLinkCreateResult {
  createdAt: string;
  id: string;
  url: string;
}

interface OneTimeCollaborationLink {
  id: string;
  url: string;
}

interface ShareDialogProps {
  invitations: InvitationRecord[];
  loading: boolean;
  open: boolean;
  shares: ShareRecord[];
  onClose(): void;
  onCreateInvitation(input: { email: string; role: 'editor' | 'viewer' }): Promise<void>;
  onCreateCollaborationLink?(): Promise<CollaborationLinkCreateResult>;
  onCreateShareLink(expiresAt: string | null): Promise<void>;
  onGetCurrentCollaborationLink?(): Promise<CollaborationLinkStatus>;
  onRevokeInvitation(invitationId: string): Promise<void>;
  onRevokeCollaborationLink?(linkId: string): Promise<void>;
  onRevokeShare(shareId: string): Promise<void>;
}

function formatDateTime(value: string | null): string {
  if (!value) return '永不失效';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

function formatLastUsed(value: string | null): string {
  return value ? formatDateTime(value) : '尚未使用';
}

async function copyText(value: string): Promise<void> {
  await navigator.clipboard.writeText(value);
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  if (error && typeof error === 'object') {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message) return message;
  }
  return fallback;
}

function isConfigurationError(error: unknown): boolean {
  return error instanceof CollaborationConfigurationError;
}

export function ShareDialog({
  invitations,
  loading,
  open,
  shares,
  onClose,
  onCreateInvitation,
  onCreateCollaborationLink,
  onCreateShareLink,
  onGetCurrentCollaborationLink,
  onRevokeInvitation,
  onRevokeCollaborationLink,
  onRevokeShare,
}: ShareDialogProps) {
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<'editor' | 'viewer'>('editor');
  const [shareExpiresAt, setShareExpiresAt] = useState('');
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [collaborationState, setCollaborationState] = useState<CollaborationStatusState>({
    phase: 'loading',
  });
  const [collaborationBusy, setCollaborationBusy] = useState(false);
  const [collaborationError, setCollaborationError] = useState<string | null>(null);
  const [collaborationSuccess, setCollaborationSuccess] = useState<string | null>(null);
  const [oneTimeCollaborationLink, setOneTimeCollaborationLink] = useState<
    OneTimeCollaborationLink | null
  >(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const collaborationOperationRef = useRef(0);
  const mountedRef = useRef(false);
  const openRef = useRef(open);
  const onCreateCollaborationLinkRef = useRef(onCreateCollaborationLink);
  const onGetCurrentCollaborationLinkRef = useRef(onGetCurrentCollaborationLink);
  const onRevokeCollaborationLinkRef = useRef(onRevokeCollaborationLink);
  onCreateCollaborationLinkRef.current = onCreateCollaborationLink;
  onGetCurrentCollaborationLinkRef.current = onGetCurrentCollaborationLink;
  onRevokeCollaborationLinkRef.current = onRevokeCollaborationLink;
  openRef.current = open;
  const canManageCollaboration = Boolean(
    onCreateCollaborationLink
      && onGetCurrentCollaborationLink
      && onRevokeCollaborationLink,
  );

  const hasContent = useMemo(
    () => invitations.length > 0
      || shares.length > 0
      || Boolean(oneTimeCollaborationLink)
      || (collaborationState.phase === 'ready' && collaborationState.status.active),
    [collaborationState, invitations.length, oneTimeCollaborationLink, shares.length],
  );

  function beginCollaborationOperation(): number {
    collaborationOperationRef.current += 1;
    return collaborationOperationRef.current;
  }

  function isCurrentCollaborationOperation(operation: number): boolean {
    return collaborationOperationRef.current === operation;
  }

  function handleClose(): void {
    beginCollaborationOperation();
    setOneTimeCollaborationLink(null);
    setCollaborationError(null);
    setCollaborationSuccess(null);
    onClose();
  }

  const onCloseRef = useRef(handleClose);
  onCloseRef.current = handleClose;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      beginCollaborationOperation();
    };
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    closeButtonRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [])].filter((element) => element.getAttribute('aria-hidden') !== 'true');
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) {
        event.preventDefault();
        return;
      }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open || !canManageCollaboration) {
      beginCollaborationOperation();
      setOneTimeCollaborationLink(null);
      setCollaborationError(null);
      setCollaborationSuccess(null);
      setCollaborationBusy(false);
      setCollaborationState({ phase: 'loading' });
      return undefined;
    }

    void loadCurrentCollaborationStatus();
    return () => {
      beginCollaborationOperation();
    };
  }, [canManageCollaboration, open]);

  async function loadCurrentCollaborationStatus(): Promise<void> {
    const getCurrent = onGetCurrentCollaborationLinkRef.current;
    if (!getCurrent) return;
    const operation = beginCollaborationOperation();
    setCollaborationState({ phase: 'loading' });
    setCollaborationBusy(false);
    setCollaborationError(null);
    setCollaborationSuccess(null);
    try {
      const status = await getCurrent();
      if (!isCurrentCollaborationOperation(operation)) return;
      setOneTimeCollaborationLink((current) => (
        current && (!status.active || status.id !== current.id) ? null : current
      ));
      setCollaborationState({ phase: 'ready', status });
    } catch {
      if (!isCurrentCollaborationOperation(operation)) return;
      setCollaborationState({ phase: 'error' });
    }
  }

  async function reconcileCreateFailure(operation: number): Promise<void> {
    if (!isCurrentCollaborationOperation(operation)) return;
    const getCurrent = onGetCurrentCollaborationLinkRef.current;
    if (!getCurrent) return;
    const reconciliation = beginCollaborationOperation();
    setCollaborationState({ phase: 'loading' });
    try {
      const status = await getCurrent();
      if (!isCurrentCollaborationOperation(reconciliation)) return;
      setCollaborationState({ phase: 'ready', status });
      if (status.active) {
        setCollaborationSuccess('创建结果未确认；已重新读取当前状态。原始秘密链接无法重新显示。');
      } else {
        setCollaborationError('固定协作入口创建失败，请重试。');
      }
    } catch {
      if (!isCurrentCollaborationOperation(reconciliation)) return;
      setCollaborationState({ phase: 'error' });
    } finally {
      if (isCurrentCollaborationOperation(reconciliation)) setCollaborationBusy(false);
    }
  }

  async function reconcileRevokeFailure(operation: number, linkId: string): Promise<void> {
    if (!isCurrentCollaborationOperation(operation)) return;
    const getCurrent = onGetCurrentCollaborationLinkRef.current;
    if (!getCurrent) return;
    const reconciliation = beginCollaborationOperation();
    setCollaborationState({ phase: 'loading' });
    try {
      const status = await getCurrent();
      if (!isCurrentCollaborationOperation(reconciliation)) return;
      if (!status.active || status.id !== linkId) setOneTimeCollaborationLink(null);
      setCollaborationState({ phase: 'ready', status });
      if (status.active) {
        setCollaborationError('固定协作入口撤销未完成；已重新读取当前状态。');
      } else {
        setCollaborationSuccess('固定链接已撤销。');
      }
    } catch {
      if (!isCurrentCollaborationOperation(reconciliation)) return;
      setCollaborationState({ phase: 'error' });
    } finally {
      if (isCurrentCollaborationOperation(reconciliation)) setCollaborationBusy(false);
    }
  }

  async function handleCreateCollaborationLink(): Promise<void> {
    const createCollaborationLink = onCreateCollaborationLinkRef.current;
    if (!createCollaborationLink || collaborationState.phase !== 'ready') return;
    const operation = beginCollaborationOperation();
    setCollaborationBusy(true);
    setCollaborationError(null);
    setCollaborationSuccess(null);
    try {
      const created = await createCollaborationLink();
      if (!isCurrentCollaborationOperation(operation)) {
        if (mountedRef.current && openRef.current) void loadCurrentCollaborationStatus();
        return;
      }
      setOneTimeCollaborationLink({ id: created.id, url: created.url });
      setCollaborationState({
        phase: 'ready',
        status: {
          active: true,
          createdAt: created.createdAt,
          id: created.id,
          lastUsedAt: null,
        },
      });
      try {
        await copyText(created.url);
        if (!isCurrentCollaborationOperation(operation)) return;
        setCollaborationSuccess('固定链接已创建并复制。');
      } catch {
        if (!isCurrentCollaborationOperation(operation)) return;
        setCollaborationSuccess('固定链接已创建，请手动复制。');
      }
    } catch (error) {
      if (!isCurrentCollaborationOperation(operation)) return;
      setOneTimeCollaborationLink(null);
      if (isConfigurationError(error)) {
        setCollaborationError(COLLABORATION_CONFIG_ERROR_MESSAGE);
      } else {
        await reconcileCreateFailure(operation);
      }
    } finally {
      if (isCurrentCollaborationOperation(operation)) setCollaborationBusy(false);
    }
  }

  async function handleRevokeCollaborationLink(): Promise<void> {
    const revokeCollaborationLink = onRevokeCollaborationLinkRef.current;
    if (
      !revokeCollaborationLink
      || collaborationState.phase !== 'ready'
      || !collaborationState.status.active
    ) return;
    if (!window.confirm('确定撤销当前固定协作链接？持有旧链接的人将无法继续进入。')) return;
    const linkId = collaborationState.status.id;
    const operation = beginCollaborationOperation();
    setCollaborationBusy(true);
    setCollaborationError(null);
    setCollaborationSuccess(null);
    try {
      await revokeCollaborationLink(linkId);
      if (!isCurrentCollaborationOperation(operation)) return;
      setOneTimeCollaborationLink(null);
      setCollaborationState({ phase: 'ready', status: { active: false } });
      setCollaborationSuccess('固定链接已撤销。');
    } catch {
      await reconcileRevokeFailure(operation, linkId);
    } finally {
      if (isCurrentCollaborationOperation(operation)) setCollaborationBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div className="dialog-scrim" role="presentation">
      <div
        aria-labelledby="permission-dialog-title"
        aria-modal="true"
        className="dialog-panel"
        ref={dialogRef}
        role="dialog"
      >
        <div className="panel-heading">
          <div>
            <p className="eyebrow">MAX DAILY INTEL</p>
            <h2 id="permission-dialog-title">权限设置</h2>
          </div>
          <button
            aria-label="关闭权限设置"
            className="icon-button"
            onClick={handleClose}
            ref={closeButtonRef}
            type="button"
            title="关闭"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="dialog-grid">
          <section className="dialog-section">
            <div className="section-title">
              <MailPlus size={16} aria-hidden="true" />
              协作邀请
            </div>
            <label className="field">
              <span className="field-label">邮箱</span>
              <input
                className="field-input"
                onChange={(event) => setInviteEmail(event.target.value)}
                placeholder="editor@example.com"
                type="email"
                value={inviteEmail}
              />
            </label>
            <label className="field">
              <span className="field-label">角色</span>
              <select
                className="field-input"
                onChange={(event) => setInviteRole(event.target.value as 'editor' | 'viewer')}
                value={inviteRole}
              >
                <option value="editor">editor</option>
                <option value="viewer">viewer</option>
              </select>
            </label>
            <button
              className="primary-button"
              disabled={loading || !inviteEmail.trim()}
              onClick={async () => {
                try {
                  setDialogError(null);
                  await onCreateInvitation({
                    email: inviteEmail.trim(),
                    role: inviteRole,
                  });
                  setInviteEmail('');
                } catch (error) {
                  setDialogError(errorMessage(error, '邀请创建失败'));
                }
              }}
              type="button"
            >
              <Shield size={16} aria-hidden="true" />
              发送邀请
            </button>

            <ul className="share-list">
              {invitations.map((invitation) => (
                <li className="share-row" key={invitation.id}>
                  <div>
                    <strong>{invitation.email}</strong>
                    <span>{invitation.role}</span>
                    <span>{formatDateTime(invitation.expiresAt)}</span>
                  </div>
                  <div className="row-actions">
                    <button
                      aria-label="复制邀请链接"
                      className="icon-button"
                      onClick={() => {
                        void copyText(invitation.link);
                      }}
                      title="复制邀请链接"
                      type="button"
                    >
                      <Copy size={16} aria-hidden="true" />
                    </button>
                    <button
                      aria-label="撤销邀请"
                      className="icon-button"
                      disabled={loading}
                      onClick={async () => {
                        try {
                          setDialogError(null);
                          await onRevokeInvitation(invitation.id);
                        } catch (error) {
                          setDialogError(errorMessage(error, '邀请撤销失败'));
                        }
                      }}
                      title="撤销邀请"
                      type="button"
                    >
                      <Trash2 size={16} aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="dialog-section">
            <div className="section-title">
              <Link2 size={16} aria-hidden="true" />
              公开只读链接
            </div>
            <label className="field">
              <span className="field-label">失效时间</span>
              <input
                className="field-input"
                onChange={(event) => setShareExpiresAt(event.target.value)}
                type="datetime-local"
                value={shareExpiresAt}
              />
            </label>
            <button
              className="primary-button"
              disabled={loading}
              onClick={async () => {
                try {
                  setDialogError(null);
                  await onCreateShareLink(
                    shareExpiresAt
                      ? new Date(shareExpiresAt).toISOString()
                      : null,
                  );
                  setShareExpiresAt('');
                } catch (error) {
                  setDialogError(errorMessage(error, '分享链接创建失败'));
                }
              }}
              type="button"
            >
              <Eye size={16} aria-hidden="true" />
              创建只读链接
            </button>

            <ul className="share-list">
              {shares.map((share) => (
                <li className="share-row" key={share.id}>
                  <div>
                    <strong>Public Reader</strong>
                    <span>{formatDateTime(share.expiresAt)}</span>
                  </div>
                  <div className="row-actions">
                    <button
                      aria-label="复制只读链接"
                      className="icon-button"
                      onClick={() => {
                        void copyText(share.link);
                      }}
                      title="复制只读链接"
                      type="button"
                    >
                      <Copy size={16} aria-hidden="true" />
                    </button>
                    <button
                      aria-label="撤销只读链接"
                      className="icon-button"
                      disabled={loading}
                      onClick={async () => {
                        try {
                          setDialogError(null);
                          await onRevokeShare(share.id);
                        } catch (error) {
                          setDialogError(errorMessage(error, '分享链接撤销失败'));
                        }
                      }}
                      title="撤销只读链接"
                      type="button"
                    >
                      <Trash2 size={16} aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>

          {canManageCollaboration ? (
            <section className="dialog-section fixed-link-section">
              <div className="section-title">
                <KeyRound size={16} aria-hidden="true" />
                固定协作入口
              </div>
              <p className="section-subtitle">
                持有此链接的人无需登录，可查看已发布日报，且仅可编辑 MAX 卡片、反馈和审核状态。
              </p>

              {collaborationBusy || collaborationState.phase === 'loading' ? (
                <p className="status-banner">
                  <LoaderCircle className="spin" size={16} aria-hidden="true" />
                  正在处理固定协作入口…
                </p>
              ) : null}

              {collaborationState.phase === 'ready' && collaborationState.status.active ? (
                <div className="fixed-link-status" aria-live="polite">
                  <strong>当前固定链接有效</strong>
                  <span>创建时间：{formatDateTime(collaborationState.status.createdAt)}</span>
                  <span>上次使用：{formatLastUsed(collaborationState.status.lastUsedAt)}</span>
                  <span>原始秘密链接只会在创建时显示一次；刷新后不能重新显示。可撤销后创建新链接。</span>
                </div>
              ) : collaborationState.phase === 'ready' ? (
                <p className="section-subtitle">当前没有有效固定协作链接。</p>
              ) : null}

              {oneTimeCollaborationLink ? (
                <label className="field">
                  <span className="field-label">仅本次显示的固定链接</span>
                  <div className="copy-field-row">
                    <input
                      aria-label="固定协作链接"
                      className="field-input"
                      readOnly
                      value={oneTimeCollaborationLink.url}
                    />
                    <button
                      aria-label="复制固定协作链接"
                      className="icon-button"
                      onClick={async () => {
                        try {
                          await copyText(oneTimeCollaborationLink.url);
                          setCollaborationSuccess('固定链接已复制。');
                        } catch (error) {
                          setCollaborationError(errorMessage(error, '固定链接复制失败'));
                        }
                      }}
                      title="复制固定协作链接"
                      type="button"
                    >
                      <Copy size={16} aria-hidden="true" />
                    </button>
                  </div>
                </label>
              ) : null}

              <div className="action-row">
                {collaborationState.phase === 'error' ? (
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void loadCurrentCollaborationStatus();
                    }}
                    type="button"
                  >
                    <LoaderCircle size={16} aria-hidden="true" />
                    重试读取固定链接状态
                  </button>
                ) : collaborationState.phase === 'ready' && collaborationState.status.active ? (
                  <button
                    className="secondary-button danger-button"
                    disabled={loading || collaborationBusy}
                    onClick={() => {
                      void handleRevokeCollaborationLink();
                    }}
                    type="button"
                  >
                    <Trash2 size={16} aria-hidden="true" />
                    撤销固定协作链接
                  </button>
                ) : collaborationState.phase === 'ready' ? (
                  <button
                    className="primary-button"
                    disabled={loading || collaborationBusy}
                    onClick={() => {
                      void handleCreateCollaborationLink();
                    }}
                    type="button"
                  >
                    <KeyRound size={16} aria-hidden="true" />
                    创建固定协作链接
                  </button>
                ) : null}
              </div>

              {collaborationSuccess ? (
                <p className="status-banner status-banner-success" role="status">
                  <CheckCircle2 size={16} aria-hidden="true" />
                  {collaborationSuccess}
                </p>
              ) : null}
              {collaborationError ? (
                <p className="inline-error" role="alert">{collaborationError}</p>
              ) : null}
              {collaborationState.phase === 'error' ? (
                <p className="inline-error" role="alert">固定协作入口状态读取失败，请重试。</p>
              ) : null}
            </section>
          ) : null}
        </div>

        {dialogError ? (
          <p className="inline-error" role="alert">{dialogError}</p>
        ) : null}
        {!hasContent ? <div className="empty-state compact-empty">暂无已创建记录。</div> : null}
      </div>
    </div>
  );
}
