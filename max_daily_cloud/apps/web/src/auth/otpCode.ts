export function validateOtpCode(value: string):
  | { code: string }
  | { error: string } {
  const code = value.trim();
  if (!/^\d{8}$/.test(code)) {
    return { error: '请输入邮件中的 8 位验证码' };
  }
  return { code };
}
