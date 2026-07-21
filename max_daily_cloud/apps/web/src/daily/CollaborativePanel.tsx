import { Lock, RefreshCcw, Save, TriangleAlert, Unlock } from 'lucide-react';
import { CopyTextButton } from './CopyTextButton';

interface CollaborativeDraft {
  maxDailyCard: string;
  maxFeedback: string;
  reviewStatus: string;
}

interface CollaborativePanelProps {
  canEdit: boolean;
  conflict: boolean;
  draft: CollaborativeDraft | null;
  errorMessage: string | null;
  itemTitle: string | null;
  onChange(field: keyof CollaborativeDraft, value: string): void;
  onRefresh(): void;
  onSave(): void;
  saving: boolean;
  version: number | null;
}

export function CollaborativePanel({
  canEdit,
  conflict,
  draft,
  errorMessage,
  itemTitle,
  onChange,
  onRefresh,
  onSave,
  saving,
  version,
}: CollaborativePanelProps) {
  return (
    <section className="panel collaborative-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">MAX DAILY INTEL</p>
          <h2>MAX口喷卡片</h2>
        </div>
        <span className="role-chip">
          {canEdit ? <Unlock size={14} aria-hidden="true" /> : <Lock size={14} aria-hidden="true" />}
          {version ? `v${version}` : '只读'}
        </span>
      </div>

      <p className="section-subtitle">{itemTitle ?? '未选择素材'}</p>

      <label className="field">
        <span className="field-label">MAX口喷卡片</span>
        <textarea
          aria-label="MAX口喷卡片"
          className="field-textarea max-daily-card-textarea"
          disabled={!canEdit || !draft}
          onChange={(event) => onChange('maxDailyCard', event.target.value)}
          rows={8}
          value={draft?.maxDailyCard ?? ''}
        />
      </label>
      <CopyTextButton label="复制口喷卡片" text={draft?.maxDailyCard ?? ''} />

      <label className="field">
        <span className="field-label">MAX反馈</span>
        <textarea
          aria-label="MAX反馈"
          className="field-textarea"
          disabled={!canEdit || !draft}
          onChange={(event) => onChange('maxFeedback', event.target.value)}
          rows={5}
          value={draft?.maxFeedback ?? ''}
        />
      </label>

      <label className="field">
        <span className="field-label">审核状态</span>
        <select
          aria-label="审核状态"
          className="field-input"
          disabled={!canEdit || !draft}
          onChange={(event) => onChange('reviewStatus', event.target.value)}
          value={draft?.reviewStatus ?? ''}
        >
          <option value="pending">pending</option>
          <option value="approved">approved</option>
          <option value="hold">hold</option>
          <option value="rejected">rejected</option>
        </select>
      </label>

      {errorMessage ? (
        <p className="inline-error">
          <TriangleAlert size={14} aria-hidden="true" />
          {errorMessage}
        </p>
      ) : null}

      <div className="action-row">
        {canEdit ? (
          <button
            className="primary-button"
            disabled={!draft || saving}
            onClick={onSave}
            type="button"
          >
            <Save size={16} aria-hidden="true" />
            保存修改
          </button>
        ) : null}
        {conflict ? (
          <button
            className="secondary-button"
            onClick={onRefresh}
            type="button"
          >
            <RefreshCcw size={16} aria-hidden="true" />
            刷新内容
          </button>
        ) : null}
      </div>
    </section>
  );
}
