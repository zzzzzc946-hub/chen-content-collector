import { CalendarDays, LoaderCircle, TriangleAlert } from 'lucide-react';
import { useRef } from 'react';
import type { KeyboardEvent } from 'react';
import type { ReportIndexEntryDto } from '../../../../packages/shared/src/contracts.js';

interface ReportDateNavProps {
  entries: ReportIndexEntryDto[];
  errorMessage?: string | null;
  loadingReportId?: string | null;
  onSelect(reportId: string): void;
  selectedReportId: string | null;
}

export function ReportDateNav({
  entries,
  errorMessage = null,
  loadingReportId = null,
  onSelect,
  selectedReportId,
}: ReportDateNavProps) {
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function focusButton(index: number): void {
    const next = buttonRefs.current[index];
    next?.focus();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault();
      focusButton((index + 1) % entries.length);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault();
      focusButton((index - 1 + entries.length) % entries.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      focusButton(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      focusButton(entries.length - 1);
    }
  }

  return (
    <nav className="report-date-nav" aria-label="日报日期">
      <div className="report-date-nav-heading">
        <CalendarDays size={17} aria-hidden="true" />
        <span>日报日期</span>
      </div>

      {entries.length > 0 ? (
        <div className="report-date-list" role="list">
          {entries.map((entry, index) => {
            const selected = entry.id === selectedReportId;
            const loading = entry.id === loadingReportId;
            return (
              <button
                aria-busy={loading ? 'true' : undefined}
                aria-current={selected ? 'date' : undefined}
                aria-pressed={selected}
                className={`report-date-button${selected ? ' is-selected' : ''}`}
                key={entry.id}
                onClick={() => onSelect(entry.id)}
                onKeyDown={(event) => handleKeyDown(event, index)}
                ref={(element) => {
                  buttonRefs.current[index] = element;
                }}
                type="button"
              >
                <span className="report-date-main">{entry.dailyDate}</span>
                <span className="report-date-meta">
                  {entry.itemCount}
                  {' '}
                  条素材
                </span>
                {loading ? (
                  <span className="report-date-loading">
                    <LoaderCircle className="spin" size={14} aria-hidden="true" />
                    正在切换
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="empty-state compact-empty">还没有已发布日报</div>
      )}

      {errorMessage ? (
        <p className="inline-error report-date-error">
          <TriangleAlert size={14} aria-hidden="true" />
          {errorMessage}
        </p>
      ) : null}
    </nav>
  );
}
