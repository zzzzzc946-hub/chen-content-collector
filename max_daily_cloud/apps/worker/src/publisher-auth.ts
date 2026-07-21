import { MediaError, type PublisherDeviceAuthenticator } from './media.js';

const MIN_SECRET_LENGTH = 32;

export interface PublisherDeviceAuthStorage {
  authenticatePublisherDeviceAtomic(input: {
    tokenHash: string;
    usedAt: Date;
  }): Promise<{ deviceId: string } | null>;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

function requirePepper(pepper: string): void {
  if (pepper.length < MIN_SECRET_LENGTH) {
    throw new MediaError(
      500,
      'publisher_token_pepper_invalid',
      'publisher token pepper is not configured',
    );
  }
}

function readPublisherToken(request: Request): string | null {
  const value = request.headers.get('x-publisher-token');
  if (value === null) return null;
  if (!value) {
    throw new MediaError(
      401,
      'missing_publisher_token',
      'publisher token is required',
    );
  }
  if (value.length < MIN_SECRET_LENGTH) {
    throw new MediaError(
      401,
      'invalid_publisher_token',
      'publisher token is invalid',
    );
  }
  return value;
}

export function createPublisherDeviceAuthenticator(input: {
  now?: () => Date;
  pepper: string;
  storage: PublisherDeviceAuthStorage;
}): PublisherDeviceAuthenticator {
  return {
    async authenticate(request) {
      const token = readPublisherToken(request);
      if (token === null) return null;
      requirePepper(input.pepper);
      const device = await input.storage.authenticatePublisherDeviceAtomic({
        tokenHash: await sha256Hex(`${input.pepper}:${token}`),
        usedAt: input.now?.() ?? new Date(),
      });
      if (!device) {
        throw new MediaError(
          403,
          'publisher_device_unavailable',
          'publisher device is unavailable',
        );
      }
      return device;
    },
  };
}
