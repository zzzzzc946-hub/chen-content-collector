import { LoaderCircle, Send } from 'lucide-react';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import type {
  ApiError,
  MobileInboxItemDto,
  MobileInboxSnapshotDto,
  MobileInboxSubmitResult,
} from '../../../../packages/shared/src/contracts.js';

type SubmitState = 'idle' | 'submitting' | 'succeeded' | 'failed';

interface MobileInboxPageProps {
  apiBaseUrl: string;
}

function normalizeBaseUrl(value: string): string {
  if (!value) return '';
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  let message = '提交失败，请稍后重试';
  try {
    const error = await response.json() as Partial<ApiError>;
    if (typeof error.message === 'string' && error.message) message = error.message;
  } catch {
    message = response.statusText || message;
  }
  throw new Error(message);
}

function formatSubmittedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    day: '2-digit',
    hour: '2-digit',
    hour12: false,
    minute: '2-digit',
    month: '2-digit',
    year: 'numeric',
  });
}

function RecentItem({ item }: { item: MobileInboxItemDto }) {
  const platformLabel = item.platform.trim() ? item.platform : '未知';
  return (
    <li className="mobile-inbox-item">
      <div className="mobile-inbox-item-top">
        <span className="mobile-inbox-platform">{platformLabel}</span>
        <span className="mobile-inbox-status">
          <span className="mobile-inbox-status-dot" aria-hidden="true" />
          {item.status}
        </span>
      </div>
      <p className="mobile-inbox-url">{item.url}</p>
      <time dateTime={item.submittedAt}>{formatSubmittedAt(item.submittedAt)}</time>
    </li>
  );
}

export function MobileInboxPage({ apiBaseUrl }: MobileInboxPageProps) {
  const normalizedBaseUrl = normalizeBaseUrl(apiBaseUrl);
  const [snapshot, setSnapshot] = useState<MobileInboxSnapshotDto>({
    items: [],
    pendingCount: 0,
  });
  const [text, setText] = useState('');
  const [note, setNote] = useState('');
  const [submitState, setSubmitState] = useState<SubmitState>('idle');
  const [resultMessage, setResultMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const controllersRef = useRef<Set<AbortController>>(new Set());
  const snapshotRequestSeq = useRef(0);

  const createController = useCallback(() => {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    return controller;
  }, []);

  const releaseController = useCallback((controller: AbortController) => {
    controllersRef.current.delete(controller);
  }, []);

  const loadSnapshot = useCallback(async (controller: AbortController): Promise<void> => {
    const requestSeq = snapshotRequestSeq.current + 1;
    snapshotRequestSeq.current = requestSeq;
    try {
      const response = await fetch(`${normalizedBaseUrl}/api/mobile-inbox`, {
        signal: controller.signal,
      });
      const nextSnapshot = await readJson<MobileInboxSnapshotDto>(response);
      if (controller.signal.aborted || snapshotRequestSeq.current !== requestSeq) return;
      setSnapshot(nextSnapshot);
      setErrorMessage(null);
    } catch (error) {
      if (controller.signal.aborted || snapshotRequestSeq.current !== requestSeq) return;
      setErrorMessage(error instanceof Error ? error.message : '读取失败，请稍后重试');
    } finally {
      releaseController(controller);
    }
  }, [normalizedBaseUrl, releaseController]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'CHEN 链接收集箱';
    return () => {
      document.title = previousTitle;
    };
  }, []);

  useEffect(() => {
    const loadOnce = () => {
      void loadSnapshot(createController());
    };
    loadOnce();
    const timer = window.setInterval(loadOnce, 10_000);
    return () => {
      window.clearInterval(timer);
      controllersRef.current.forEach((controller) => controller.abort());
      controllersRef.current.clear();
    };
  }, [createController, loadSnapshot]);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmedText = text.trim();
    if (!trimmedText || submitState === 'submitting') return;

    const controller = createController();
    setSubmitState('submitting');
    setResultMessage(null);
    setErrorMessage(null);
    try {
      const response = await fetch(`${normalizedBaseUrl}/api/mobile-inbox`, {
        body: JSON.stringify({
          note: note.trim(),
          text: trimmedText,
        }),
        headers: { 'content-type': 'application/json' },
        method: 'POST',
        signal: controller.signal,
      });
      const result = await readJson<MobileInboxSubmitResult>(response);
      if (controller.signal.aborted) return;
      setSnapshot({
        items: result.items,
        pendingCount: result.pendingCount,
      });
      setText('');
      setNote('');
      setSubmitState('succeeded');
      setResultMessage(result.ignoredCount > 0
        ? `新增 ${result.addedCount} 条，已存在 ${result.existingCount} 条，忽略 ${result.ignoredCount} 条`
        : `新增 ${result.addedCount} 条，已存在 ${result.existingCount} 条`);
      void loadSnapshot(createController());
    } catch (error) {
      if (controller.signal.aborted) return;
      setSubmitState('failed');
      setErrorMessage(error instanceof Error ? error.message : '提交失败，请稍后重试');
    } finally {
      releaseController(controller);
    }
  }

  const submitLabel = submitState === 'failed' ? '重新提交' : '提交到待扒取表';
  const canSubmit = text.trim().length > 0 && submitState !== 'submitting';

  return (
    <main className="mobile-inbox-shell">
      <section className="mobile-inbox-panel" aria-labelledby="mobile-inbox-title">
        <header className="mobile-inbox-header">
          <div>
            <p className="eyebrow">MOBILE INBOX</p>
            <h1 id="mobile-inbox-title">CHEN 链接收集箱</h1>
          </div>
          <strong className="mobile-inbox-count">待扒取 {snapshot.pendingCount}</strong>
        </header>

        <form className="mobile-inbox-form" onSubmit={(event) => {
          void submit(event);
        }}>
          <label className="field">
            <span className="field-label">作品链接</span>
            <textarea
              className="field-textarea mobile-inbox-textarea"
              onChange={(event) => setText(event.target.value)}
              placeholder="粘贴分享文案或链接"
              value={text}
            />
          </label>

          <label className="field">
            <span className="field-label">备注（可选）</span>
            <input
              className="field-input"
              onChange={(event) => setNote(event.target.value)}
              placeholder="给待扒取表留一句上下文"
              type="text"
              value={note}
            />
          </label>

          <button className="primary-button mobile-inbox-submit" disabled={!canSubmit} type="submit">
            {submitState === 'submitting' ? (
              <LoaderCircle className="spin" size={18} aria-hidden="true" />
            ) : (
              <Send size={18} aria-hidden="true" />
            )}
            <span>{submitState === 'submitting' ? '提交中' : submitLabel}</span>
          </button>
        </form>

        {resultMessage ? (
          <p className="status-banner status-banner-success">{resultMessage}</p>
        ) : null}
        {errorMessage ? (
          <p className="inline-error">{errorMessage}</p>
        ) : null}
      </section>

      <section className="mobile-inbox-panel" aria-labelledby="mobile-inbox-recent-title">
        <div className="mobile-inbox-section-heading">
          <h2 id="mobile-inbox-recent-title">最近记录</h2>
        </div>
        {snapshot.items.length > 0 ? (
          <ul className="mobile-inbox-list">
            {snapshot.items.map((item) => (
              <RecentItem item={item} key={item.id} />
            ))}
          </ul>
        ) : (
          <p className="empty-state compact-empty">还没有提交记录</p>
        )}
      </section>
    </main>
  );
}
