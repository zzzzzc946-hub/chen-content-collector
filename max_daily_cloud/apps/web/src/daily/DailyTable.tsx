import { Check, ExternalLink, Film, Minus, Radio } from 'lucide-react';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts.js';
import type { TableField } from './workbenchPreferences.js';

interface DailyTableProps {
  fields: TableField[];
  items: ReportItemDto[];
  onSelect(itemId: string): void;
  selectedItemId: string | null;
}

const FIELD_LABELS: Record<TableField, string> = {
  caption: '文稿',
  max_daily_card: 'MAX口喷卡片',
  max_feedback: 'MAX反馈',
  media: '视频',
  review_status: '审核状态',
  source: '来源',
  version: '版本',
};

function fieldValue(item: ReportItemDto, field: TableField) {
  if (field === 'source') {
    return item.sourceUrl ? (
      <a href={item.sourceUrl} rel="noreferrer" target="_blank" title="打开来源">
        <ExternalLink size={15} aria-hidden="true" />
        原链接
      </a>
    ) : <Minus size={15} aria-label="无来源" />;
  }
  if (field === 'review_status') return item.reviewStatus || 'pending';
  if (field === 'media') {
    return item.mediaId
      ? <span className="table-media-state"><Film size={15} aria-hidden="true" />可播放</span>
      : <span className="table-media-state"><Radio size={15} aria-hidden="true" />仅链接</span>;
  }
  if (field === 'version') return `v${item.version}`;
  if (field === 'caption') return item.caption || '无';
  if (field === 'max_daily_card') return item.maxDailyCard || '无';
  return item.maxFeedback || '无';
}

export function DailyTable({ fields, items, onSelect, selectedItemId }: DailyTableProps) {
  return (
    <section className="panel table-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">DAILY OVERVIEW</p>
          <h2>表格总览</h2>
        </div>
        <span className="role-chip">{items.length} 条素材</span>
      </div>

      {items.length === 0 ? (
        <div className="empty-state">没有符合条件的素材</div>
      ) : (
        <div className="daily-table-scroll">
          <table className="daily-table">
            <thead>
              <tr>
                <th scope="col">序号</th>
                <th scope="col">标题</th>
                {fields.map((field) => <th key={field} scope="col">{FIELD_LABELS[field]}</th>)}
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => {
                const selected = item.id === selectedItemId;
                return (
                  <tr className={selected ? 'is-selected' : ''} key={item.id}>
                    <td>{String(index + 1).padStart(2, '0')}</td>
                    <td className="table-title-cell">
                      <button
                        aria-label={`选择${item.title}`}
                        onClick={() => onSelect(item.id)}
                        type="button"
                      >
                        {selected ? <Check size={15} aria-hidden="true" /> : null}
                        {item.title}
                      </button>
                    </td>
                    {fields.map((field) => <td key={field}>{fieldValue(item, field)}</td>)}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
