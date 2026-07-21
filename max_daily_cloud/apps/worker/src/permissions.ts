import type { Context } from 'hono';
import { HTTPException } from 'hono/http-exception';
import type { Role } from '../../../packages/shared/src/contracts.js';
import { requireIdentity, type Identity } from './auth.js';
import type { WorkerAppEnv } from './env.js';

export type Permission = 'read_report' | 'edit_report' | 'manage_access';

export function canPerform(role: Role | null, permission: Permission): boolean {
  if (!role) return false;
  if (role === 'owner') return true;
  if (permission === 'read_report') return true;
  if (permission === 'edit_report') return role === 'editor';
  return false;
}

export async function requireOwner(c: Context<WorkerAppEnv>): Promise<Identity> {
  const identity = await requireIdentity(c);
  if (!canPerform(identity.role, 'manage_access')) {
    throw new HTTPException(403, { message: 'owner access required' });
  }
  return identity;
}
