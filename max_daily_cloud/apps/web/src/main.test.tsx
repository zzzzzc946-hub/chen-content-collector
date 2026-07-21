// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReportDto, ReportIndexEntryDto, Role } from '../../../packages/shared/src/contracts';

const supabaseAuth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
  signInWithOtp: vi.fn(),
  signOut: vi.fn(),
  verifyOtp: vi.fn(),
}));
const createClient = vi.hoisted(() => vi.fn(() => ({ auth: supabaseAuth })));

vi.mock('@supabase/supabase-js', () => ({
  createClient,
}));

let emitAuthChange: ((event: string, session: ReturnType<typeof createSupabaseSession> | null) => void) | null = null;

const reportIndex: ReportIndexEntryDto[] = [
  {
    dailyDate: '2026-07-14',
    id: 'report-old',
    itemCount: 1,
    publishedAt: '2026-07-14T12:00:00.000Z',
  },
  {
    dailyDate: '2026-07-15',
    id: 'report-new',
    itemCount: 2,
    publishedAt: '2026-07-15T12:00:00.000Z',
  },
];

function createReport(
  id: string,
  dailyDate: string,
  title: string,
  accessRole: Role = 'collaborator',
): ReportDto {
  return {
    accessRole,
    dailyDate,
    id,
    mediaMode: id === 'report-new' ? 'current_day' : 'historical',
    items: [
      {
        caption: `${title} 文稿`,
        id: `${id}-item`,
        localRecordId: `${id}-local`,
        maxDailyCard: `${title} 卡片`,
        maxFeedback: '',
        mediaId: `${id}-media`,
        reviewStatus: 'pending',
        sourceUrl: `https://example.com/${id}`,
        title,
        version: 1,
      },
    ],
    publishedVersion: 1,
    status: 'published',
  };
}

function createSupabaseSession(role: 'owner' | 'editor' | 'viewer') {
  return {
    access_token: `${role}-bearer-token`,
    user: {
      email: `${role}@example.com`,
    },
  };
}

function installSupabaseSession(session: ReturnType<typeof createSupabaseSession> | null): void {
  vi.stubEnv('VITE_SUPABASE_URL', 'https://project.supabase.co');
  vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'public-anon-key');
  supabaseAuth.getSession.mockResolvedValue({ data: { session } });
  supabaseAuth.onAuthStateChange.mockImplementation((callback) => {
    emitAuthChange = callback;
    return { data: { subscription: { unsubscribe: vi.fn() } } };
  });
}

function jsonResponse(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json' },
    status,
  }));
}

function deferredJson(body: unknown) {
  let resolve!: () => void;
  const promise = new Promise<Response>((done) => {
    resolve = () => done(new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
      status: 200,
    }));
  });
  return { promise, resolve };
}

async function expectTextVisible(text: string): Promise<void> {
  const matches = await screen.findAllByText(text);
  expect(matches.some((element) => element.offsetParent !== null || element.isConnected)).toBe(true);
}

async function renderMain(pathname = '/daily'): Promise<void> {
  document.body.innerHTML = '<div id="root"></div>';
  window.history.pushState({}, '', pathname);
  await import('./main');
}

describe('fixed /daily collaborator portal', () => {
  afterEach(() => {
    cleanup();
    document.body.innerHTML = '';
    vi.resetModules();
    vi.clearAllMocks();
    emitAuthChange = null;
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it.each(['owner', 'editor', 'viewer'] as const)(
    'keeps a pre-existing %s session out of fixed collaborator mode',
    async (role) => {
      const identitySession = createSupabaseSession(role);
      installSupabaseSession(identitySession);
      const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
        if (url.endsWith('/api/reports/report-new')) {
          return jsonResponse(createReport('report-new', '2026-07-15', '固定协作日报', role));
        }
        if (url.endsWith('/api/media/session')) return jsonResponse({ ok: true });
        throw new Error(`unexpected fetch ${url}`);
      });
      vi.stubGlobal('fetch', fetcher);

      await renderMain('/daily');

      await expectTextVisible('固定协作日报');
      expect(screen.queryByText(identitySession.user.email)).toBeNull();
      expect(screen.queryByRole('button', { name: '退出登录' })).toBeNull();
      expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
      expect(fetcher.mock.calls
        .filter(([input]) => String(input).includes('/api/'))
        .every(([, init]) => !Object.prototype.hasOwnProperty.call(init?.headers ?? {}, 'authorization')))
        .toBe(true);
    },
  );

  it('ignores auth changes after the fixed collaborator portal has bootstrapped', async () => {
    installSupabaseSession(null);
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
      if (url.endsWith('/api/reports/report-new')) {
        return jsonResponse(createReport('report-new', '2026-07-15', '固定协作日报', 'owner'));
      }
      if (url.endsWith('/api/media/session')) return jsonResponse({ ok: true });
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');
    await expectTextVisible('固定协作日报');

    const ownerSession = createSupabaseSession('owner');
    await act(async () => {
      emitAuthChange?.('SIGNED_IN', ownerSession);
    });

    await waitFor(() => {
      expect(screen.queryByText(ownerSession.user.email)).toBeNull();
      expect(screen.queryByRole('button', { name: '退出登录' })).toBeNull();
      expect(screen.queryByRole('button', { name: '权限设置' })).toBeNull();
      expect(fetcher.mock.calls
        .filter(([input]) => String(input).includes('/api/'))
        .every(([, init]) => !Object.prototype.hasOwnProperty.call(init?.headers ?? {}, 'authorization')))
        .toBe(true);
    });
  });

  it('selects the newest published report by default with cookie credentials', async () => {
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
      if (url.endsWith('/api/reports/report-new')) {
        expect(init?.credentials).toBe('include');
        expect(init?.headers).not.toHaveProperty('authorization');
        return jsonResponse(createReport('report-new', '2026-07-15', '最新日报素材'));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');

    await expectTextVisible('最新日报素材');
    expect(screen.getAllByText('2026-07-15').length).toBeGreaterThan(0);
    expect(window.location.pathname).toBe('/daily');
    expect(fetcher).toHaveBeenCalledWith(
      '/api/reports',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('selects the newest daily date by default even when an older date was republished later', async () => {
    const republishedIndex: ReportIndexEntryDto[] = [
      {
        dailyDate: '2026-07-14',
        id: 'report-old-republished',
        itemCount: 1,
        publishedAt: '2026-07-16T23:00:00.000Z',
      },
      {
        dailyDate: '2026-07-15',
        id: 'report-newer-date',
        itemCount: 2,
        publishedAt: '2026-07-15T08:00:00.000Z',
      },
    ];
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(republishedIndex);
      if (url.endsWith('/api/reports/report-newer-date')) {
        expect(init?.credentials).toBe('include');
        return jsonResponse(createReport('report-newer-date', '2026-07-15', '日期最新日报素材'));
      }
      if (url.endsWith('/api/reports/report-old-republished')) {
        return jsonResponse(createReport('report-old-republished', '2026-07-14', '旧日期重发素材'));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');

    await expectTextVisible('日期最新日报素材');
    expect(screen.queryByText('旧日期重发素材')).toBeNull();
    expect(screen.getByRole('button', { name: /2026-07-15/ })).toHaveAttribute('aria-current', 'date');
  });

  it('changes reports without changing the fixed /daily URL', async () => {
    const fetcher = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
      if (url.endsWith('/api/reports/report-new')) {
        return jsonResponse(createReport('report-new', '2026-07-15', '最新日报素材'));
      }
      if (url.endsWith('/api/reports/report-old')) {
        return jsonResponse(createReport('report-old', '2026-07-14', '历史日报素材'));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');
    await expectTextVisible('最新日报素材');

    await userEvent.click(screen.getByRole('button', { name: /2026-07-14/ }));

    await expectTextVisible('历史日报素材');
    expect(window.location.pathname).toBe('/daily');
    expect(window.location.search).toBe('');
  });

  it('keeps the committed report and date selected when a date switch fails', async () => {
    const fetcher = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
      if (url.endsWith('/api/reports/report-new')) {
        return jsonResponse(createReport('report-new', '2026-07-15', '最新日报素材'));
      }
      if (url.endsWith('/api/reports/report-old')) {
        return jsonResponse({
          code: 'report_unavailable',
          message: '历史日报读取失败',
          requestId: 'req-old',
        }, 500);
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');
    await expectTextVisible('最新日报素材');

    await userEvent.click(screen.getByRole('button', { name: /2026-07-14/ }));

    expect(await screen.findByText('历史日报读取失败')).toBeVisible();
    expect(screen.getAllByText('最新日报素材').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /2026-07-15/ })).toHaveAttribute('aria-current', 'date');
    expect(screen.getByRole('button', { name: /2026-07-14/ })).not.toHaveAttribute('aria-current');
  });

  it('does not let a stale report fetch overwrite the current selection', async () => {
    const slowOld = deferredJson(createReport('report-old', '2026-07-14', '历史日报素材'));
    const fastNew = deferredJson(createReport('report-new', '2026-07-15', '最新日报素材 二次'));
    let newReportReads = 0;
    const fetcher = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) return jsonResponse(reportIndex);
      if (url.endsWith('/api/reports/report-new')) {
        newReportReads += 1;
        if (newReportReads === 1) {
          return jsonResponse(createReport('report-new', '2026-07-15', '最新日报素材'));
        }
        return fastNew.promise;
      }
      if (url.endsWith('/api/reports/report-old')) return slowOld.promise;
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');
    await expectTextVisible('最新日报素材');

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /2026-07-14/ }));
    await user.click(screen.getByRole('button', { name: /2026-07-15/ }));

    fastNew.resolve();
    await expectTextVisible('最新日报素材 二次');

    slowOld.resolve();
    await waitFor(() => {
      expect(screen.getAllByText('最新日报素材 二次').length).toBeGreaterThan(0);
      expect(screen.queryByText('历史日报素材')).toBeNull();
    });
  });

  it('shows empty and invalid collaboration states without rendering OTP controls', async () => {
    const fetcher = vi.fn(() => jsonResponse([], 200));
    vi.stubGlobal('fetch', fetcher);

    await renderMain('/daily');

    expect(await screen.findByText('还没有已发布日报')).toBeVisible();
    expect(screen.queryByRole('button', { name: '发送验证码' })).toBeNull();

    cleanup();
    document.body.innerHTML = '';
    vi.resetModules();
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/reports')) {
        return jsonResponse({
          code: 'collaboration_link_unavailable',
          message: 'collaboration link is unavailable',
          requestId: 'req-fixed',
        }, 410);
      }
      throw new Error(`unexpected fetch ${url}`);
    }));

    await renderMain('/daily');

    expect(await screen.findByText('协作链接已失效，请重新打开原始固定链接')).toBeVisible();
    expect(screen.queryByRole('button', { name: '发送验证码' })).toBeNull();
  });
});

describe('/collect mobile inbox route', () => {
  afterEach(() => {
    cleanup();
    document.body.innerHTML = '';
    vi.resetModules();
    vi.clearAllMocks();
    emitAuthChange = null;
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('routes /collect without creating a Supabase client', async () => {
    vi.stubEnv('VITE_SUPABASE_URL', 'https://project.supabase.co');
    vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'public-anon-key');
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse({
      items: [],
      pendingCount: 0,
    })));

    await renderMain('/collect');

    expect(await screen.findByRole('heading', { name: 'CHEN 链接收集箱' })).toBeVisible();
    expect(createClient).not.toHaveBeenCalled();
  });
});

describe('owner fixed collaboration link management API client', () => {
  afterEach(() => {
    cleanup();
    document.body.innerHTML = '';
    vi.resetModules();
    vi.clearAllMocks();
    emitAuthChange = null;
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('uses bearer credentials to inspect, create, and revoke the fixed collaboration link', async () => {
    installSupabaseSession(createSupabaseSession('owner'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(async () => undefined) },
    });
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/reports/report-1')) {
        return jsonResponse(createReport('report-1', '2026-07-15', 'Owner 日报素材', 'owner'));
      }
      if (url.endsWith('/api/collaboration-links/current')) {
        return jsonResponse({ active: false });
      }
      if (url.endsWith('/api/collaboration-links') && init?.method === 'POST') {
        return jsonResponse({
          createdAt: '2026-07-15T08:00:00.000Z',
          id: 'collab-1',
          token: 'raw-token',
        }, 201);
      }
      if (url.endsWith('/api/collaboration-links/collab-1') && init?.method === 'DELETE') {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await renderMain('/r/report-1');
    await expectTextVisible('Owner 日报素材');

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '权限设置' }));
    await screen.findByText('固定协作入口');
    await user.click(screen.getByRole('button', { name: '创建固定协作链接' }));
    expect(await screen.findByDisplayValue(`${window.location.origin}/c/raw-token`)).toBeVisible();
    await user.click(screen.getByRole('button', { name: '撤销固定协作链接' }));

    expect(fetcher).toHaveBeenCalledWith(
      '/api/collaboration-links/current',
      expect.objectContaining({
        credentials: 'include',
        headers: expect.objectContaining({ authorization: 'Bearer owner-bearer-token' }),
      }),
    );
    expect(fetcher).toHaveBeenCalledWith(
      '/api/collaboration-links',
      expect.objectContaining({
        credentials: 'include',
        headers: expect.objectContaining({ authorization: 'Bearer owner-bearer-token' }),
        method: 'POST',
      }),
    );
    expect(fetcher).toHaveBeenCalledWith(
      '/api/collaboration-links/collab-1',
      expect.objectContaining({
        credentials: 'include',
        headers: expect.objectContaining({ authorization: 'Bearer owner-bearer-token' }),
        method: 'DELETE',
      }),
    );
  });
});
