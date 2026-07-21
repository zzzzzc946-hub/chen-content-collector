// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApiError, ReportDto, Role } from '../../../../packages/shared/src/contracts';
import { DailyPage } from './DailyPage';
import { DEFAULT_WORKBENCH_PREFERENCES } from './workbenchPreferences';

interface SessionStub {
  accessToken: string | null;
  email: string | null;
  mode: 'identity' | 'collaborator' | 'public';
}

function createSession(role: Role): SessionStub {
  return {
    accessToken: role === 'public_reader' ? null : `${role}-token`,
    email: role === 'public_reader' ? null : `${role}@example.com`,
    mode: role === 'public_reader' ? 'public' : 'identity',
  };
}

function createReport(accessRole: Role = 'owner'): ReportDto {
  return {
    accessRole,
    dailyDate: '2026-07-11',
    id: 'report-1',
    mediaMode: 'current_day',
    items: [
      {
        caption: '原始文稿',
        id: 'item-1',
        localRecordId: 'local-1',
        maxDailyCard: '初始卡片',
        maxFeedback: '初始反馈',
        mediaId: 'media-1',
        reviewStatus: 'pending',
        sourceUrl: 'https://example.com/item-1',
        title: '素材一',
        version: 1,
      },
      {
        caption: '第二条原始文稿',
        id: 'item-2',
        localRecordId: 'local-2',
        maxDailyCard: '第二条卡片',
        maxFeedback: '',
        mediaId: 'media-2',
        reviewStatus: 'approved',
        sourceUrl: 'https://example.com/item-2',
        title: '素材二',
        version: 3,
      },
    ],
    publishedVersion: 4,
    status: 'published',
  };
}

function deferred<T>() {
  let reject!: (reason?: unknown) => void;
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, reject, resolve };
}

describe('DailyPage', () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  it('shows editing and permission controls only to allowed roles', async () => {
    const report = createReport('editor');
    const editorSession = createSession('editor');
    const ownerSession = createSession('owner');
    const viewerSession = createSession('viewer');

    const { rerender } = render(
      <DailyPage report={report} session={editorSession} />,
    );

    expect(screen.getByLabelText('MAX口喷卡片')).toBeEnabled();
    expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();

    rerender(<DailyPage report={createReport('owner')} session={ownerSession} />);
    expect(screen.getByRole('button', { name: '权限设置' })).toBeVisible();

    rerender(<DailyPage report={createReport('viewer')} session={viewerSession} />);
    expect(screen.getByLabelText('MAX口喷卡片')).toBeDisabled();
  });

  it('copies an unsaved collaborator card draft while retaining feedback, review, and save controls', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });

    render(
      <DailyPage
        report={createReport('collaborator')}
        session={{ accessToken: null, email: null, mode: 'collaborator' }}
      />,
    );

    const cardField = screen.getByLabelText('MAX口喷卡片');
    await user.clear(cardField);
    await user.type(cardField, '新口喷草稿');
    await user.click(screen.getByRole('button', { name: '复制口喷卡片' }));

    expect(writeText).toHaveBeenCalledWith('新口喷草稿');
    expect(await screen.findByRole('button', { name: '已复制' })).toBeVisible();
    expect(screen.getByLabelText('MAX反馈')).toBeVisible();
    expect(screen.getByLabelText('审核状态')).toBeVisible();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeVisible();
  });

  it('renders the real report date, material count, and workbench settings', () => {
    render(
      <DailyPage
        report={createReport('viewer')}
        session={createSession('viewer')}
      />,
    );

    expect(screen.getByText('2026-07-11')).toBeVisible();
    expect(screen.getByText('2 条素材')).toBeVisible();
    expect(screen.queryByRole('tab')).toBeNull();
    expect(screen.getByRole('button', { name: '筛选' })).toBeVisible();
  });

  it('orders the workbench as materials, video, mobile transcript, and collaboration', () => {
    render(
      <DailyPage
        report={createReport('viewer')}
        session={createSession('viewer')}
      />,
    );

    const workbench = document.querySelector('.daily-layout[data-view="workbench"]');

    expect(workbench).not.toBeNull();
    expect(Array.from(workbench!.children, (element) => element.className)).toEqual([
      'panel material-panel',
      'panel video-panel',
      'mobile-workbench-transcript',
      'panel collaborative-panel',
    ]);
    expect(workbench!.querySelector('.mobile-workbench-transcript a')).toHaveAttribute(
      'href',
      'https://example.com/item-1',
    );
  });

  it('ignores a persisted alternate view and always opens the workbench', () => {
    window.localStorage.setItem('max-daily-workbench-preferences:v1', JSON.stringify({
      ...DEFAULT_WORKBENCH_PREFERENCES,
      viewMode: 'table',
    }));

    render(
      <DailyPage
        report={createReport('viewer')}
        session={createSession('viewer')}
      />,
    );

    expect(screen.queryByRole('tab')).toBeNull();
    expect(document.querySelector('.daily-layout[data-view="workbench"]')).not.toBeNull();
    expect(document.querySelector('.video-panel')).not.toBeNull();
    expect(screen.getByText('完整文稿')).toBeVisible();
    expect(screen.getByLabelText('MAX口喷卡片')).toBeDisabled();
  });

  it('filters the visible materials without changing trusted permissions', async () => {
    render(
      <DailyPage
        report={createReport('viewer')}
        session={createSession('viewer')}
      />,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: '筛选' }));
    await user.type(screen.getByRole('searchbox', { name: '筛选素材' }), '第二条');

    expect(screen.queryByText('素材一')).toBeNull();
    expect(screen.getAllByText('素材二').length).toBeGreaterThan(0);
    expect(screen.getByLabelText('MAX口喷卡片')).toBeDisabled();
  });

  it('does not let metadata or local storage elevate a trusted viewer role', () => {
    window.localStorage.setItem('max-daily-role:report-1:attacker', 'owner');
    const forgedSession = {
      accessToken: 'viewer-token',
      app_metadata: { role: 'owner' },
      email: 'viewer@example.com',
      mode: 'identity',
      role: 'owner',
      user_metadata: { max_daily_role: 'owner' },
    } as SessionStub & { role: 'owner' };

    render(
      <DailyPage
        report={createReport('viewer')}
        session={forgedSession}
      />,
    );

    expect(screen.getByLabelText('MAX口喷卡片')).toBeDisabled();
    expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
  });

  it('keeps unsaved text after an HTTP 409 conflict and shows the required message', async () => {
    const report = createReport();
    const editorSession = createSession('editor');
    const patchItem = vi.fn().mockRejectedValue({
      code: 'stale_version',
      message: 'item version is stale',
      requestId: 'req-409',
      status: 409,
    } satisfies ApiError & { status: number });

    render(
      <DailyPage
        report={report}
        session={editorSession}
        apiClient={{ patchItem }}
      />,
    );

    const user = userEvent.setup();
    const cardField = screen.getByLabelText('MAX口喷卡片');
    await user.clear(cardField);
    await user.type(cardField, '冲突后保留的未保存文案');
    await user.click(screen.getByRole('button', { name: '保存修改' }));

    await waitFor(() => {
      expect(patchItem).toHaveBeenCalledWith({
        accessToken: 'editor-token',
        expectedVersion: 1,
        field: 'max_daily_card',
        itemId: 'item-1',
        mode: 'identity',
        value: '冲突后保留的未保存文案',
      });
    });

    expect(await screen.findByText('内容已被其他人更新，请刷新后继续编辑')).toBeVisible();
    expect(screen.getByLabelText('MAX口喷卡片')).toHaveValue('冲突后保留的未保存文案');
  });

  it('lets a collaborator save the editable fields with a cookie session', async () => {
    const report = createReport('collaborator');
    const patchItem = vi.fn(async (input: {
      expectedVersion: number;
      field: string;
      itemId: string;
      value: string;
    }) => ({
      ...report.items[0],
      [input.field === 'max_daily_card' ? 'maxDailyCard' : input.field === 'max_feedback' ? 'maxFeedback' : 'reviewStatus']: input.value,
      version: input.expectedVersion + 1,
    }));

    render(
      <DailyPage
        report={report}
        session={{ accessToken: null, email: null, mode: 'collaborator' }}
        apiClient={{ patchItem }}
      />,
    );

    const user = userEvent.setup();
    await user.clear(screen.getByLabelText('MAX口喷卡片'));
    await user.type(screen.getByLabelText('MAX口喷卡片'), '协作者卡片');
    await user.clear(screen.getByLabelText('MAX反馈'));
    await user.type(screen.getByLabelText('MAX反馈'), '协作者反馈');
    await user.selectOptions(screen.getByLabelText('审核状态'), 'approved');
    await user.click(screen.getByRole('button', { name: '保存修改' }));

    await waitFor(() => {
      expect(patchItem).toHaveBeenCalledTimes(3);
    });
    expect(patchItem).toHaveBeenNthCalledWith(1, {
      accessToken: null,
      expectedVersion: 1,
      field: 'max_daily_card',
      itemId: 'item-1',
      mode: 'collaborator',
      value: '协作者卡片',
    });
    expect(patchItem).toHaveBeenNthCalledWith(2, {
      accessToken: null,
      expectedVersion: 2,
      field: 'max_feedback',
      itemId: 'item-1',
      mode: 'collaborator',
      value: '协作者反馈',
    });
    expect(patchItem).toHaveBeenNthCalledWith(3, {
      accessToken: null,
      expectedVersion: 3,
      field: 'review_status',
      itemId: 'item-1',
      mode: 'collaborator',
      value: 'approved',
    });
  });

  it('commits successful field saves immediately and retries only remaining drafts with the latest version', async () => {
    const report = createReport('collaborator');
    const firstCardSave = deferred<ReportDto['items'][number]>();
    const patchItem = vi.fn((input: {
      expectedVersion: number;
      field: string;
      itemId: string;
      value: string;
    }) => {
      if (patchItem.mock.calls.length === 1) return firstCardSave.promise;
      if (patchItem.mock.calls.length === 2) {
        return Promise.reject({
          code: 'write_failed',
          message: '反馈保存失败，请重试',
          requestId: 'req-feedback',
          status: 500,
        });
      }
      if (patchItem.mock.calls.length === 3) {
        return Promise.resolve({
          ...report.items[0],
          maxDailyCard: '已保存卡片',
          maxFeedback: input.value,
          version: 3,
        });
      }
      return Promise.resolve({
        ...report.items[0],
        maxDailyCard: '已保存卡片',
        maxFeedback: '待保存反馈',
        reviewStatus: input.value,
        version: 4,
      });
    });

    render(
      <DailyPage
        apiClient={{ patchItem }}
        report={report}
        session={{ accessToken: null, email: null, mode: 'collaborator' }}
      />,
    );

    const user = userEvent.setup();
    await user.clear(screen.getByLabelText('MAX口喷卡片'));
    await user.type(screen.getByLabelText('MAX口喷卡片'), '已保存卡片');
    await user.clear(screen.getByLabelText('MAX反馈'));
    await user.type(screen.getByLabelText('MAX反馈'), '待保存反馈');
    await user.selectOptions(screen.getByLabelText('审核状态'), 'approved');
    await user.click(screen.getByRole('button', { name: '保存修改' }));

    expect(patchItem).toHaveBeenCalledTimes(1);
    await act(async () => {
      firstCardSave.resolve({
        ...report.items[0],
        maxDailyCard: '已保存卡片',
        version: 2,
      });
    });

    expect(await screen.findByText('反馈保存失败，请重试')).toBeVisible();
    expect(screen.getByLabelText('MAX口喷卡片')).toHaveValue('已保存卡片');
    expect(screen.getByLabelText('MAX反馈')).toHaveValue('待保存反馈');
    expect(screen.getByLabelText('审核状态')).toHaveValue('approved');
    expect(screen.getAllByText('v2').length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: '保存修改' }));

    await waitFor(() => {
      expect(patchItem).toHaveBeenCalledTimes(4);
    });
    expect(patchItem).toHaveBeenNthCalledWith(3, {
      accessToken: null,
      expectedVersion: 2,
      field: 'max_feedback',
      itemId: 'item-1',
      mode: 'collaborator',
      value: '待保存反馈',
    });
    expect(patchItem).toHaveBeenNthCalledWith(4, {
      accessToken: null,
      expectedVersion: 3,
      field: 'review_status',
      itemId: 'item-1',
      mode: 'collaborator',
      value: 'approved',
    });
  });

  it.each(['resolve', 'reject'] as const)(
    'ignores an old save %s after the active report changes',
    async (outcome) => {
      const oldReport = createReport('collaborator');
      const newReport: ReportDto = {
        ...createReport('collaborator'),
        dailyDate: '2026-07-12',
        id: 'report-2',
        items: [{
          ...createReport('collaborator').items[0],
          id: 'report-2-item',
          maxDailyCard: '新报告卡片',
          title: '新报告素材',
        }],
      };
      const pending = deferred<ReportDto['items'][number]>();
      const patchItem = vi.fn(() => pending.promise);
      const { rerender } = render(
        <DailyPage
          apiClient={{ patchItem }}
          report={oldReport}
          session={{ accessToken: null, email: null, mode: 'collaborator' }}
        />,
      );
      const user = userEvent.setup();
      await user.clear(screen.getByLabelText('MAX口喷卡片'));
      await user.type(screen.getByLabelText('MAX口喷卡片'), '旧报告待保存卡片');
      await user.click(screen.getByRole('button', { name: '保存修改' }));
      expect(patchItem).toHaveBeenCalledTimes(1);

      rerender(
        <DailyPage
          apiClient={{ patchItem }}
          report={newReport}
          session={{ accessToken: null, email: null, mode: 'collaborator' }}
        />,
      );

      await waitFor(() => {
        expect(screen.getAllByText('新报告素材').length).toBeGreaterThan(0);
      });
      expect(screen.getByLabelText('MAX口喷卡片')).toHaveValue('新报告卡片');
      expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();

      await act(async () => {
        if (outcome === 'resolve') {
          pending.resolve({
            ...oldReport.items[0],
            maxDailyCard: '旧报告待保存卡片',
            version: 2,
          });
        } else {
          pending.reject(new Error('old save failed'));
        }
      });

      expect(screen.getAllByText('新报告素材').length).toBeGreaterThan(0);
      expect(screen.getByLabelText('MAX口喷卡片')).toHaveValue('新报告卡片');
      expect(screen.queryByText('保存失败')).toBeNull();
      expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
    },
  );

  it('does not render login, logout, or owner permission controls for a collaborator', () => {
    render(
      <DailyPage
        report={createReport('collaborator')}
        session={{ accessToken: null, email: null, mode: 'collaborator' }}
        onSignOut={vi.fn()}
      />,
    );

    expect(screen.getByText('collaborator')).toBeVisible();
    expect(screen.queryByRole('button', { name: '发送验证码' })).toBeNull();
    expect(screen.queryByRole('button', { name: '退出登录' })).toBeNull();
    expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
  });

  it('loads current fixed collaboration status when an owner opens the permission panel', async () => {
    const user = userEvent.setup();
    const getCurrentCollaborationLink = vi.fn(async () => ({
      active: true,
      createdAt: '2026-07-15T08:00:00.000Z',
      id: 'collab-1',
      lastUsedAt: null,
    }));

    render(
      <DailyPage
        apiClient={{ getCurrentCollaborationLink }}
        report={createReport('owner')}
        session={createSession('owner')}
      />,
    );

    await user.click(screen.getByRole('button', { name: '权限设置' }));

    expect(await screen.findByText('当前固定链接有效')).toBeVisible();
    expect(getCurrentCollaborationLink).toHaveBeenCalledWith({ accessToken: 'owner-token' });
  });

  it('builds and copies the fixed collaboration URL without storing the raw token', async () => {
    const user = userEvent.setup();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    const localStorageSet = vi.spyOn(window.localStorage.__proto__, 'setItem');
    const sessionStorageSet = vi.spyOn(window.sessionStorage.__proto__, 'setItem');
    const createCollaborationLink = vi.fn(async () => ({
      createdAt: '2026-07-15T08:00:00.000Z',
      id: 'collab-1',
      token: 'raw/token with spaces',
    }));

    render(
      <DailyPage
        apiBaseUrl="https://api.example.com/"
        apiClient={{
          createCollaborationLink,
          getCurrentCollaborationLink: vi.fn(async () => ({ active: false as const })),
        }}
        report={createReport('owner')}
        session={createSession('owner')}
      />,
    );

    await user.click(screen.getByRole('button', { name: '权限设置' }));
    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));

    const expectedUrl = 'https://api.example.com/c/raw%2Ftoken%20with%20spaces';
    expect(await screen.findByDisplayValue(expectedUrl)).toBeVisible();
    expect(clipboard).toHaveBeenCalledWith(expectedUrl);
    expect(createCollaborationLink).toHaveBeenCalledWith({ accessToken: 'owner-token' });
    expect(localStorageSet.mock.calls.flat().join('\n')).not.toContain('raw/token with spaces');
    expect(sessionStorageSet.mock.calls.flat().join('\n')).not.toContain('raw/token with spaces');
    expect(window.location.href).not.toContain('raw/token with spaces');
  });

  it('uses the browser origin for an empty API base', async () => {
    const user = userEvent.setup();
    const clipboard = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboard },
    });
    render(
      <DailyPage
        apiBaseUrl=""
        apiClient={{
          createCollaborationLink: vi.fn(async () => ({
            createdAt: '2026-07-15T08:00:00.000Z',
            id: 'collab-1',
            token: 'raw/token',
          })),
          getCurrentCollaborationLink: vi.fn(async () => ({ active: false as const })),
        }}
        report={createReport('owner')}
        session={createSession('owner')}
      />,
    );

    await user.click(screen.getByRole('button', { name: '权限设置' }));
    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));

    const expectedUrl = `${window.location.origin}/c/raw%2Ftoken`;
    expect(await screen.findByDisplayValue(expectedUrl)).toBeVisible();
    expect(clipboard).toHaveBeenCalledWith(expectedUrl);
  });

  it.each([
    'https://user:password@api.example.com',
    'https://api.example.com/base',
    'https://api.example.com?region=cn',
    'https://api.example.com#internal',
    'ftp://api.example.com',
    '/api',
  ])('rejects unsafe collaboration URL base %s before creating a link', async (apiBaseUrl) => {
    const user = userEvent.setup();
    const createCollaborationLink = vi.fn();
    render(
      <DailyPage
        apiBaseUrl={apiBaseUrl}
        apiClient={{
          createCollaborationLink,
          getCurrentCollaborationLink: vi.fn(async () => ({ active: false as const })),
        }}
        report={createReport('owner')}
        session={createSession('owner')}
      />,
    );

    await user.click(screen.getByRole('button', { name: '权限设置' }));
    await user.click(await screen.findByRole('button', { name: '创建固定协作链接' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('固定协作入口地址配置无效，请联系管理员。');
    expect(createCollaborationLink).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('固定协作链接')).toBeNull();
  });

  it('does not render or call collaboration-link management for non-owner modes', () => {
    const apiClient = {
      createCollaborationLink: vi.fn(async () => ({
        createdAt: '2026-07-15T08:00:00.000Z',
        id: 'collab-1',
        token: 'secret-token',
      })),
      getCurrentCollaborationLink: vi.fn(async () => ({ active: false as const })),
      revokeCollaborationLink: vi.fn(async () => undefined),
    };

    const { rerender } = render(
      <DailyPage
        apiClient={apiClient}
        report={createReport('editor')}
        session={createSession('editor')}
      />,
    );

    expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
    expect(screen.queryByText('固定协作入口')).toBeNull();

    rerender(
      <DailyPage
        apiClient={apiClient}
        report={createReport('owner')}
        session={{ accessToken: null, email: null, mode: 'collaborator' }}
      />,
    );

    expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
    expect(screen.queryByText('固定协作入口')).toBeNull();
    expect(apiClient.getCurrentCollaborationLink).not.toHaveBeenCalled();
    expect(apiClient.createCollaborationLink).not.toHaveBeenCalled();
    expect(apiClient.revokeCollaborationLink).not.toHaveBeenCalled();
  });
});
