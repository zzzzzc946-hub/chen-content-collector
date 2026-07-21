const REQUIRED_PRODUCTION_WEB_ENV = [
  'VITE_API_BASE_URL',
  'VITE_SUPABASE_ANON_KEY',
  'VITE_SUPABASE_URL',
] as const;

export function requireProductionWebEnv(
  env: Record<string, string | undefined>,
): void {
  const missing = REQUIRED_PRODUCTION_WEB_ENV.filter((name) => !env[name]?.trim());
  if (missing.length > 0) {
    throw new Error(`Missing production web environment variables: ${missing.join(', ')}`);
  }
}
