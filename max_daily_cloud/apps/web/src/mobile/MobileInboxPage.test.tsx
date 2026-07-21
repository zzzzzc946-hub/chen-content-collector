// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  MobileInboxItemDto,
  MobileInboxSnapshotDto,
  MobileInboxSubmitResult,
} from '../../../../packages/shared/src/contracts';
import { MobileInboxPage } from './MobileInboxPage';

const initialSnapshot: MobileInboxSnapshotDto = {
  items: [
    {
      id: 'item-1',
      note: '开头可参考',
      platform: 'douyin',
      status: '待扒取',
      submittedAt: '2026-07-17T08:30:00.000Z',
      url: 'https://v.douyin.com/abc',
    },
  ],
  pendingCount: 12,
};

const submittedSnapshot: MobileInboxSubmitResult = {
  addedCount: 1,
  existingCount: 0,
  ignoredCount: 0,
  items: initialSnapshot.items,
  pendingCount: 12,
};

function jsonResponse(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json' },
    status,
  }));
}

function deferredJson(body: unknown, status = 200) {
  let resolve!: () => void;
  const promise = new Promise<Response>((done) => {
    resolve = () => done(new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
      status,
    }));
  });
  return { promise, resolve };
}

function createFetcher(options: {
  get?: MobileInboxSnapshotDto;
  post?: MobileInboxSubmitResult | { message: string };
  postStatus?: number;
} = {}) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url !== '/api/mobile-inbox') {
      throw new Error(`unexpected fetch ${url}`);
    }
    if (init?.method === 'POST') {
      return jsonResponse(options.post ?? submittedSnapshot, options.postStatus ?? 200);
    }
    return jsonResponse(options.get ?? initialSnapshot);
  });
}

describe('MobileInboxPage', () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('submits share text and shows added existing and pending counts', async () => {
    const fetcher = createFetcher();
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();

    render(<MobileInboxPage apiBaseUrl="" />);

    await user.type(screen.getByLabelText('作品链接'), '复制文案 https://v.douyin.com/abc');
    await user.type(screen.getByLabelText('备注（可选）'), '开头可参考');
    await user.click(screen.getByRole('button', { name: '提交到待扒取表' }));

    expect(await screen.findByText('新增 1 条，已存在 0 条')).toBeVisible();
    expect(screen.getByText('待扒取 12')).toBeVisible();
    expect(fetcher).toHaveBeenCalledWith('/api/mobile-inbox', expect.objectContaining({
      body: JSON.stringify({
        note: '开头可参考',
        text: '复制文案 https://v.douyin.com/abc',
      }),
      headers: { 'content-type': 'application/json' },
      method: 'POST',
    }));
  });

  it('keeps input and exposes retry after a failed submission', async () => {
    const fetcher = createFetcher({
      post: { message: 'upstream unavailable' },
      postStatus: 502,
    });
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();

    render(<MobileInboxPage apiBaseUrl="" />);

    await user.type(screen.getByLabelText('作品链接'), 'https://xhslink.com/abc');
    await user.click(screen.getByRole('button', { name: '提交到待扒取表' }));

    expect(await screen.findByRole('button', { name: '重新提交' })).toBeVisible();
    expect(screen.getByLabelText('作品链接')).toHaveValue('https://xhslink.com/abc');
  });

  it('does not render any scrape or daily action', () => {
    vi.stubGlobal('fetch', createFetcher());

    render(<MobileInboxPage apiBaseUrl="" />);

    expect(screen.queryByRole('button', { name: /^(开始扒取|生成日报|转写|下载)$/ })).toBeNull();
  });

  it('refreshes the read-only snapshot every ten seconds', async () => {
    vi.useFakeTimers();
    const fetcher = createFetcher();
    vi.stubGlobal('fetch', fetcher);

    render(<MobileInboxPage apiBaseUrl="" />);

    expect(fetcher).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(fetcher).toHaveBeenCalledWith('/api/mobile-inbox', expect.anything());
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('ignores older snapshot responses that finish after a successful submission refresh', async () => {
    const staleSnapshot: MobileInboxSnapshotDto = {
      items: [
        {
          id: 'item-stale',
          note: '旧返回',
          platform: 'douyin',
          status: '待扒取',
          submittedAt: '2026-07-17T08:29:00.000Z',
          url: 'https://v.douyin.com/stale',
        },
      ],
      pendingCount: 1,
    };
    const freshSnapshot: MobileInboxSnapshotDto = {
      items: [
        {
          id: 'item-fresh',
          note: '新返回',
          platform: 'xhs',
          status: '待扒取',
          submittedAt: '2026-07-17T08:31:00.000Z',
          url: 'https://xhslink.com/fresh',
        },
      ],
      pendingCount: 12,
    };
    const initialGet = deferredJson(staleSnapshot);
    const postRefreshGet = deferredJson(freshSnapshot);
    let getCount = 0;
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url !== '/api/mobile-inbox') {
        throw new Error(`unexpected fetch ${url}`);
      }
      if (init?.method === 'POST') {
        return jsonResponse({
          addedCount: 1,
          existingCount: 0,
          ignoredCount: 0,
          items: freshSnapshot.items,
          pendingCount: freshSnapshot.pendingCount,
        });
      }
      getCount += 1;
      return getCount === 1 ? initialGet.promise : postRefreshGet.promise;
    });
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();

    render(<MobileInboxPage apiBaseUrl="" />);

    await user.type(screen.getByLabelText('作品链接'), 'https://xhslink.com/fresh');
    await user.click(screen.getByRole('button', { name: '提交到待扒取表' }));

    expect(await screen.findByText('新增 1 条，已存在 0 条')).toBeVisible();
    expect(fetcher).toHaveBeenCalledTimes(3);

    await act(async () => {
      postRefreshGet.resolve();
      await postRefreshGet.promise;
    });
    expect(await screen.findByText('https://xhslink.com/fresh')).toBeVisible();
    expect(screen.getByText('待扒取 12')).toBeVisible();

    await act(async () => {
      initialGet.resolve();
      await initialGet.promise;
    });

    expect(screen.getByText('https://xhslink.com/fresh')).toBeVisible();
    expect(screen.queryByText('https://v.douyin.com/stale')).toBeNull();
    expect(screen.getByText('待扒取 12')).toBeVisible();
  });

  it('shows recent records as read-only platform url time and status', async () => {
    const item: MobileInboxItemDto = {
      id: 'item-xhs',
      note: '仅备注，不展示操作',
      platform: 'xhs',
      status: '待扒取',
      submittedAt: '2026-07-17T09:15:00.000Z',
      url: 'https://xhslink.com/abc',
    };
    vi.stubGlobal('fetch', createFetcher({
      get: { items: [item], pendingCount: 1 },
    }));

    render(<MobileInboxPage apiBaseUrl="" />);

    expect(await screen.findByText('xhs')).toBeVisible();
    expect(screen.getByText('https://xhslink.com/abc')).toBeVisible();
    expect(screen.getByText('待扒取')).toBeVisible();
    expect(screen.getByText(/2026/)).toBeVisible();
    expect(screen.queryByText('仅备注，不展示操作')).toBeNull();
  });

  it('shows 未知 when the platform is empty', async () => {
    vi.stubGlobal('fetch', createFetcher({
      get: {
        items: [{
          id: 'item-unknown',
          note: '',
          platform: '',
          status: '待扒取',
          submittedAt: '2026-07-17T09:20:00.000Z',
          url: 'https://example.com/unknown',
        }],
        pendingCount: 1,
      },
    }));

    render(<MobileInboxPage apiBaseUrl="" />);

    expect(await screen.findByText('未知')).toBeVisible();
  });

  it('sets the document title while mounted and restores it on unmount', () => {
    vi.stubGlobal('fetch', createFetcher());
    document.title = 'MAX DAILY INTEL';

    const view = render(<MobileInboxPage apiBaseUrl="" />);

    expect(document.title).toBe('CHEN 链接收集箱');

    view.unmount();

    expect(document.title).toBe('MAX DAILY INTEL');
  });
});
