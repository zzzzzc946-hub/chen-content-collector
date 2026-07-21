import { HTTPException } from 'hono/http-exception';
import type { Context } from 'hono';
import type { WorkerAppEnv } from './env.js';
import { normalizeEmail } from './invitations.js';

export type IdentityRole = 'owner' | 'editor' | 'viewer';

export interface VerifiedIdentity {
  id: string;
  email: string;
}

export interface Identity extends VerifiedIdentity {
  role: IdentityRole | null;
}

export interface IdentityVerifier {
  verify(request: Request): Promise<VerifiedIdentity | null>;
}

export interface IdentityRoleStore {
  claimOwner(input: {
    email: string;
    userId: string;
  }): Promise<'owner'>;
  getIdentityRole(userId: string): Promise<IdentityRole | null>;
}

interface SupabaseAuthUser {
  email_confirmed_at?: unknown;
  id?: unknown;
  email?: unknown;
}

export function createSupabaseIdentityVerifier(input: {
  anonKey: string;
  fetch?: typeof fetch;
  supabaseUrl: string;
}): IdentityVerifier {
  const fetcher = input.fetch ?? fetch;
  const baseUrl = input.supabaseUrl.replace(/\/+$/, '');

  return {
    async verify(request: Request): Promise<VerifiedIdentity | null> {
      const authorization = request.headers.get('authorization');
      if (!authorization?.startsWith('Bearer ') || authorization.length <= 7) {
        return null;
      }

      const response = await fetcher(`${baseUrl}/auth/v1/user`, {
        headers: {
          apikey: input.anonKey,
          authorization,
        },
      });
      if (!response.ok) return null;

      const user = await response.json() as SupabaseAuthUser;
      if (
        typeof user.id !== 'string'
        || typeof user.email !== 'string'
        || typeof user.email_confirmed_at !== 'string'
        || Number.isNaN(Date.parse(user.email_confirmed_at))
      ) {
        return null;
      }

      const email = normalizeEmail(user.email);
      return email ? { id: user.id, email } : null;
    },
  };
}

export async function requireIdentity(c: Context<WorkerAppEnv>): Promise<Identity> {
  const cached = c.get('identity');
  if (cached) return cached;

  const services = c.get('services');
  const verified = await services.identityVerifier.verify(c.req.raw);
  if (!verified) {
    throw new HTTPException(401, { message: 'authentication required' });
  }

  const email = normalizeEmail(verified.email);
  let role = await services.storage.getIdentityRole(verified.id);
  if (
    role !== 'owner'
    && email
    && email === normalizeEmail(services.ownerEmail)
  ) {
    role = await services.storage.claimOwner({
      email,
      userId: verified.id,
    });
  }
  const identity = {
    id: verified.id,
    email,
    role,
  };
  c.set('identity', identity);
  return identity;
}
