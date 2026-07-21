// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LoginPage } from './LoginPage';

afterEach(cleanup);

function renderLoginPage(
  input: Partial<React.ComponentProps<typeof LoginPage>> = {},
) {
  const props: React.ComponentProps<typeof LoginPage> = {
    email: '',
    loading: false,
    onEmailChange: vi.fn(),
    onOtpCodeChange: vi.fn(),
    onReportIdChange: vi.fn(),
    onResend: vi.fn(),
    onSubmit: vi.fn(),
    otpCode: '',
    reportId: '',
    resendSeconds: null,
    sendingOtp: false,
    statusMessage: null,
    submitLabel: '发送验证码',
    ...input,
  };
  render(<LoginPage {...props} />);
  return props;
}

describe('LoginPage', () => {
  it('prompts for the configured eight-digit OTP', () => {
    renderLoginPage();

    expect(screen.getByPlaceholderText('8 位验证码')).toHaveAttribute('maxlength', '8');
  });

  it('shows immediate pending feedback while requesting an OTP', () => {
    renderLoginPage({ loading: true, sendingOtp: true });

    expect(screen.getByRole('button', { name: '发送中...' })).toBeDisabled();
  });

  it('shows a disabled resend countdown after an OTP is sent', () => {
    renderLoginPage({ resendSeconds: 59, submitLabel: '验证并进入' });

    expect(
      screen.getByRole('button', { name: '59 秒后可重新发送' }),
    ).toBeDisabled();
  });

  it('enables resending when the countdown reaches zero', async () => {
    const user = userEvent.setup();
    const props = renderLoginPage({ resendSeconds: 0, submitLabel: '验证并进入' });
    const resend = screen.getByRole('button', { name: '重新发送验证码' });

    expect(resend).toBeEnabled();
    await user.click(resend);
    expect(props.onResend).toHaveBeenCalledOnce();
  });
});
