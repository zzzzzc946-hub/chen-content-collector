import { Check, Copy, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';

interface CopyTextButtonProps {
  label: string;
  text: string;
}

function fallbackCopy(text: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.dataset.copyFallback = 'true';
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return typeof document.execCommand === 'function' && document.execCommand('copy');
  } finally {
    textarea.remove();
  }
}

export function CopyTextButton({ label, text }: CopyTextButtonProps) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle');

  useEffect(() => {
    setStatus('idle');
  }, [text]);

  useEffect(() => {
    if (status === 'idle') {
      return undefined;
    }

    const timer = window.setTimeout(() => setStatus('idle'), 2000);
    return () => window.clearTimeout(timer);
  }, [status]);

  async function copy(): Promise<void> {
    try {
      if (typeof navigator.clipboard?.writeText === 'function') {
        try {
          await navigator.clipboard.writeText(text);
          setStatus('copied');
          return;
        } catch {
          // Fall through to the legacy copy path when the Clipboard API rejects.
        }
      }

      setStatus(fallbackCopy(text) ? 'copied' : 'failed');
    } catch {
      setStatus('failed');
    }
  }

  const visibleLabel = status === 'copied' ? '已复制' : status === 'failed' ? '复制失败' : label;
  const Icon = status === 'copied' ? Check : status === 'failed' ? TriangleAlert : Copy;

  return (
    <button className="secondary-button copy-text-button" disabled={!text} onClick={() => void copy()} type="button">
      <Icon aria-hidden="true" size={16} />
      {visibleLabel}
    </button>
  );
}
