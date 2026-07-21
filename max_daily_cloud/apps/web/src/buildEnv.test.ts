import { describe, expect, it } from 'vitest';

import { requireProductionWebEnv } from './buildEnv';

const completeEnv = {
  VITE_API_BASE_URL: 'https://worker.example.com',
  VITE_SUPABASE_ANON_KEY: 'public-anon-key',
  VITE_SUPABASE_URL: 'https://project.supabase.co',
};

describe('requireProductionWebEnv', () => {
  it('accepts a complete production web configuration', () => {
    expect(() => requireProductionWebEnv(completeEnv)).not.toThrow();
  });

  it('lists every missing production variable', () => {
    expect(() => requireProductionWebEnv({ VITE_SUPABASE_URL: '' })).toThrow(
      'Missing production web environment variables: VITE_API_BASE_URL, VITE_SUPABASE_ANON_KEY, VITE_SUPABASE_URL',
    );
  });
});
