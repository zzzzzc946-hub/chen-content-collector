import { MediaError } from './media.js';

const MEDIA_SESSION_COOKIE = 'max_daily_media';
const MEDIA_SESSION_LIFETIME_SECONDS = 5 * 60;

interface MediaSessionPayload {
  actorId: string;
  exp: number;
  v: 1;
}

function base64UrlEncode(value: Uint8Array): string {
  let binary = '';
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '');
}

function base64UrlDecode(value: string): Uint8Array {
  const normalized = value.replaceAll('-', '+').replaceAll('_', '/');
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function sign(payload: string, secret: string): Promise<string> {
  if (secret.length < 32) {
    throw new MediaError(
      500,
      'media_session_secret_invalid',
      'media session secret is not configured',
    );
  }
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { hash: 'SHA-256', name: 'HMAC' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign(
    'HMAC',
    key,
    new TextEncoder().encode(`max-daily-media-session:v1:${payload}`),
  );
  return base64UrlEncode(new Uint8Array(signature));
}

function equalSignatures(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(';')) {
    const [key, ...value] = part.trim().split('=');
    if (key === name) return value.join('=') || null;
  }
  return null;
}

function invalidSession(): MediaError {
  return new MediaError(
    401,
    'media_session_invalid',
    'media session is invalid or expired',
  );
}

export async function createMediaSessionCookie(
  actorId: string,
  secret: string,
  now = new Date(),
): Promise<string> {
  const payload = base64UrlEncode(new TextEncoder().encode(JSON.stringify({
    actorId,
    exp: Math.floor(now.getTime() / 1000) + MEDIA_SESSION_LIFETIME_SECONDS,
    v: 1,
  } satisfies MediaSessionPayload)));
  const session = `${payload}.${await sign(payload, secret)}`;
  return `${MEDIA_SESSION_COOKIE}=${session}; Max-Age=${MEDIA_SESSION_LIFETIME_SECONDS}; Path=/api/media; HttpOnly; Secure; SameSite=None`;
}

export async function readMediaSession(
  cookieHeader: string,
  secret: string,
  now = new Date(),
): Promise<{ actorId: string } | null> {
  const session = readCookie(cookieHeader, MEDIA_SESSION_COOKIE);
  if (!session) return null;
  const [payload, signature, extra] = session.split('.');
  if (!payload || !signature || extra) throw invalidSession();

  const expected = await sign(payload, secret);
  if (!equalSignatures(signature, expected)) throw invalidSession();

  try {
    const value = JSON.parse(
      new TextDecoder().decode(base64UrlDecode(payload)),
    ) as Partial<MediaSessionPayload>;
    if (
      value.v !== 1
      || typeof value.actorId !== 'string'
      || !value.actorId
      || typeof value.exp !== 'number'
      || value.exp <= Math.floor(now.getTime() / 1000)
    ) {
      throw new Error('invalid payload');
    }
    return { actorId: value.actorId };
  } catch {
    throw invalidSession();
  }
}
