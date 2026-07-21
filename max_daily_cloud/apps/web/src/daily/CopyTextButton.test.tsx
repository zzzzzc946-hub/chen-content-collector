// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CopyTextButton } from './CopyTextButton';

describe('CopyTextButton', () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('copies the exact text and shows success feedback', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<CopyTextButton label="复制文稿" text={'第一行\n第二行'} />);

    const button = screen.getByRole('button', { name: '复制文稿' });
    expect(button).not.toBeDisabled();
    await user.click(button);

    expect(writeText).toHaveBeenCalledWith('第一行\n第二行');
    expect(screen.getByRole('button', { name: '已复制' })).toBeVisible();
  });

  it('disables copying when the text is empty', () => {
    render(<CopyTextButton label="复制文稿" text="" />);

    expect(screen.getByRole('button', { name: '复制文稿' })).toBeDisabled();
  });

  it('falls back to execCommand when Clipboard API is unavailable', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });
    render(<CopyTextButton label="复制口喷卡片" text="当前草稿" />);

    await user.click(screen.getByRole('button', { name: '复制口喷卡片' }));

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(screen.getByRole('button', { name: '已复制' })).toBeVisible();
    expect(document.querySelector('[data-copy-fallback]')).toBeNull();
  });

  it('focuses and fully selects the fallback textarea before copying', async () => {
    const user = userEvent.setup();
    const text = '需要完整选择的正文\n第二行';
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const focus = vi.spyOn(HTMLTextAreaElement.prototype, 'focus');
    const select = vi.spyOn(HTMLTextAreaElement.prototype, 'select');
    const setSelectionRange = vi.spyOn(HTMLTextAreaElement.prototype, 'setSelectionRange');
    const execCommand = vi.fn().mockImplementation(() => {
      const textarea = document.activeElement;
      expect(textarea).toBeInstanceOf(HTMLTextAreaElement);
      expect(textarea).toHaveAttribute('data-copy-fallback', 'true');
      expect((textarea as HTMLTextAreaElement).value).toBe(text);
      expect((textarea as HTMLTextAreaElement).selectionStart).toBe(0);
      expect((textarea as HTMLTextAreaElement).selectionEnd).toBe(text.length);
      return true;
    });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });
    render(<CopyTextButton label="复制文稿" text={text} />);

    await user.click(screen.getByRole('button', { name: '复制文稿' }));

    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(select).toHaveBeenCalledTimes(1);
    expect(setSelectionRange).toHaveBeenCalledWith(0, text.length);
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(document.querySelector('[data-copy-fallback]')).toBeNull();
  });

  it('falls back to execCommand when Clipboard API rejects', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });
    render(<CopyTextButton label="复制文稿" text="正文" />);

    await user.click(screen.getByRole('button', { name: '复制文稿' }));

    expect(writeText).toHaveBeenCalledWith('正文');
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(screen.getByRole('button', { name: '已复制' })).toBeVisible();
  });

  it('removes the fallback textarea and shows failure when execCommand throws', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn(() => {
        throw new Error('copy unavailable');
      }),
    });
    render(<CopyTextButton label="复制文稿" text="正文" />);

    await user.click(screen.getByRole('button', { name: '复制文稿' }));

    expect(screen.getByRole('button', { name: '复制失败' })).toBeVisible();
    expect(document.querySelector('[data-copy-fallback]')).toBeNull();
  });

  it('restores the default label after two seconds', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(true) });
    render(<CopyTextButton label="复制文稿" text="正文" />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '复制文稿' }));
      await Promise.resolve();
    });
    expect(screen.getByRole('button', { name: '已复制' })).toBeVisible();

    act(() => {
      vi.advanceTimersByTime(2000);
    });

    expect(screen.getByRole('button', { name: '复制文稿' })).toBeVisible();
    vi.useRealTimers();
  });

  it('resets the copy feedback immediately when text changes', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { rerender } = render(<CopyTextButton label="复制文稿" text="旧正文" />);

    await user.click(screen.getByRole('button', { name: '复制文稿' }));
    expect(screen.getByRole('button', { name: '已复制' })).toBeVisible();

    rerender(<CopyTextButton label="复制文稿" text="新正文" />);

    expect(screen.getByRole('button', { name: '复制文稿' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '已复制' })).toBeNull();
  });

  it('shows failure when neither copy path succeeds', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(false) });
    render(<CopyTextButton label="复制文稿" text="正文" />);

    await user.click(screen.getByRole('button', { name: '复制文稿' }));

    expect(screen.getByRole('button', { name: '复制失败' })).toBeVisible();
  });
});
