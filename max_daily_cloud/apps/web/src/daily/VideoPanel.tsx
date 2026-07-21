import { ExternalLink, Film, RefreshCcw, TriangleAlert } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ReportDto, ReportItemDto, Role } from '../../../../packages/shared/src/contracts.js';

interface VideoPanelProps {
  accessToken: string | null;
  apiBaseUrl: string;
  item: ReportItemDto | null;
  mediaMode: ReportDto['mediaMode'];
  role: Role;
  sessionMode: 'identity' | 'collaborator' | 'public';
}

function normalizeBaseUrl(value: string): string {
  if (!value) return '';
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

export function VideoPanel({
  accessToken,
  apiBaseUrl,
  item,
  mediaMode,
  role,
  sessionMode,
}: VideoPanelProps) {
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [retryAttempt, setRetryAttempt] = useState(0);
  const [sessionReady, setSessionReady] = useState(role === 'public_reader');
  const baseUrl = useMemo(() => normalizeBaseUrl(apiBaseUrl), [apiBaseUrl]);

  useEffect(() => {
    if (mediaMode === 'historical') return undefined;

    if (role === 'public_reader') {
      setSessionReady(true);
      setError(null);
      setLoading(false);
      return undefined;
    }

    const collaboratorSession = sessionMode === 'collaborator' || role === 'collaborator';

    if (!accessToken && !collaboratorSession) {
      setSessionReady(false);
      setError('当前会话没有可用授权');
      setLoading(false);
      return undefined;
    }

    const controller = new AbortController();

    async function bootstrapSession(): Promise<void> {
      setLoading(true);
      setError(null);
      setSessionReady(false);
      try {
        const response = await fetch(`${baseUrl}/api/media/session`, {
          credentials: 'include',
          headers: accessToken && sessionMode === 'identity'
            ? { authorization: `Bearer ${accessToken}` }
            : {},
          method: 'POST',
          signal: controller.signal,
        });
        if (!response.ok) {
          let message = `media_session_failed:${response.status}`;
          try {
            const body = await response.json() as { message?: unknown };
            if (typeof body.message === 'string') message = body.message;
          } catch {
            // Keep the status-based fallback when the response is not JSON.
          }
          throw new Error(message);
        }
        setSessionReady(true);
      } catch (loadError) {
        if (controller.signal.aborted) return;
        setSessionReady(false);
        setError(loadError instanceof Error ? loadError.message : '视频读取失败');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void bootstrapSession();

    return () => {
      controller.abort();
    };
  }, [accessToken, baseUrl, mediaMode, retryAttempt, role, sessionMode]);

  const mediaUrl = mediaMode === 'current_day' && item?.mediaId && sessionReady
    ? `${baseUrl}/api/media/${encodeURIComponent(item.mediaId)}`
    : null;
  const visibleError = error ?? (!item?.mediaId ? '当前素材没有可用视频' : null);
  const sourceUrl = item?.sourceUrl.trim() ?? '';

  if (!item) {
    return (
      <section className="panel video-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">外部情报口喷日报</p>
            <h2>视频面板</h2>
          </div>
        </div>
        <div className="empty-state">选择一条素材后显示视频与快照。</div>
      </section>
    );
  }

  if (mediaMode === 'historical') {
    return (
      <section className="panel video-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">外部情报口喷日报</p>
            <h2>{item.title}</h2>
          </div>
          {sourceUrl ? (
            <a
              className="secondary-button"
              href={sourceUrl}
              rel="noreferrer"
              target="_blank"
            >
              <ExternalLink size={16} aria-hidden="true" />
              打开原视频
            </a>
          ) : null}
        </div>

        <dl className="video-metadata">
          <div>
            <dt>来源</dt>
            <dd>{sourceUrl || '无'}</dd>
          </div>
          <div className="video-metadata-transcript">
            <dt>文稿</dt>
            <dd>{item.caption || '无'}</dd>
          </div>
          <div>
            <dt>当前版本</dt>
            <dd>
              <RefreshCcw size={14} aria-hidden="true" />
              v{item.version}
            </dd>
          </div>
        </dl>
      </section>
    );
  }

  return (
    <section className="panel video-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">外部情报口喷日报</p>
          <h2>{item.title}</h2>
        </div>
        {sourceUrl ? (
          <a
            className="icon-button"
            href={sourceUrl}
            rel="noreferrer"
            target="_blank"
            title="打开来源"
          >
            <ExternalLink size={16} aria-hidden="true" />
          </a>
        ) : null}
      </div>

      <div className="video-shell">
        {mediaUrl ? (
          <video
            className="video-frame"
            controls
            crossOrigin="use-credentials"
            playsInline
            preload="metadata"
            src={mediaUrl}
          />
        ) : (
          <div className="empty-state video-empty">
            <Film size={28} aria-hidden="true" />
            {loading ? '视频载入中…' : visibleError ?? '暂无视频'}
          </div>
        )}
      </div>

      {error && !loading ? (
        <div className="action-row">
          <p className="inline-error">
            <TriangleAlert size={14} aria-hidden="true" />
            {error}
          </p>
          {role !== 'public_reader' ? (
            <button
              className="secondary-button"
              onClick={() => setRetryAttempt((current) => current + 1)}
              type="button"
            >
              <RefreshCcw size={14} aria-hidden="true" />
              重试
            </button>
          ) : null}
        </div>
      ) : null}

      <dl className="video-metadata">
        <div>
          <dt>来源</dt>
          <dd>{sourceUrl || '无'}</dd>
        </div>
        <div className="video-metadata-transcript">
          <dt>文稿</dt>
          <dd>{item.caption || '无'}</dd>
        </div>
        <div>
          <dt>当前版本</dt>
          <dd>
            <RefreshCcw size={14} aria-hidden="true" />
            v{item.version}
          </dd>
        </div>
      </dl>
    </section>
  );
}
