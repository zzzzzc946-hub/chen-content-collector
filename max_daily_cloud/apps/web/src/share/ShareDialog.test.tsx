// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
// @ts-expect-error Vitest runs this static CSS assertion in Node.
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApiError } from '../../../../packages/shared/src/contracts';
import { ShareDialog } from './ShareDialog';

const invitation = {
  email: 'max@example.com',
  expiresAt: '2026-07-12T09:00:00.000Z',
  id: 'invitation-1',
  link: 'https://daily.example.com/r/report-1?invite=token',
  role: 'editor' as const,
};

const share = {
  expiresAt: null,
  id: 'share-1',
  link: 'https://worker.example.com/api/shares/exchange?token=token',
};

function apiError(message: string): ApiError {
  return { code: 'request_failed', message, requestId: 'request-1' };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, reject, resolve };
}

function renderDialog(input: Partial<React.ComponentProps<typeof ShareDialog>> = {}) {
  const props: React.ComponentProps<typeof ShareDialog> = {
    invitations: [],
    loading: false,
    onClose: vi.fn(),
    onCreateInvitation: vi.fn(async () => undefined),
    onCreateShareLink: vi.fn(async () => undefined),
    onRevokeInvitation: vi.fn(async () => undefined),
    onRevokeShare: vi.fn(async () => undefined),
    open: true,
    shares: [],
    ...input,
  };
  const renderResult = render(<ShareDialog {...props} />);
  return { props, ...renderResult };
}

afterEach(cleanup);

describe('ShareDialog errors', () => {
  it('preserves invitation and share creation messages from the Worker', async () => {
    const user = userEvent.setup();
    renderDialog({
      onCreateInvitation: vi.fn(async () => {
        throw apiError('该邮箱已经有待领取邀请');
      }),
      onCreateShareLink: vi.fn(async () => {
        throw apiError('分享链接创建频率过高，请稍后重试');
      }),
    });

    await user.type(screen.getByLabelText('邮箱'), 'max@example.com');
    await user.click(screen.getByRole('button', { name: '发送邀请' }));
    expect(await screen.findByText('该邮箱已经有待领取邀请')).toBeVisible();

    await user.click(screen.getByRole('button', { name: '创建只读链接' }));
    expect(await screen.findByText('分享链接创建频率过高，请稍后重试')).toBeVisible();
  });

  it('keeps failed invitation and share revokes visible and retryable', async () => {
    const user = userEvent.setup();
    const revokeInvitation = vi.fn(async () => {
      throw apiError('邀请已经被领取，无法撤销');
    });
    const revokeShare = vi.fn(async () => {
      throw apiError('分享链接撤销失败，请重试');
    });
    renderDialog({
      invitations: [invitation],
      onRevokeInvitation: revokeInvitation,
      onRevokeShare: revokeShare,
      shares: [share],
    });

    await user.click(screen.getByRole('button', { name: '撤销邀请' }));
    expect(await screen.findByText('邀请已经被领取，无法撤销')).toBeVisible();
    expect(screen.getByText('max@example.com')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '撤销邀请' }));
    expect(revokeInvitation).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole('button', { name: '撤销只读链接' }));
    expect(await screen.findByText('分享链接撤销失败，请重试')).toBeVisible();
    expect(screen.getByText('Public Reader')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '撤销只读链接' }));
    expect(revokeShare).toHaveBeenCalledTimes(2);
  });
});

describe('ShareDialog keyboard behavior', () => {
  it('takes focus, traps Tab, and closes on Escape', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog({ onClose });

    const closeButton = screen.getByRole('button', { name: '关闭权限设置' });
    await waitFor(() => expect(closeButton).toHaveFocus());

    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByRole('button', { name: '创建只读链接' })).toHaveFocus();
    await user.tab();
    expect(closeButton).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('ShareDialog mobile reachability styles', () => {
  it('keeps the overlay viewport-bound and scrolls the dialog panel on narrow screens', () => {
    const styles = readFileSync('apps/web/src/styles.css', 'utf8') as string;
    const scrimRule = styles.match(/\.dialog-scrim\s*\{(?<body>[^}]+)\}/)?.groups?.body ?? '';
    const panelRules = Array.from(styles.matchAll(/(?:^|[,\s])\.dialog-panel\s*\{(?<body>[^}]+)\}/gm))
      .map((match: RegExpMatchArray) => match.groups?.body ?? '')
      .join('\n');

    expect(scrimRule).toMatch(/overflow:\s*hidden/);
    expect(scrimRule).toMatch(/padding:\s*max\(12px,\s*env\(safe-area-inset-top\)\)/);
    expect(panelRules).toMatch(/max-height:\s*calc\(100dvh - 24px\)/);
    expect(panelRules).toMatch(/overflow-y:\s*auto/);
  });
});

describe('ShareDialog fixed collaboration link management', () => {
  it('keeps Create unavailable after status failure and retries status without posting', async () => {
    const user = userEvent.setup();
    const createCollaborationLink = vi.fn(async () => ({
      createdAt: '2026-07-15T08:00:00.000Z',
      id: 'collab-1',
      url: 'https://api.example.com/c/secret',
    }));
    const getCurrentCollaborationLink = vi.fn()
      .mockRejectedValueOnce(new Error('upstream leaked details token=secret'))
      .mockResolvedValueOnce({ active: false as const });
    renderDialog({
      onCreateCollaborationLink: createCollaborationLink,
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('固定协作入口状态读取失败，请重试。');
    expect(screen.queryByText('当前没有有效固定协作链接。')).toBeNull();
    expect(screen.queryByRole('button', { name: '创建固定协作链接' })).toBeNull();
    expect(screen.queryByText(/token=secret/)).toBeNull();
    expect(createCollaborationLink).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '重试读取固定链接状态' }));

    expect(await screen.findByText('当前没有有效固定协作链接。')).toBeVisible();
    expect(screen.getByRole('button', { name: '创建固定协作链接' })).toBeEnabled();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);
    expect(createCollaborationLink).not.toHaveBeenCalled();
  });

  it('creates one permanent collaboration link, labels its scope, and copies it once', async () => {
    const user = userEvent.setup();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const createCollaborationLink = vi.fn(async () => ({
      createdAt: '2026-07-15T08:00:00.000Z',
      id: 'collab-1',
      url: 'https://api.example.com/c/raw%20secret',
    }));
    renderDialog({
      onCreateCollaborationLink: createCollaborationLink,
      onGetCurrentCollaborationLink: vi.fn(async () => ({ active: false as const })),
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    expect(screen.getByText('固定协作入口')).toBeVisible();
    expect(screen.getByText('持有此链接的人无需登录，可查看已发布日报，且仅可编辑 MAX 卡片、反馈和审核状态。')).toBeVisible();

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));

    expect(await screen.findByDisplayValue('https://api.example.com/c/raw%20secret')).toBeVisible();
    expect(clipboard).toHaveBeenCalledTimes(1);
    expect(clipboard).toHaveBeenCalledWith('https://api.example.com/c/raw%20secret');
    expect(await screen.findByText('固定链接已创建并复制。')).toBeVisible();
  });

  it('shows active status without re-showing the original secret link', async () => {
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-1',
        url: 'https://api.example.com/c/secret',
      })),
      onGetCurrentCollaborationLink: vi.fn(async () => ({
        active: true,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-1',
        lastUsedAt: '2026-07-15T09:30:00.000Z',
      })),
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(screen.getByText(/原始秘密链接只会在创建时显示一次/)).toBeVisible();
    expect(screen.getByText(new RegExp(new Date('2026-07-15T08:00:00.000Z')
      .toLocaleString('zh-CN', { hour12: false })))).toBeVisible();
    expect(screen.getByText(new RegExp(new Date('2026-07-15T09:30:00.000Z')
      .toLocaleString('zh-CN', { hour12: false })))).toBeVisible();
    expect(screen.queryByDisplayValue('https://api.example.com/c/secret')).toBeNull();
  });

  it('revokes the active link only after explicit confirmation', async () => {
    const user = userEvent.setup();
    const revokeCollaborationLink = vi.fn(async () => undefined);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-1',
        url: 'https://api.example.com/c/secret',
      })),
      onGetCurrentCollaborationLink: vi.fn(async () => ({
        active: true,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-1',
        lastUsedAt: null,
      })),
      onRevokeCollaborationLink: revokeCollaborationLink,
    });

    const revokeButton = await screen.findByRole('button', { name: '撤销固定协作链接' });
    await user.click(revokeButton);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(revokeCollaborationLink).not.toHaveBeenCalled();

    await user.click(revokeButton);
    expect(revokeCollaborationLink).toHaveBeenCalledWith('collab-1');
    expect(await screen.findByText('固定链接已撤销。')).toBeVisible();
    expect(screen.queryByRole('button', { name: '撤销固定协作链接' })).toBeNull();
  });

  it('reconciles after a stale create commits after the reopened status read', async () => {
    const user = userEvent.setup();
    const pendingCreate = deferred<{
      createdAt: string;
      id: string;
      url: string;
    }>();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const reopenedStatus = deferred<{ active: false }>();
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockImplementationOnce(() => reopenedStatus.promise)
      .mockResolvedValueOnce({
        active: true as const,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'stale-collab',
        lastUsedAt: null,
      });
    const { props, rerender } = renderDialog({
      onCreateCollaborationLink: vi.fn(() => pendingCreate.promise),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    rerender(<ShareDialog {...props} open={false} />);
    rerender(<ShareDialog {...props} open />);

    await act(async () => {
      reopenedStatus.resolve({ active: false });
    });
    expect(await screen.findByText('当前没有有效固定协作链接。')).toBeVisible();

    await act(async () => {
      pendingCreate.resolve({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'stale-collab',
        url: 'https://api.example.com/c/stale-secret',
      });
    });

    expect(screen.queryByDisplayValue('https://api.example.com/c/stale-secret')).toBeNull();
    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(clipboard).not.toHaveBeenCalled();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(3);
  });

  it('waits until the next open to reconcile a create that resolves while closed', async () => {
    const user = userEvent.setup();
    const pendingCreate = deferred<{
      createdAt: string;
      id: string;
      url: string;
    }>();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockResolvedValueOnce({
        active: true as const,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'closed-collab',
        lastUsedAt: null,
      });
    const { props, rerender } = renderDialog({
      onCreateCollaborationLink: vi.fn(() => pendingCreate.promise),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    rerender(<ShareDialog {...props} open={false} />);
    await act(async () => {
      pendingCreate.resolve({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'closed-collab',
        url: 'https://api.example.com/c/closed-secret',
      });
    });

    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(1);
    expect(clipboard).not.toHaveBeenCalled();

    rerender(<ShareDialog {...props} open />);
    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(screen.queryByDisplayValue('https://api.example.com/c/closed-secret')).toBeNull();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);
  });

  it('does not reconcile or copy after a pending create unmounts', async () => {
    const user = userEvent.setup();
    const pendingCreate = deferred<{
      createdAt: string;
      id: string;
      url: string;
    }>();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const getCurrentCollaborationLink = vi.fn(async () => ({ active: false as const }));
    const { unmount } = renderDialog({
      onCreateCollaborationLink: vi.fn(() => pendingCreate.promise),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    unmount();
    await act(async () => {
      pendingCreate.resolve({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'unmounted-collab',
        url: 'https://api.example.com/c/unmounted-secret',
      });
    });

    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(1);
    expect(clipboard).not.toHaveBeenCalled();
  });

  it('preserves the one-time URL when revoke reconciliation confirms the same link', async () => {
    const user = userEvent.setup();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockResolvedValueOnce({
        active: true as const,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-old',
        lastUsedAt: null,
      });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-old',
        url: 'https://api.example.com/c/current-secret',
      })),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => {
        throw { status: 500, message: 'ambiguous revoke' };
      }),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    expect(await screen.findByDisplayValue('https://api.example.com/c/current-secret')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '撤销固定协作链接' }));

    expect(await screen.findByText('固定协作入口撤销未完成；已重新读取当前状态。')).toBeVisible();
    expect(screen.getByDisplayValue('https://api.example.com/c/current-secret')).toBeVisible();
    expect(clipboard).toHaveBeenCalledTimes(1);
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);
  });

  it('reconciles a rotated active id after a conflicting revoke', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(async () => undefined) },
    });
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockResolvedValueOnce({
        active: true as const,
        createdAt: '2026-07-15T09:00:00.000Z',
        id: 'collab-rotated',
        lastUsedAt: null,
      });
    const revokeCollaborationLink = vi.fn()
      .mockRejectedValueOnce({ status: 409, message: 'secret internal conflict' })
      .mockResolvedValueOnce(undefined);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-old',
        url: 'https://api.example.com/c/old-secret',
      })),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: revokeCollaborationLink,
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    expect(await screen.findByDisplayValue('https://api.example.com/c/old-secret')).toBeVisible();
    await user.click(await screen.findByRole('button', { name: '撤销固定协作链接' }));

    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(screen.queryByDisplayValue('https://api.example.com/c/old-secret')).toBeNull();
    expect(screen.getByText(new RegExp(new Date('2026-07-15T09:00:00.000Z')
      .toLocaleString('zh-CN', { hour12: false })))).toBeVisible();
    expect(screen.queryByText(/secret internal conflict/)).toBeNull();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole('button', { name: '撤销固定协作链接' }));
    expect(revokeCollaborationLink).toHaveBeenNthCalledWith(2, 'collab-rotated');
  });

  it('reconciles inactive status after a revoke returns not found', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(async () => undefined) },
    });
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockResolvedValueOnce({ active: false as const });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-old',
        url: 'https://api.example.com/c/old-secret',
      })),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => {
        throw { status: 404, message: 'database row missing' };
      }),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));
    expect(await screen.findByDisplayValue('https://api.example.com/c/old-secret')).toBeVisible();
    await user.click(await screen.findByRole('button', { name: '撤销固定协作链接' }));

    expect(await screen.findByText('固定链接已撤销。')).toBeVisible();
    expect(screen.queryByDisplayValue('https://api.example.com/c/old-secret')).toBeNull();
    expect(screen.getByText('当前没有有效固定协作链接。')).toBeVisible();
    expect(screen.queryByText(/database row missing/)).toBeNull();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);
  });

  it('reconciles an active link when the create response is lost without exposing a secret', async () => {
    const user = userEvent.setup();
    const getCurrentCollaborationLink = vi.fn()
      .mockResolvedValueOnce({ active: false as const })
      .mockResolvedValueOnce({
        active: true as const,
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-created',
        lastUsedAt: null,
      });
    renderDialog({
      onCreateCollaborationLink: vi.fn(async () => {
        throw new Error('network failed after token=raw-secret');
      }),
      onGetCurrentCollaborationLink: getCurrentCollaborationLink,
      onRevokeCollaborationLink: vi.fn(async () => undefined),
    });

    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));

    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(screen.getByText('创建结果未确认；已重新读取当前状态。原始秘密链接无法重新显示。')).toBeVisible();
    expect(screen.queryByText(/raw-secret/)).toBeNull();
    expect(screen.queryByLabelText('固定协作链接')).toBeNull();
    expect(getCurrentCollaborationLink).toHaveBeenCalledTimes(2);
  });
});
