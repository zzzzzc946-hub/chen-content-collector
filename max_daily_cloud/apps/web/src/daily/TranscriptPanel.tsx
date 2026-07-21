import { ExternalLink, FileText } from 'lucide-react';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts.js';
import { CopyTextButton } from './CopyTextButton';

interface TranscriptPanelProps {
  item: ReportItemDto | null;
}

export function TranscriptPanel({ item }: TranscriptPanelProps) {
  if (!item) {
    return (
      <section className="panel transcript-panel">
        <div className="empty-state">没有符合条件的素材</div>
      </section>
    );
  }

  const sourceUrl = item.sourceUrl.trim();
  return (
    <section className="panel transcript-panel">
      <div className="panel-heading transcript-heading">
        <div>
          <p className="eyebrow">TRANSCRIPT READING</p>
          <h2>{item.title}</h2>
        </div>
        {sourceUrl ? (
          <a className="secondary-button" href={sourceUrl} rel="noreferrer" target="_blank">
            <ExternalLink size={16} aria-hidden="true" />
            打开原视频
          </a>
        ) : null}
      </div>
      <div className="transcript-document">
        <div className="transcript-label">
          <FileText size={17} aria-hidden="true" />
          完整文稿
        </div>
        <p>{item.caption || '当前素材没有文稿'}</p>
        <CopyTextButton label="复制文稿" text={item.caption} />
      </div>
    </section>
  );
}
