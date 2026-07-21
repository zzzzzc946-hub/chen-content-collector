import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'VITE_SUPABASE_URL=https://e2e.supabase.co VITE_SUPABASE_ANON_KEY=e2e-anon-key npm --workspace @max-daily-cloud/web run dev -- --host 127.0.0.1 --port 4173',
    reuseExistingServer: false,
    url: 'http://127.0.0.1:4173/daily',
  },
});
