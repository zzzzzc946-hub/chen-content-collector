import { describe, expect, it } from 'vitest';

import { validateOtpCode } from './otpCode';

describe('validateOtpCode', () => {
  it('accepts the configured eight-digit OTP', () => {
    expect(validateOtpCode(' 12345678 ')).toEqual({ code: '12345678' });
  });

  it('rejects incomplete and non-numeric OTP values with a clear message', () => {
    expect(validateOtpCode('123456')).toEqual({
      error: '请输入邮件中的 8 位验证码',
    });
    expect(validateOtpCode('1234abcd')).toEqual({
      error: '请输入邮件中的 8 位验证码',
    });
  });
});
