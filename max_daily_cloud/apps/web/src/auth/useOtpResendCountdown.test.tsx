// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useOtpResendCountdown } from './useOtpResendCountdown';

afterEach(() => {
  vi.useRealTimers();
});

describe('useOtpResendCountdown', () => {
  it('counts down from 59 seconds and stops at zero', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useOtpResendCountdown());

    act(() => result.current.restart());
    expect(result.current.resendSeconds).toBe(59);

    act(() => vi.advanceTimersByTime(1_000));
    expect(result.current.resendSeconds).toBe(58);

    act(() => vi.advanceTimersByTime(58_000));
    expect(result.current.resendSeconds).toBe(0);
  });
});
