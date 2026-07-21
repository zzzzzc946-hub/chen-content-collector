import { useEffect, useState } from 'react';

const RESEND_DELAY_SECONDS = 59;

export function useOtpResendCountdown(): {
  resendSeconds: number | null;
  restart(): void;
} {
  const [resendSeconds, setResendSeconds] = useState<number | null>(null);

  useEffect(() => {
    if (resendSeconds === null || resendSeconds === 0) return;
    const interval = window.setInterval(() => {
      setResendSeconds((current) => current === null ? null : Math.max(0, current - 1));
    }, 1_000);
    return () => window.clearInterval(interval);
  }, [resendSeconds]);

  return {
    resendSeconds,
    restart: () => setResendSeconds(RESEND_DELAY_SECONDS),
  };
}
