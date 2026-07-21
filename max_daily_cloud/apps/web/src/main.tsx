import './styles.css';

import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js';
import { LoaderCircle } from 'lucide-react';
import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type {
  ApiError,
  ReportIndexEntryDto,
  ReportDto,
  ReportItemDto,
} from '../../../packages/shared/src/contracts.js';
import { LoginPage } from './auth/LoginPage.js';
import { validateOtpCode } from './auth/otpCode.js';
import { useOtpResendCountdown } from './auth/useOtpResendCountdown.js';
import { DailyPage, type DailySession } from './daily/DailyPage.js';
import { ReportDateNav } from './daily/ReportDateNav.js';
import { MobileInboxPage } from './mobile/MobileInboxPage.js';

interface AuthState {
  session: Session | null;
  step: 'idle' | 'otp_requested' | 'ready';
}

interface RouteState {
  fixedDaily: boolean;
  inviteToken: string | null;
  reportId: string;
}

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;
const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';

function createSupabase(): SupabaseClient | null {
  if (!supabaseUrl || !supabaseAnonKey) return null;
  return createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      autoRefreshToken: true,
      persistSession: true,
    },
  });
}

function normalizeBaseUrl(value: string): string {
  if (!value) return '';
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

function parseRoute(pathname: string, search: string): RouteState {
  const match = pathname.match(/^\/r\/([^/]+)$/);
  const params = new URLSearchParams(search);
  return {
    fixedDaily: pathname === '/daily',
    inviteToken: params.get('invite'),
    reportId: match ? decodeURIComponent(match[1]) : params.get('reportId') ?? '',
  };
}

function applyRoute(reportId: string, inviteToken: string | null): void {
  const nextUrl = new URL(window.location.href);
  nextUrl.pathname = `/r/${encodeURIComponent(reportId)}`;
  nextUrl.search = '';
  if (inviteToken) nextUrl.searchParams.set('invite', inviteToken);
  window.history.replaceState({}, '', nextUrl);
}

function toApiError(error: unknown): (ApiError & { status?: number }) | null {
  if (!error || typeof error !== 'object') return null;
  const candidate = error as Partial<ApiError> & { status?: number };
  if (
    typeof candidate.code === 'string'
    && typeof candidate.message === 'string'
    && typeof candidate.requestId === 'string'
  ) {
    return {
      code: candidate.code,
      message: candidate.message,
      requestId: candidate.requestId,
      status: candidate.status,
    };
  }
  return null;
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  const error = await response.json() as ApiError;
  throw { ...error, status: response.status };
}

function fixedDailyErrorMessage(error: unknown): string {
  const apiError = toApiError(error);
  if (apiError?.status === 401 || apiError?.status === 410) {
    return '协作链接已失效，请重新打开原始固定链接';
  }
  return apiError?.message ?? '日报读取失败';
}

function sortReportsNewestFirst(entries: ReportIndexEntryDto[]): ReportIndexEntryDto[] {
  return [...entries].sort((left, right) => {
    const byDailyDate = right.dailyDate.localeCompare(left.dailyDate);
    if (byDailyDate !== 0) return byDailyDate;
    const byPublishedAt = Date.parse(right.publishedAt) - Date.parse(left.publishedAt);
    if (byPublishedAt !== 0) return byPublishedAt;
    return right.id.localeCompare(left.id);
  });
}

function App() {
  const supabase = useMemo(() => createSupabase(), []);
  const route = useMemo(
    () => parseRoute(window.location.pathname, window.location.search),
    [],
  );
  const [authState, setAuthState] = useState<AuthState>({ session: null, step: 'idle' });
  const [email, setEmail] = useState('');
  const [otpCode, setOtpCode] = useState('');
  const [reportId, setReportId] = useState(route.reportId);
  const [inviteToken, setInviteToken] = useState(route.inviteToken);
  const [loading, setLoading] = useState(true);
  const [sendingOtp, setSendingOtp] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [report, setReport] = useState<ReportDto | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportIndex, setReportIndex] = useState<ReportIndexEntryDto[]>([]);
  const [selectedReportId, setSelectedReportId] = useState<string | null>(
    route.fixedDaily ? null : route.reportId,
  );
  const [loadingReportId, setLoadingReportId] = useState<string | null>(null);
  const reportRequestSeq = useRef(0);
  const reportControllerRef = useRef<AbortController | null>(null);
  const { resendSeconds, restart: restartResendCountdown } = useOtpResendCountdown();

  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      if (!route.fixedDaily) setStatusMessage('Supabase 环境变量未配置');
      return;
    }

    let alive = true;
    void supabase.auth.getSession().then(({ data }) => {
      if (!alive) return;
      setAuthState({
        session: data.session,
        step: data.session ? 'ready' : 'idle',
      });
      setEmail(data.session?.user.email ?? '');
      setLoading(false);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!alive) return;
      setAuthState({
        session: nextSession,
        step: nextSession ? 'ready' : 'idle',
      });
      setEmail(nextSession?.user.email ?? '');
    });

    return () => {
      alive = false;
      subscription.unsubscribe();
    };
  }, [route.fixedDaily]);

  const sessionMode: DailySession['mode'] = useMemo(() => {
    if (route.fixedDaily) return 'collaborator';
    return authState.session ? 'identity' : 'public';
  }, [authState.session, route.fixedDaily]);

  const sessionForPage: DailySession = useMemo(() => {
    if (route.fixedDaily) {
      return {
        accessToken: null,
        email: null,
        mode: 'collaborator',
      };
    }
    return {
      accessToken: authState.session?.access_token ?? null,
      email: authState.session?.user.email ?? null,
      mode: sessionMode,
    };
  }, [authState.session, route.fixedDaily, sessionMode]);

  const identityAccessToken = route.fixedDaily
    ? null
    : authState.session?.access_token ?? null;

  const authHeaders = useCallback((mode: DailySession['mode']): Record<string, string> => {
    if (mode === 'identity' && identityAccessToken) {
      return { authorization: `Bearer ${identityAccessToken}` };
    }
    return {};
  }, [identityAccessToken]);

  const loadReport = useCallback(async (
    targetReportId: string,
    options: {
      mode?: DailySession['mode'];
      preserveFixedUrl?: boolean;
    } = {},
  ): Promise<void> => {
    const normalizedReportId = targetReportId.trim();
    if (!normalizedReportId) return;
    if (!options.preserveFixedUrl) applyRoute(normalizedReportId, inviteToken);

    const requestSeq = reportRequestSeq.current + 1;
    reportRequestSeq.current = requestSeq;
    reportControllerRef.current?.abort();
    const controller = new AbortController();
    reportControllerRef.current = controller;
    setLoading(true);
    setLoadingReportId(normalizedReportId);
    setReportError(null);
    try {
      const response = await fetch(
        `${normalizeBaseUrl(apiBaseUrl)}/api/reports/${encodeURIComponent(normalizedReportId)}`,
        {
          credentials: 'include',
          headers: authHeaders(options.mode ?? sessionMode),
          signal: controller.signal,
        },
      );
      const nextReport = await readJson<ReportDto>(response);
      if (controller.signal.aborted || reportRequestSeq.current !== requestSeq) return;
      setReport(nextReport);
      setReportId(normalizedReportId);
      setSelectedReportId(normalizedReportId);
    } catch (error) {
      if (controller.signal.aborted || reportRequestSeq.current !== requestSeq) return;
      const apiError = toApiError(error);
      setReportError(route.fixedDaily
        ? fixedDailyErrorMessage(error)
        : apiError?.message ?? '日报读取失败');
    } finally {
      if (!controller.signal.aborted && reportRequestSeq.current === requestSeq) {
        setLoading(false);
        setLoadingReportId(null);
      }
    }
  }, [authHeaders, inviteToken, route.fixedDaily, sessionMode]);

  const loadFixedDailyIndex = useCallback(async (signal: AbortSignal): Promise<void> => {
    setLoading(true);
    setReportError(null);
    try {
      const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/reports`, {
        credentials: 'include',
        headers: authHeaders(sessionMode),
        signal,
      });
      const entries = sortReportsNewestFirst(await readJson<ReportIndexEntryDto[]>(response));
      if (signal.aborted) return;
      setReportIndex(entries);
      if (entries.length === 0) {
        setSelectedReportId(null);
        setReport(null);
        setLoading(false);
        setLoadingReportId(null);
        return;
      }
      const nextReportId = entries[0].id;
      await loadReport(nextReportId, {
        mode: sessionMode,
        preserveFixedUrl: true,
      });
    } catch (error) {
      if (signal.aborted) return;
      setReportIndex([]);
      setReport(null);
      setReportError(fixedDailyErrorMessage(error));
      setLoading(false);
      setLoadingReportId(null);
    }
  }, [authHeaders, loadReport, sessionMode]);

  useEffect(() => {
    if (route.fixedDaily) return;
    if (!reportId.trim()) {
      setReport(null);
      return;
    }
    void loadReport(reportId, { mode: sessionMode });
  }, [loadReport, reportId, route.fixedDaily, sessionMode]);

  useEffect(() => {
    if (!route.fixedDaily) return undefined;
    const controller = new AbortController();
    void loadFixedDailyIndex(controller.signal);
    return () => {
      controller.abort();
    };
  }, [loadFixedDailyIndex, route.fixedDaily]);

  useEffect(() => {
    if (route.fixedDaily || !inviteToken || !authState.session?.access_token) return;
    let cancelled = false;

    async function claimInvite(): Promise<void> {
      try {
        const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/invitations/claim`, {
          body: JSON.stringify({ token: inviteToken }),
          credentials: 'include',
          headers: {
            'content-type': 'application/json',
            authorization: `Bearer ${authState.session?.access_token}`,
          },
          method: 'POST',
        });
        const membership = await readJson<{ reportId: string; role: 'editor' | 'viewer' }>(response);
        if (cancelled) return;
        setInviteToken(null);
        setStatusMessage(null);
        if (membership.reportId !== reportId) setReportId(membership.reportId);
        void loadReport(membership.reportId, { mode: 'identity' });
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.delete('invite');
        window.history.replaceState({}, '', nextUrl);
      } catch (error) {
        if (cancelled) return;
        const apiError = toApiError(error);
        setStatusMessage(apiError?.message ?? '邀请领取失败');
      }
    }

    void claimInvite();
    return () => {
      cancelled = true;
    };
  }, [authState.session?.access_token, inviteToken, loadReport, reportId, route.fixedDaily]);

  async function requestOtp(): Promise<void> {
    if (!supabase) return;
    setSendingOtp(true);
    setStatusMessage('正在发送验证码...');
    const { error } = await supabase.auth.signInWithOtp({
      email: email.trim(),
    });
    if (error) {
      setStatusMessage(error.message);
    } else {
      setAuthState((current) => ({ ...current, step: 'otp_requested' }));
      restartResendCountdown();
      setStatusMessage('验证码已发送，请检查邮箱');
    }
    setSendingOtp(false);
  }

  async function verifyOtp(): Promise<void> {
    if (!supabase) return;
    const validated = validateOtpCode(otpCode);
    if ('error' in validated) {
      setStatusMessage(validated.error);
      return;
    }
    setLoading(true);
    const { data, error } = await supabase.auth.verifyOtp({
      email: email.trim(),
      token: validated.code,
      type: 'email',
    });
    if (error) {
      setStatusMessage(error.message);
    } else {
      setAuthState({ session: data.session, step: 'ready' });
      setStatusMessage(null);
      if (reportId.trim()) {
        await loadReport(reportId, { mode: 'identity' });
      }
    }
    setLoading(false);
  }

  async function signOut(): Promise<void> {
    if (!supabase) return;
    await supabase.auth.signOut();
    setReport(null);
    setReportError(null);
    setStatusMessage(null);
  }

  const apiClient = useMemo(() => ({
    createCollaborationLink: async (input: {
      accessToken: string;
    }) => {
      const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/collaboration-links`, {
        credentials: 'include',
        headers: {
          authorization: `Bearer ${input.accessToken}`,
        },
        method: 'POST',
      });
      return readJson<{ createdAt: string; id: string; token: string }>(response);
    },
    createInvitation: async (input: {
      accessToken: string;
      email: string;
      reportId: string;
      role: 'editor' | 'viewer';
    }) => {
      const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/invitations`, {
        body: JSON.stringify({
          email: input.email,
          reportId: input.reportId,
          role: input.role,
        }),
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${input.accessToken}`,
        },
        method: 'POST',
      });
      return readJson<{ expiresAt: string; id: string; token: string }>(response);
    },
    createShareLink: async (input: {
      accessToken: string;
      expiresAt: string | null;
      reportId: string;
    }) => {
      const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/shares`, {
        body: JSON.stringify({
          expiresAt: input.expiresAt,
          reportId: input.reportId,
        }),
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${input.accessToken}`,
        },
        method: 'POST',
      });
      return readJson<{ expiresAt: string | null; id: string; token: string }>(response);
    },
    patchItem: async (input: {
      accessToken: string | null;
      expectedVersion: number;
      field: 'max_daily_card' | 'max_feedback' | 'review_status';
      itemId: string;
      mode: DailySession['mode'];
      value: string;
    }) => {
      const headers: Record<string, string> = {
        'content-type': 'application/json',
      };
      if (input.mode === 'identity' && input.accessToken) {
        headers.authorization = `Bearer ${input.accessToken}`;
      }
      const response = await fetch(
        `${normalizeBaseUrl(apiBaseUrl)}/api/items/${encodeURIComponent(input.itemId)}`,
        {
          body: JSON.stringify({
            expectedVersion: input.expectedVersion,
            field: input.field,
            value: input.value,
          }),
          credentials: 'include',
          headers,
          method: 'PATCH',
        },
      );
      return readJson<ReportItemDto>(response);
    },
    getCurrentCollaborationLink: async (input: { accessToken: string }) => {
      const response = await fetch(`${normalizeBaseUrl(apiBaseUrl)}/api/collaboration-links/current`, {
        credentials: 'include',
        headers: {
          authorization: `Bearer ${input.accessToken}`,
        },
      });
      return readJson<
        | { active: false }
        | {
          active: true;
          createdAt: string;
          id: string;
          lastUsedAt: string | null;
        }
      >(response);
    },
    revokeInvitation: async (input: { accessToken: string; invitationId: string }) => {
      const response = await fetch(
        `${normalizeBaseUrl(apiBaseUrl)}/api/invitations/${encodeURIComponent(input.invitationId)}`,
        {
          headers: {
            authorization: `Bearer ${input.accessToken}`,
          },
          method: 'DELETE',
        },
      );
      if (!response.ok) {
        throw await response.json() as ApiError;
      }
    },
    revokeCollaborationLink: async (input: { accessToken: string; linkId: string }) => {
      const response = await fetch(
        `${normalizeBaseUrl(apiBaseUrl)}/api/collaboration-links/${encodeURIComponent(input.linkId)}`,
        {
          credentials: 'include',
          headers: {
            authorization: `Bearer ${input.accessToken}`,
          },
          method: 'DELETE',
        },
      );
      if (!response.ok) {
        throw await response.json() as ApiError;
      }
    },
    revokeShareLink: async (input: { accessToken: string; shareId: string }) => {
      const response = await fetch(
        `${normalizeBaseUrl(apiBaseUrl)}/api/shares/${encodeURIComponent(input.shareId)}`,
        {
          headers: {
            authorization: `Bearer ${input.accessToken}`,
          },
          method: 'DELETE',
        },
      );
      if (!response.ok) {
        throw await response.json() as ApiError;
      }
    },
  }), []);

  async function refreshCurrentReport(): Promise<void> {
    const targetReportId = route.fixedDaily ? selectedReportId : reportId;
    if (!targetReportId) return;
    await loadReport(targetReportId, {
      mode: sessionMode,
      preserveFixedUrl: route.fixedDaily,
    });
  }

  function selectFixedReport(nextReportId: string): void {
    void loadReport(nextReportId, {
      mode: sessionMode,
      preserveFixedUrl: true,
    });
  }

  if (loading && !report) {
    return (
      <main className="auth-shell">
        <div className="loading-panel">
          <LoaderCircle className="spin" size={20} aria-hidden="true" />
          加载中…
        </div>
      </main>
    );
  }

  if (route.fixedDaily && !report) {
    return (
      <main className="daily-shell">
        <header className="daily-topbar">
          <div className="daily-brand">
            <p className="eyebrow">MAX DAILY INTEL</p>
            <h1>外部情报口喷日报</h1>
            <p className="daily-date">固定协作入口</p>
          </div>
          <div className="daily-account">
            <strong className="material-count">{reportIndex.length} 期日报</strong>
          </div>
        </header>
        <ReportDateNav
          entries={reportIndex}
          errorMessage={reportError}
          loadingReportId={loadingReportId}
          onSelect={selectFixedReport}
          selectedReportId={selectedReportId}
        />
      </main>
    );
  }

  if (!route.fixedDaily && (!reportId.trim() || !report || (!authState.session && reportError))) {
    return (
      <LoginPage
        email={email}
        loading={loading}
        onEmailChange={setEmail}
        onOtpCodeChange={setOtpCode}
        onReportIdChange={setReportId}
        onResend={() => {
          void requestOtp();
        }}
        onSubmit={() => {
          if (authState.step === 'otp_requested') {
            void verifyOtp();
            return;
          }
          void requestOtp();
        }}
        otpCode={otpCode}
        reportId={reportId}
        resendSeconds={resendSeconds}
        sendingOtp={sendingOtp}
        statusMessage={authState.session ? reportError ?? statusMessage : statusMessage}
        statusTone={statusMessage === '验证码已发送，请检查邮箱'
          ? 'success'
          : 'default'}
        submitLabel={authState.step === 'otp_requested' ? '验证并进入' : '发送验证码'}
      />
    );
  }

  if (!report) return null;

  return (
    <DailyPage
      apiBaseUrl={normalizeBaseUrl(apiBaseUrl)}
      apiClient={apiClient}
      dateNavigation={route.fixedDaily ? (
        <ReportDateNav
          entries={reportIndex}
          errorMessage={reportError}
          loadingReportId={loadingReportId}
          onSelect={selectFixedReport}
          selectedReportId={selectedReportId}
        />
      ) : null}
      onRefreshReport={refreshCurrentReport}
      onSignOut={!route.fixedDaily && authState.session ? () => {
        void signOut();
      } : undefined}
      report={report}
      session={sessionForPage}
    />
  );
}

const rootElement = document.getElementById('root');
if (!rootElement) throw new Error('root element is missing');
createRoot(rootElement).render(
  <StrictMode>
    {window.location.pathname === '/collect'
      ? <MobileInboxPage apiBaseUrl={normalizeBaseUrl(apiBaseUrl)} />
      : <App />}
  </StrictMode>,
);
