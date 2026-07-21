import type { Identity, IdentityRoleStore, IdentityVerifier } from './auth.js';
import type { CollaborationLinkStorage } from './collaboration-links.js';
import type { InvitationStorage } from './invitations.js';
import type {
  MediaBucket,
  MediaObjectStore,
  MediaStorage,
  PublisherDeviceAuthenticator,
} from './media.js';
import type { PublisherDeviceAuthStorage } from './publisher-auth.js';
import type { ReportStorage } from './reports.js';
import type { MobileInboxStore } from './mobile-inbox.js';

export interface RateLimiter {
  limit(input: { key: string }): Promise<{ success: boolean }>;
}

export interface CurrentCollaborationLink {
  createdAt: Date;
  id: string;
  lastUsedAt: Date | null;
}

export interface WorkerServices {
  appOrigin: string;
  identityVerifier: IdentityVerifier;
  invitationClaimRateLimiter: RateLimiter;
  mediaBucket?: MediaBucket;
  mediaObjectStore?: MediaObjectStore;
  mediaSessionSecret: string;
  mobileInbox?: MobileInboxStore;
  mobileInboxRateLimiter?: RateLimiter;
  now?: () => Date;
  ownerEmail: string;
  publicShareRateLimiter: RateLimiter;
  publisherDeviceAuthenticator?: PublisherDeviceAuthenticator;
  readCurrentCollaborationLink?: () => Promise<CurrentCollaborationLink | null>;
  shareCookieSecret: string;
  storage:
    & CollaborationLinkStorage
    & InvitationStorage
    & IdentityRoleStore
    & ReportStorage
    & Partial<PublisherDeviceAuthStorage>
    & Partial<MediaStorage>;
}

export interface WorkerBindings {
  APP_ORIGIN: string;
  FEISHU_APP_ID: string;
  FEISHU_APP_SECRET: string;
  FEISHU_APP_TOKEN: string;
  FEISHU_MOBILE_INBOX_TABLE_ID: string;
  INVITATION_CLAIM_RATE_LIMITER: RateLimiter;
  MEDIA_SESSION_SECRET: string;
  MOBILE_INBOX_RATE_LIMITER: RateLimiter;
  OWNER_EMAIL: string;
  PUBLIC_SHARE_RATE_LIMITER: RateLimiter;
  PUBLISHER_TOKEN_PEPPER: string;
  SHARE_COOKIE_SECRET: string;
  SUPABASE_ANON_KEY: string;
  SUPABASE_SERVICE_ROLE_KEY: string;
  SUPABASE_STORAGE_BUCKET: string;
  SUPABASE_URL: string;
}

export type WorkerAppEnv = {
  Bindings: WorkerBindings;
  Variables: {
    identity: Identity;
    services: WorkerServices;
  };
};
