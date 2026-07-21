import { KeyRound, LoaderCircle, LogIn, Mail, RefreshCw, ShieldCheck } from 'lucide-react';

interface LoginPageProps {
  email: string;
  loading: boolean;
  otpCode: string;
  reportId: string;
  resendSeconds: number | null;
  sendingOtp: boolean;
  statusMessage: string | null;
  statusTone?: 'default' | 'success';
  submitLabel: '发送验证码' | '验证并进入';
  onEmailChange(value: string): void;
  onOtpCodeChange(value: string): void;
  onReportIdChange(value: string): void;
  onResend(): void;
  onSubmit(): void;
}

export function LoginPage({
  email,
  loading,
  otpCode,
  reportId,
  resendSeconds,
  sendingOtp,
  statusMessage,
  statusTone = 'default',
  submitLabel,
  onEmailChange,
  onOtpCodeChange,
  onReportIdChange,
  onResend,
  onSubmit,
}: LoginPageProps) {
  const resendLabel = resendSeconds === 0
    ? '重新发送验证码'
    : `${resendSeconds ?? 0} 秒后可重新发送`;

  return (
    <main className="auth-shell">
      <section className="auth-panel">
        <header className="auth-header">
          <p className="eyebrow">MAX DAILY INTEL</p>
          <h1>外部情报口喷日报</h1>
        </header>

        <div className="auth-grid">
          <label className="field">
            <span className="field-label">
              <Mail size={16} aria-hidden="true" />
              邮箱
            </span>
            <input
              autoComplete="email"
              className="field-input"
              onChange={(event) => onEmailChange(event.target.value)}
              placeholder="name@example.com"
              type="email"
              value={email}
            />
          </label>

          <label className="field">
            <span className="field-label">
              <KeyRound size={16} aria-hidden="true" />
              验证码
            </span>
            <input
              autoComplete="one-time-code"
              className="field-input"
              inputMode="numeric"
              maxLength={8}
              onChange={(event) => onOtpCodeChange(event.target.value)}
              placeholder="8 位验证码"
              value={otpCode}
            />
          </label>

          <label className="field">
            <span className="field-label">
              <ShieldCheck size={16} aria-hidden="true" />
              日报 ID
            </span>
            <input
              className="field-input"
              onChange={(event) => onReportIdChange(event.target.value)}
              placeholder="report-id"
              value={reportId}
            />
          </label>
        </div>

        {statusMessage ? (
          <p className={`status-banner status-banner-${statusTone}`}>{statusMessage}</p>
        ) : null}

        <div className="auth-actions">
          <button
            className="primary-button"
            disabled={loading || sendingOtp}
            onClick={onSubmit}
            type="button"
          >
            {sendingOtp ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <LogIn size={16} aria-hidden="true" />
            )}
            {sendingOtp ? '发送中...' : submitLabel}
          </button>
          {submitLabel === '验证并进入' && resendSeconds !== null ? (
            <button
              className="secondary-button"
              disabled={sendingOtp || resendSeconds > 0}
              onClick={onResend}
              type="button"
            >
              <RefreshCw size={16} aria-hidden="true" />
              {sendingOtp ? '重新发送中...' : resendLabel}
            </button>
          ) : null}
        </div>
      </section>
    </main>
  );
}
