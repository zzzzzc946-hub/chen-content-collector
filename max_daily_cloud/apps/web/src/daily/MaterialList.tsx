import { Clapperboard, PlayCircle, Radio, ShieldCheck } from 'lucide-react';
import type { ReportItemDto, Role } from '../../../../packages/shared/src/contracts.js';
import type { RowDensity } from './workbenchPreferences.js';

interface MaterialListProps {
  items: ReportItemDto[];
  role: Role;
  rowDensity?: RowDensity;
  selectedItemId: string | null;
  onSelect(itemId: string): void;
}

function roleLabel(role: Role): string {
  if (role === 'owner') return 'Owner';
  if (role === 'editor') return 'Editor';
  if (role === 'viewer') return 'Viewer';
  if (role === 'collaborator') return '协作者 Collaborator';
  return 'Public';
}

export function MaterialList({
  items,
  role,
  rowDensity = 'standard',
  selectedItemId,
  onSelect,
}: MaterialListProps) {
  return (
    <section className="panel material-panel" aria-labelledby="timeline-heading" data-density={rowDensity}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">DAILY MATERIALS</p>
          <h2 id="timeline-heading">当天素材</h2>
        </div>
        <span className="role-chip">
          <ShieldCheck size={14} aria-hidden="true" />
          {roleLabel(role)}
        </span>
      </div>

      {items.length === 0 ? (
        <div className="empty-state compact-empty">没有符合条件的素材</div>
      ) : (
        <ol className="timeline-list material-carousel">
          {items.map((item, index) => {
          const active = item.id === selectedItemId;
          return (
            <li key={item.id}>
              <button
                aria-pressed={active}
                className={`timeline-item${active ? ' is-active' : ''}`}
                onClick={() => onSelect(item.id)}
                type="button"
              >
                <span className="timeline-index">{String(index + 1).padStart(2, '0')}</span>
                <span className="timeline-copy">
                  <strong>{item.title}</strong>
                  <span>{item.reviewStatus || 'pending'}</span>
                </span>
                <span className="timeline-meta">
                  <Clapperboard size={16} aria-hidden="true" />
                  {item.mediaId ? <PlayCircle size={16} aria-hidden="true" /> : <Radio size={16} aria-hidden="true" />}
                </span>
              </button>
            </li>
          );
          })}
        </ol>
      )}
    </section>
  );
}
