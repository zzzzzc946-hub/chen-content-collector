import { createHash } from 'node:crypto';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
} from 'vitest';

const migrationsDirectory = fileURLToPath(
  new URL('../migrations/', import.meta.url),
);
const expectedMigrationFiles = [
  '202607100001_initial.sql',
  '202607100002_worker_security.sql',
  '202607100003_report_collaboration.sql',
  '202607100004_media_upload.sql',
  '202607100005_media_recovery.sql',
  '202607100006_media_completion_recovery.sql',
  '202607110007_web_access_sessions.sql',
  '202607110008_publisher_delivery.sql',
  '202607120009_media_retention.sql',
  '202607120010_storage_bucket.sql',
  '202607130011_media_upload_read_lock.sql',
  '202607130012_publisher_draft_reconciliation.sql',
  '202607130013_publisher_reappear_retention_read.sql',
  '202607130014_media_purge_finish_claim_guard.sql',
  '202607140015_storage_bucket_file_limit.sql',
  '202607140016_storage_bucket_free_limit.sql',
  '202607140017_fixed_collaboration_portal.sql',
  '202607150018_backfill_collaboration_snapshot_drift.sql',
  '202607150019_non_retryable_collaboration_conflicts.sql',
];
const immutableMigrationHashes = {
  '202607100001_initial.sql':
    '5a41d671c4e5a353212e661f74bfe68f2aa9e8c7cb78805aa349cb4ad68ffcb6',
  '202607100002_worker_security.sql':
    'cc8b6dbe5af9f741c1a3b79ba08cdc7d5c157c6963a9635d67b57df14c01c46d',
  '202607100003_report_collaboration.sql':
    'e54847620dc17e7cca0e18b21c9dfc1bb11afb2cd7ee2bfc98a776dae9468d5f',
  '202607100004_media_upload.sql':
    '513f401e65078cc304ee6a9546f08a250568f33078eaa0a24abdd3dac0182098',
  '202607100005_media_recovery.sql':
    'e86c6871fb79f0aaf0a4b9024c81b1152d7f8eb51496273496fc2aebda737f99',
  '202607100006_media_completion_recovery.sql':
    'd95848a9fde399fbf5bb7527a4b89e83a5827f3c7fa97e79a39f3498773293ef',
  '202607110007_web_access_sessions.sql':
    '7a97a068166a8cf423c2770a6470b91f1858de3e24bdd96c613ee9b109eb047c',
  '202607110008_publisher_delivery.sql':
    'b94d50da9839a16a6a8266817d57922fcf7fc414577f7faebaca37c313c6370e',
  '202607120009_media_retention.sql':
    '6b5f60f16148a8600d4ad6e7cb54e48a98dc3cc55a3b38b5332ea2693801eb5c',
  '202607120010_storage_bucket.sql':
    '305176a29eecfb5907ab9b84631dd6e793b176b69e56848e0f1b8248eabd3738',
  '202607130011_media_upload_read_lock.sql':
    '485a360db80a8506a3ec09f4fd76b8ba138ff5705d25618dd38f0d8b11cc5e66',
  '202607130012_publisher_draft_reconciliation.sql':
    '7fdc9102a2f30b00885e29ee2dd226c0b32e194f25e8a74d0f84ebbb5ba33e6d',
  '202607130013_publisher_reappear_retention_read.sql':
    'f23cca55369b5ba95d47e8e70c50a5a862f4628f474f4dfb34861817c1a613de',
  '202607130014_media_purge_finish_claim_guard.sql':
    '49c48092ff67729ea9ed1bdb09d009773717c4cf69cb32a4c20bdb1a6aad98b1',
  '202607140015_storage_bucket_file_limit.sql':
    'dd4f954842bf11bbe5f16ce360d39350113a95af7ba7d18b03e26023a5731451',
  '202607140016_storage_bucket_free_limit.sql':
    '2f073a09748b0c3ee9f301b6305c5356626aa68a88b76c271f25b8e47c2d5cb7',
  '202607140017_fixed_collaboration_portal.sql':
    'b7d446f3004f823d0f7437a772af41b524c0144324348cef46cc5c017e441a0e',
} as const;

const expectedTables = [
  'audit_logs',
  'invitations',
  'media_objects',
  'media_upload_parts',
  'media_uploads',
  'portal_collaboration_links',
  'profiles',
  'publisher_devices',
  'report_items',
  'report_members',
  'report_publication_items',
  'report_publications',
  'reports',
  'revisions',
  'share_links',
];

type Database = {
  close(): Promise<void>;
  exec(sql: string): Promise<unknown>;
  query<T>(sql: string, params?: unknown[]): Promise<{ rows: T[] }>;
};

let db: Database;
let migrationFiles: string[];

async function createDatabaseWithMigrations(
  files: string[],
): Promise<Database> {
  const moduleName = '@electric-sql/pglite';
  const { PGlite } = await import(/* @vite-ignore */ moduleName);
  const database = new PGlite() as Database;
  await database.exec(`
    create schema auth;
    create schema storage;
    create table auth.users (
      id uuid primary key,
      email text,
      email_confirmed_at timestamptz
    );
    create table storage.buckets (
      id text primary key,
      name text not null,
      public boolean not null default false,
      file_size_limit bigint
    );
    create function auth.uid()
    returns uuid
    language sql
    stable
    as $$
      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
    $$;

    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin bypassrls;
  `);
  for (const file of files) {
    await database.exec(await readFile(`${migrationsDirectory}/${file}`, 'utf8'));
  }
  return database;
}

beforeAll(async () => {
  migrationFiles = (await readdir(migrationsDirectory))
    .filter((file) => file.endsWith('.sql'))
    .sort();
  db = await createDatabaseWithMigrations(migrationFiles);
}, 30_000);

beforeEach(async () => {
  await db.exec('begin');
});

afterEach(async () => {
  await db.exec('rollback');
});

afterAll(async () => {
  await db?.close();
});

async function seedReadyReport(database: Database = db): Promise<void> {
  await database.exec(`
    insert into auth.users(id, email, email_confirmed_at) values
      (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        '2026-07-10T07:00:00.000Z'
      ),
      (
        '00000000-0000-0000-0000-000000000002',
        'max@example.com',
        '2026-07-10T07:00:00.000Z'
      ),
      (
        '00000000-0000-0000-0000-000000000003',
        'viewer@example.com',
        '2026-07-10T07:00:00.000Z'
      );
    insert into public.profiles(id, email, global_role) values
      (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        'owner'
      ),
      (
        '00000000-0000-0000-0000-000000000002',
        'max@example.com',
        'viewer'
      ),
      (
        '00000000-0000-0000-0000-000000000003',
        'viewer@example.com',
        'viewer'
      );
    insert into public.reports(id, daily_date, source_table_id)
    values (
      '00000000-0000-0000-0000-000000000101',
      '2026-07-10',
      'source-table'
    );
    insert into public.report_items(
      id,
      report_id,
      local_record_id,
      title,
      caption,
      source_url,
      max_daily_card,
      item_order
    ) values (
      '00000000-0000-0000-0000-000000000201',
      '00000000-0000-0000-0000-000000000101',
      'local-1',
      'source title',
      'source caption',
      'https://example.com/source',
      'published card',
      10
    );
    insert into public.media_objects(
      id,
      report_item_id,
      object_key,
      content_type,
      byte_size,
      sha256,
      upload_status,
      uploaded_at
    ) values (
      '00000000-0000-0000-0000-000000000301',
      '00000000-0000-0000-0000-000000000201',
      'reports/2026-07-10/video.mp4',
      'video/mp4',
      1024,
      'ready-sha256',
      'ready',
      '2026-07-10T08:59:00.000Z'
    );
    insert into public.report_members(report_id, user_id, role, created_by) values
      (
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000002',
        'editor',
        '00000000-0000-0000-0000-000000000001'
      ),
      (
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000003',
        'viewer',
        '00000000-0000-0000-0000-000000000001'
      );
  `);
}

async function expectPublisherDeviceDenied(tokenHash: string): Promise<void> {
  await db.exec('savepoint publisher_device_denied');
  try {
    await expect(db.query(`
      select *
      from public.authenticate_publisher_device(
        '${tokenHash}',
        '2026-07-10T09:31:00.000Z'
      )
    `)).rejects.toThrow(/publisher_device_unavailable/);
  } finally {
    await db.exec('rollback to savepoint publisher_device_denied');
    await db.exec('release savepoint publisher_device_denied');
  }
}

async function expectPublisherReportDenied(
  reportId: string,
  expectedDraftVersion: number,
): Promise<void> {
  await db.exec('savepoint publisher_report_denied');
  try {
    await expect(db.query(`
      select *
      from public.publish_publisher_report(
        '${reportId}',
        '00000000-0000-0000-0000-000000000901',
        ${expectedDraftVersion},
        '2026-07-10T09:39:00.000Z'
      )
    `)).rejects.toThrow(/stale_version/);
  } finally {
    await db.exec('rollback to savepoint publisher_report_denied');
    await db.exec('release savepoint publisher_report_denied');
  }
}

async function seedActivePublisherDevice(): Promise<void> {
  await db.exec(`
    insert into public.publisher_devices(
      id,
      name,
      token_hash,
      created_by
    ) values (
      '00000000-0000-0000-0000-000000000901',
      'active owner device',
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      '00000000-0000-0000-0000-000000000001'
    );
  `);
}

const ownerId = '00000000-0000-0000-0000-000000000001';
const collaborationLinkId = '00000000-0000-0000-0000-000000000701';

async function seedPublishedCollaborationReports(): Promise<void> {
  await seedReadyReport();
  await db.exec(`
    insert into public.reports(id, daily_date, source_table_id) values
      (
        '00000000-0000-0000-0000-000000000102',
        '2026-07-09',
        'older-source-table'
      ),
      (
        '00000000-0000-0000-0000-000000000103',
        '2026-07-11',
        'draft-source-table'
      );
    insert into public.report_items(
      id,
      report_id,
      local_record_id,
      title,
      caption,
      source_url,
      max_daily_card,
      item_order
    ) values
      (
        '00000000-0000-0000-0000-000000000202',
        '00000000-0000-0000-0000-000000000102',
        'older-local-1',
        'older published title',
        'older published caption',
        'https://example.com/older',
        'older published card',
        10
      ),
      (
        '00000000-0000-0000-0000-000000000203',
        '00000000-0000-0000-0000-000000000103',
        'draft-local-1',
        'draft title',
        'draft caption',
        'https://example.com/draft',
        'draft card',
        10
      );
    insert into public.media_objects(
      id,
      report_item_id,
      object_key,
      content_type,
      byte_size,
      sha256,
      upload_status,
      uploaded_at
    ) values
      (
        '00000000-0000-0000-0000-000000000302',
        '00000000-0000-0000-0000-000000000202',
        'reports/2026-07-09/video.mp4',
        'video/mp4',
        2048,
        'older-ready-sha256',
        'ready',
        '2026-07-09T08:59:00.000Z'
      ),
      (
        '00000000-0000-0000-0000-000000000303',
        '00000000-0000-0000-0000-000000000203',
        'reports/2026-07-11/draft.mp4',
        'video/mp4',
        4096,
        'draft-ready-sha256',
        'ready',
        '2026-07-11T08:59:00.000Z'
      );
  `);
  await db.query(`
    select *
    from public.publish_report(
      '00000000-0000-0000-0000-000000000102',
      '${ownerId}',
      '2026-07-09T09:00:00.000Z'
    )
  `);
  await db.query(`
    select *
    from public.publish_report(
      '00000000-0000-0000-0000-000000000101',
      '${ownerId}',
      '2026-07-10T09:00:00.000Z'
    )
  `);
  await db.query(`
    select *
    from public.replace_active_portal_collaboration_link(
      '${collaborationLinkId}',
      '${'a'.repeat(64)}',
      '${ownerId}',
      '2026-07-10T09:01:00.000Z'
    )
  `);
}

async function expectDatabaseError(
  savepoint: string,
  sql: string,
  expected: { code: string; message: string },
): Promise<void> {
  await db.exec(`savepoint ${savepoint}`);
  try {
    await expect(db.query(sql)).rejects.toMatchObject(expected);
  } finally {
    await db.exec(`rollback to savepoint ${savepoint}`);
    await db.exec(`release savepoint ${savepoint}`);
  }
}

async function readCollaborationMutationState(): Promise<Record<string, unknown>> {
  const result = await db.query<Record<string, unknown>>(`
    select
      r.draft_version,
      ri.version as live_version,
      length(ri.max_daily_card) as live_card_length,
      length(ri.max_feedback) as live_feedback_length,
      length(ri.review_status) as live_status_length,
      rpi.item_version as snapshot_version,
      length(rpi.max_daily_card) as snapshot_card_length,
      length(rpi.max_feedback) as snapshot_feedback_length,
      length(rpi.review_status) as snapshot_status_length,
      (
        select count(*)::integer
        from public.revisions rv
        where rv.report_item_id = ri.id
      ) as revision_count,
      (
        select count(*)::integer
        from public.audit_logs al
        where al.event_type = 'report_item.updated'
          and al.target_id = ri.id
      ) as audit_count
    from public.reports r
    join public.report_items ri
      on ri.report_id = r.id
    join public.report_publication_items rpi
      on rpi.report_id = r.id
      and rpi.published_version = r.published_version
      and rpi.report_item_id = ri.id
    where ri.id = '00000000-0000-0000-0000-000000000201'
  `);
  return result.rows[0]!;
}

describe('Supabase permissions migration', () => {
  it('retains ready Supabase media for exactly 72 hours and retries failed purges', async () => {
    await seedReadyReport();

    const lifecycle = await db.query<{
      purge_after: Date;
      purge_matches_upload: boolean;
    }>(`
      select
        purge_after,
        purge_after = uploaded_at + interval '72 hours' as purge_matches_upload
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `);
    expect(lifecycle.rows).toEqual([{
      purge_after: new Date('2026-07-13T08:59:00.000Z'),
      purge_matches_upload: true,
    }]);

    await db.exec(`
      update public.media_objects
      set uploaded_at = '2026-07-10T09:30:00.000Z'
      where id = '00000000-0000-0000-0000-000000000301'
    `);
    expect((await db.query<{ purge_after: Date }>(`
      select purge_after
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      purge_after: new Date('2026-07-13T08:59:00.000Z'),
    }]);

    const claimed = await db.query<{
      id: string;
      object_key: string;
    }>(`
      select id, object_key
      from public.claim_expired_media_objects(
        '2026-07-13T09:00:00.000Z',
        100
      )
    `);
    expect(claimed.rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000301',
      object_key: 'reports/2026-07-10/video.mp4',
    }]);
    expect((await db.query<{ purge_attempted_at: Date }>(`
      select purge_attempted_at
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      purge_attempted_at: new Date('2026-07-13T09:00:00.000Z'),
    }]);
    expect((await db.query<{ id: string }>(`
      select id
      from public.claim_expired_media_objects(
        '2026-07-13T09:00:00.000Z',
        100
      )
    `)).rows).toEqual([]);

    await db.query(`
      select public.finish_media_object_purge(
        '00000000-0000-0000-0000-000000000301',
        null,
        'storage_delete_failed'
      )
    `);
    const retried = await db.query<{ id: string }>(`
      select id
      from public.claim_expired_media_objects(
        '2026-07-13T10:00:00.000Z',
        100
      )
    `);
    expect(retried.rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000301',
    }]);

    await db.query(`
      select public.finish_media_object_purge(
        '00000000-0000-0000-0000-000000000301',
        '2026-07-13T10:00:00.000Z',
        null
      )
    `);
    await db.query(`
      select public.finish_media_object_purge(
        '00000000-0000-0000-0000-000000000301',
        '2026-07-13T10:05:00.000Z',
        'late_retry_after_success'
      )
    `);
    expect((await db.query<{
      purge_error: string | null;
      purged_at: Date | null;
    }>(`
      select purge_error, purged_at
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      purge_error: null,
      purged_at: new Date('2026-07-13T10:00:00.000Z'),
    }]);
    expect((await db.query<{ id: string }>(`
      select id
      from public.claim_expired_media_objects(
        '2026-07-13T11:00:00.000Z',
        100
      )
    `)).rows).toEqual([]);
  });

  it('ignores late purge completion after a claimed object receives a fresh finalized upload', async () => {
    await seedReadyReport();
    await seedActivePublisherDevice();

    const claimed = await db.query<{ id: string; object_key: string }>(`
      select id, object_key
      from public.claim_expired_media_objects(
        '2026-07-13T09:00:00.000Z',
        100
      )
    `);
    expect(claimed.rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000301',
      object_key: 'reports/2026-07-10/video.mp4',
    }]);

    await db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        null,
        '00000000-0000-0000-0000-000000000901',
        'video/mp4',
        8,
        '2026-07-14T09:01:00.000Z'
      )
    `);
    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        'r2-fresh-upload',
        '2026-07-14T09:02:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.record_media_upload_part(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        1,
        'etag-fresh',
        8,
        '2026-07-14T09:03:00.000Z'
      )
    `);
    await db.query(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        '2026-07-14T09:04:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.finalize_media_upload_completion(
        '00000000-0000-0000-0000-000000000502',
        '2026-07-14T09:05:00.000Z'
      )
    `);

    await db.query(`
      select public.finish_media_object_purge(
        '00000000-0000-0000-0000-000000000301',
        '2026-07-14T09:06:00.000Z',
        null
      )
    `);

    expect((await db.query<{
      object_key: string;
      purge_after: Date;
      purge_attempted_at: Date | null;
      purge_error: string | null;
      purged_at: Date | null;
    }>(`
      select
        object_key,
        purge_after,
        purge_attempted_at,
        purge_error,
        purged_at
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000502',
      purge_after: new Date('2026-07-17T09:05:00.000Z'),
      purge_attempted_at: null,
      purge_error: null,
      purged_at: null,
    }]);
  });

  it('authenticates only active owner-created publisher devices', async () => {
    await seedReadyReport();
    await db.exec(`
      insert into public.publisher_devices(
        id,
        name,
        token_hash,
        created_by,
        revoked_at
      ) values
        (
          '00000000-0000-0000-0000-000000000901',
          'active owner device',
          'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          '00000000-0000-0000-0000-000000000001',
          null
        ),
        (
          '00000000-0000-0000-0000-000000000902',
          'revoked owner device',
          'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T08:00:00.000Z'
        ),
        (
          '00000000-0000-0000-0000-000000000903',
          'viewer device',
          'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
          '00000000-0000-0000-0000-000000000002',
          null
        );
    `);

    const accepted = await db.query<{ device_id: string }>(`
      select *
      from public.authenticate_publisher_device(
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        '2026-07-10T09:30:00.000Z'
      )
    `);
    const lastUsed = await db.query<{ last_used_epoch: number }>(`
      select extract(epoch from last_used_at)::integer as last_used_epoch
      from public.publisher_devices
      where id = '00000000-0000-0000-0000-000000000901'
    `);

    expect(accepted.rows).toEqual([{
      device_id: '00000000-0000-0000-0000-000000000901',
    }]);
    expect(lastUsed.rows[0]?.last_used_epoch).toBe(1783675800);

    for (const tokenHash of [
      'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      'short',
    ]) {
      await expectPublisherDeviceDenied(tokenHash);
    }
  });

  it('upserts publisher drafts idempotently and publishes with a version guard', async () => {
    await seedReadyReport();
    await seedActivePublisherDevice();
    await db.exec(`
      update public.report_items
      set max_daily_card = 'editor card',
          max_feedback = 'editor feedback',
          review_status = 'pending'
      where id = '00000000-0000-0000-0000-000000000201';
    `);

    const first = await db.query<{ report: Record<string, unknown> }>(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "refreshed title",
          "caption": "refreshed caption",
          "source_url": "https://example.com/refreshed",
          "max_daily_card": "publisher card",
          "max_feedback": "publisher feedback",
          "review_status": "approved",
          "item_order": 1
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:30:00.000Z'
      ) as report
    `);
    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "refreshed title again",
          "caption": "refreshed caption again",
          "source_url": "https://example.com/refreshed-again",
          "max_daily_card": "publisher card",
          "max_feedback": "publisher feedback",
          "review_status": "approved",
          "item_order": 1
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:31:00.000Z'
      )
    `);
    const rows = await db.query<{
      item_count: number;
      max_daily_card: string;
      max_feedback: string;
      media_count: number;
      source_url: string;
    }>(`
      select
        count(*)::integer as item_count,
        max(ri.max_daily_card) as max_daily_card,
        max(ri.max_feedback) as max_feedback,
        count(mo.id)::integer as media_count,
        max(ri.source_url) as source_url
      from public.report_items ri
      left join public.media_objects mo on mo.report_item_id = ri.id
      where ri.report_id = '00000000-0000-0000-0000-000000000101'
    `);

    expect(first.rows[0]?.report).toMatchObject({
      id: '00000000-0000-0000-0000-000000000101',
    });
    expect(rows.rows).toEqual([{
      item_count: 1,
      max_daily_card: 'editor card',
      max_feedback: 'editor feedback',
      media_count: 1,
      source_url: 'https://example.com/refreshed-again',
    }]);

    const report = first.rows[0]?.report as { draft_version: number };
    await expectPublisherReportDenied(
      '00000000-0000-0000-0000-000000000101',
      report.draft_version - 1,
    );
    const published = await db.query<{
      published_version: number;
      status: string;
    }>(`
      select published_version, status::text
      from public.publish_publisher_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000901',
        (select draft_version from public.reports where id = '00000000-0000-0000-0000-000000000101'),
        '2026-07-10T09:40:00.000Z'
      )
    `);
    expect(published.rows).toEqual([{
      published_version: 1,
      status: 'published',
    }]);

    const retried = await db.query<{
      published_version: number;
      status: string;
    }>(`
      select published_version, status::text
      from public.publish_publisher_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000901',
        (select draft_version from public.reports where id = '00000000-0000-0000-0000-000000000101'),
        '2026-07-10T09:41:00.000Z'
      )
    `);
    const publicationCount = await db.query<{ count: number }>(`
      select count(*)::integer as count
      from public.report_publications
      where report_id = '00000000-0000-0000-0000-000000000101'
    `);
    expect(retried.rows).toEqual([{
      published_version: 1,
      status: 'published',
    }]);
    expect(publicationCount.rows).toEqual([{ count: 1 }]);
  });

  it('reconciles omitted publisher draft items without deleting retained media rows', async () => {
    await seedReadyReport();
    await seedActivePublisherDevice();
    await db.exec(`
      update public.report_items
      set max_daily_card = 'editor card',
          max_feedback = 'editor feedback',
          review_status = 'approved'
      where id = '00000000-0000-0000-0000-000000000201';
    `);

    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1",
          "caption": "local 1 caption",
          "source_url": "https://example.com/local-1",
          "item_order": 20
        }, {
          "local_record_id": "local-2",
          "title": "local 2",
          "caption": "local 2 caption",
          "source_url": "https://example.com/local-2",
          "item_order": 10
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:30:00.000Z'
      )
    `);
    await db.exec(`
      update public.media_objects mo
      set object_key = 'reports/2026-07-10/local-2.mp4',
          content_type = 'video/mp4',
          byte_size = 2048,
          sha256 = 'local-2-sha256',
          upload_status = 'ready',
          uploaded_at = '2026-07-10T09:31:00.000Z'
      from public.report_items ri
      where ri.id = mo.report_item_id
        and ri.report_id = '00000000-0000-0000-0000-000000000101'
        and ri.local_record_id = 'local-2';
    `);

    const reconciled = await db.query<{ report: { items: unknown[] } }>(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1 refreshed",
          "caption": "local 1 refreshed caption",
          "source_url": "https://example.com/local-1-refreshed",
          "item_order": 5
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:32:00.000Z'
      ) as report
    `);
    expect(reconciled.rows[0]?.report.items).toEqual([
      expect.objectContaining({
        local_record_id: 'local-1',
        max_daily_card: 'editor card',
        max_feedback: 'editor feedback',
      }),
    ]);

    const itemState = await db.query<{
      local_record_id: string;
      media_count: number;
      publisher_removed_at: Date | null;
      purge_after: Date | null;
    }>(`
      select
        ri.local_record_id,
        count(mo.id)::integer as media_count,
        ri.publisher_removed_at,
        max(mo.purge_after) as purge_after
      from public.report_items ri
      left join public.media_objects mo on mo.report_item_id = ri.id
      where ri.report_id = '00000000-0000-0000-0000-000000000101'
      group by ri.local_record_id, ri.publisher_removed_at
      order by ri.local_record_id
    `);
    expect(itemState.rows).toEqual([
      {
        local_record_id: 'local-1',
        media_count: 1,
        publisher_removed_at: null,
        purge_after: new Date('2026-07-13T08:59:00.000Z'),
      },
      {
        local_record_id: 'local-2',
        media_count: 1,
        publisher_removed_at: new Date('2026-07-10T09:32:00.000Z'),
        purge_after: new Date('2026-07-13T09:31:00.000Z'),
      },
    ]);

    await db.query(`
      select *
      from public.publish_publisher_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000901',
        (select draft_version from public.reports where id = '00000000-0000-0000-0000-000000000101'),
        '2026-07-10T09:40:00.000Z'
      )
    `);

    const snapshot = await db.query<{
      local_record_id: string;
      item_order: number;
    }>(`
      select local_record_id, item_order
      from public.report_publication_items
      where report_id = '00000000-0000-0000-0000-000000000101'
      order by item_order, report_item_id
    `);
    expect(snapshot.rows).toEqual([{
      local_record_id: 'local-1',
      item_order: 5,
    }]);
  });

  it('excludes omitted publisher items from trusted draft reads', async () => {
    await seedReadyReport();
    await seedActivePublisherDevice();

    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1",
          "caption": "local 1 caption",
          "source_url": "https://example.com/local-1",
          "item_order": 20
        }, {
          "local_record_id": "local-2",
          "title": "local 2",
          "caption": "local 2 caption",
          "source_url": "https://example.com/local-2",
          "item_order": 10
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:30:00.000Z'
      )
    `);
    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1 refreshed",
          "caption": "local 1 refreshed caption",
          "source_url": "https://example.com/local-1-refreshed",
          "item_order": 5
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:32:00.000Z'
      )
    `);

    const draftRead = await db.query<{ local_record_ids: string[] }>(`
      select array(
        select item->>'local_record_id'
        from jsonb_array_elements(
          public.read_report_with_access_role(
            '00000000-0000-0000-0000-000000000101',
            '00000000-0000-0000-0000-000000000001',
            null,
            '2026-07-10T09:33:00.000Z'
          )->'items'
        ) as item
      ) as local_record_ids
    `);

    expect(draftRead.rows).toEqual([{
      local_record_ids: ['local-1'],
    }]);
  });

  it('resets stale purge metadata when an omitted publisher item reappears and finalizes fresh media', async () => {
    await seedReadyReport();
    await seedActivePublisherDevice();

    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1",
          "caption": "local 1 caption",
          "source_url": "https://example.com/local-1",
          "item_order": 20
        }, {
          "local_record_id": "local-2",
          "title": "local 2",
          "caption": "local 2 caption",
          "source_url": "https://example.com/local-2",
          "item_order": 10
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:30:00.000Z'
      )
    `);
    await db.exec(`
      update public.media_objects mo
      set object_key = 'reports/2026-07-10/local-2.mp4',
          content_type = 'video/mp4',
          byte_size = 2048,
          sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          upload_status = 'ready',
          uploaded_at = '2026-07-10T09:31:00.000Z'
      from public.report_items ri
      where ri.id = mo.report_item_id
        and ri.report_id = '00000000-0000-0000-0000-000000000101'
        and ri.local_record_id = 'local-2';
    `);
    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1 refreshed",
          "caption": "local 1 refreshed caption",
          "source_url": "https://example.com/local-1-refreshed",
          "item_order": 5
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-10T09:32:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.claim_expired_media_objects(
        '2026-07-13T09:31:00.000Z',
        100
      )
    `);
    await db.query(`
      select public.finish_media_object_purge(
        (
          select mo.id
          from public.media_objects mo
          join public.report_items ri on ri.id = mo.report_item_id
          where ri.local_record_id = 'local-2'
        ),
        '2026-07-13T09:32:00.000Z',
        null
      )
    `);

    await db.query(`
      select public.upsert_publisher_draft(
        '2026-07-10',
        'source-table',
        '[{
          "local_record_id": "local-1",
          "title": "local 1 refreshed",
          "caption": "local 1 refreshed caption",
          "source_url": "https://example.com/local-1-refreshed",
          "item_order": 5
        }, {
          "local_record_id": "local-2",
          "title": "local 2 returned",
          "caption": "local 2 returned caption",
          "source_url": "https://example.com/local-2-returned",
          "item_order": 10
        }]'::jsonb,
        '00000000-0000-0000-0000-000000000901',
        '2026-07-14T09:00:00.000Z'
      )
    `);

    const upload = await db.query<{ upload: { media_id: string } }>(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000101',
        (
          select id
          from public.report_items
          where report_id = '00000000-0000-0000-0000-000000000101'
            and local_record_id = 'local-2'
        ),
        null,
        '00000000-0000-0000-0000-000000000901',
        'video/mp4',
        8,
        '2026-07-14T09:01:00.000Z'
      ) as upload
    `);
    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        'r2-returned-upload',
        '2026-07-14T09:02:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.record_media_upload_part(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        1,
        'etag-returned',
        8,
        '2026-07-14T09:03:00.000Z'
      )
    `);
    await db.query(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000502',
        null,
        '00000000-0000-0000-0000-000000000901',
        'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        '2026-07-14T09:04:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.finalize_media_upload_completion(
        '00000000-0000-0000-0000-000000000502',
        '2026-07-14T09:05:00.000Z'
      )
    `);

    const media = await db.query<{
      media_id: string;
      purge_after: Date;
      purge_attempted_at: Date | null;
      purge_error: string | null;
      purged_at: Date | null;
    }>(`
      select
        mo.id as media_id,
        mo.purge_after,
        mo.purged_at,
        mo.purge_attempted_at,
        mo.purge_error
      from public.media_objects mo
      join public.report_items ri on ri.id = mo.report_item_id
      where ri.local_record_id = 'local-2'
    `);

    expect(media.rows).toEqual([{
      media_id: upload.rows[0]!.upload.media_id,
      purge_after: new Date('2026-07-17T09:05:00.000Z'),
      purged_at: null,
      purge_attempted_at: null,
      purge_error: null,
    }]);
    const immediatelyClaimed = await db.query<{ id: string }>(`
      select id
      from public.claim_expired_media_objects(
        '2026-07-14T09:06:00.000Z',
        100
      )
    `);
    expect(immediatelyClaimed.rows).not.toContainEqual({
      id: upload.rows[0]!.upload.media_id,
    });
  });

  it('returns trusted owner, editor, viewer, and public access roles', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      insert into public.share_links(
        id,
        report_id,
        token_hash,
        created_by,
        created_at
      ) values (
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        'trusted-role-share',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);

    const result = await db.query<{ access_role: string }>(`
      select
        public.read_report_with_access_role(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000001',
          null,
          '2026-07-10T09:01:00.000Z'
        )->>'access_role' as access_role
      union all
      select
        public.read_report_with_access_role(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000002',
          null,
          '2026-07-10T09:01:00.000Z'
        )->>'access_role'
      union all
      select
        public.read_report_with_access_role(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000003',
          null,
          '2026-07-10T09:01:00.000Z'
        )->>'access_role'
      union all
      select
        public.read_report_with_access_role(
          '00000000-0000-0000-0000-000000000101',
          null,
          '00000000-0000-0000-0000-000000000401',
          '2026-07-10T09:01:00.000Z'
        )->>'access_role'
    `);

    expect(result.rows.map(({ access_role }) => access_role)).toEqual([
      'owner',
      'editor',
      'viewer',
      'public_reader',
    ]);
  });

  it('loads every additive migration in sorted order and upgrades a Task 2 database', async () => {
    expect(migrationFiles).toEqual(expectedMigrationFiles);

    const upgraded = await createDatabaseWithMigrations([migrationFiles[0]!]);
    try {
      for (const file of migrationFiles.slice(1)) {
        await upgraded.exec(await readFile(`${migrationsDirectory}/${file}`, 'utf8'));
      }
      const result = await upgraded.query<{ present: boolean }>(`
        select
          to_regclass('public.report_publications') is not null
          and to_regclass('public.media_uploads') is not null
          and to_regprocedure(
            'public.read_report(uuid,uuid,uuid,timestamp with time zone)'
          ) is not null
          and to_regprocedure(
            'public.authorize_media_read(uuid,uuid,uuid,uuid,timestamp with time zone)'
          ) is not null
          as present
      `);
      expect(result.rows).toEqual([{ present: true }]);
    } finally {
      await upgraded.close();
    }
  }, 30_000);

  it('provisions the private max-daily-media bucket idempotently', async () => {
    const migration = await readFile(
      `${migrationsDirectory}/202607120010_storage_bucket.sql`,
      'utf8',
    );
    expect(migration).not.toMatch(/storage\.objects|create\s+policy/i);

    await db.exec(`
      delete from storage.buckets where id = 'max-daily-media';
      insert into storage.buckets (id, name, public)
      values ('max-daily-media', 'legacy-public-media', true);
    `);

    await db.exec(await readFile(
      `${migrationsDirectory}/202607120010_storage_bucket.sql`,
      'utf8',
    ));

    const result = await db.query<{
      id: string;
      name: string;
      public: boolean;
    }>(`
      select id, name, public
      from storage.buckets
      where id = 'max-daily-media'
    `);

    expect(result.rows).toEqual([{
      id: 'max-daily-media',
      name: 'max-daily-media',
      public: false,
    }]);

    await db.exec(await readFile(
      `${migrationsDirectory}/202607120010_storage_bucket.sql`,
      'utf8',
    ));

    expect((await db.query(`
      select id, name, public
      from storage.buckets
      where id = 'max-daily-media'
    `)).rows).toEqual(result.rows);
  });

  it('sets the private media bucket limit to exactly 200 MiB idempotently', async () => {
    const migration = await readFile(
      `${migrationsDirectory}/202607140015_storage_bucket_file_limit.sql`,
      'utf8',
    );
    expect(migration).not.toMatch(/storage\.objects|create\s+policy/i);

    await db.exec(`
      update storage.buckets
      set public = true,
          file_size_limit = 1048576
      where id = 'max-daily-media';
    `);

    await db.exec(migration);

    const result = await db.query<{
      id: string;
      public: boolean;
      file_size_limit: number;
    }>(`
      select id, public, file_size_limit
      from storage.buckets
      where id = 'max-daily-media'
    `);

    expect(result.rows).toEqual([{
      id: 'max-daily-media',
      public: false,
      file_size_limit: 209715200,
    }]);

    await db.exec(migration);

    expect((await db.query(`
      select id, public, file_size_limit
      from storage.buckets
      where id = 'max-daily-media'
    `)).rows).toEqual(result.rows);
  });

  it('pins the private media bucket to the free-plan proxy limit', async () => {
    const migration = await readFile(
      `${migrationsDirectory}/202607140016_storage_bucket_free_limit.sql`,
      'utf8',
    );
    expect(migration).not.toMatch(/storage\.objects|create\s+policy/i);
    await db.exec(`
      update storage.buckets
      set public = true, file_size_limit = 209715200
      where id = 'max-daily-media';
    `);
    await db.exec(migration);
    const result = await db.query(`
      select id, public, file_size_limit
      from storage.buckets
      where id = 'max-daily-media'
    `);
    expect(result.rows).toEqual([{
      id: 'max-daily-media',
      public: false,
      file_size_limit: 47185920,
    }]);
    await db.exec(migration);
    expect((await db.query(`
      select id, public, file_size_limit
      from storage.buckets
      where id = 'max-daily-media'
    `)).rows).toEqual(result.rows);
  });

  it('keeps migrations 001-017 immutable and upgrades an existing 004 database', async () => {
    expect(Object.keys(immutableMigrationHashes)).toEqual(
      expectedMigrationFiles.slice(0, 17),
    );
    for (const [file, expectedHash] of Object.entries(
      immutableMigrationHashes,
    )) {
      const contents = await readFile(`${migrationsDirectory}/${file}`);
      expect(createHash('sha256').update(contents).digest('hex')).toBe(
        expectedHash,
      );
    }

    const upgraded = await createDatabaseWithMigrations(
      expectedMigrationFiles.slice(0, 4),
    );
    try {
      await seedReadyReport(upgraded);
      const before = await upgraded.query<{
        byte_size: number;
        object_key: string;
        upload_status: string;
      }>(`
        select object_key, byte_size, upload_status
        from public.media_objects
        where id = '00000000-0000-0000-0000-000000000301'
      `);
      await upgraded.exec(
        await readFile(
          `${migrationsDirectory}/202607100005_media_recovery.sql`,
          'utf8',
        ),
      );
      const after = await upgraded.query<{
        byte_size: number;
        object_key: string;
        sha256_source: string;
        sha256_verification_status: string;
        upload_status: string;
      }>(`
        select
          object_key,
          byte_size,
          upload_status,
          sha256_source,
          sha256_verification_status
        from public.media_objects
        where id = '00000000-0000-0000-0000-000000000301'
      `);

      expect(after.rows).toEqual([{
        ...before.rows[0]!,
        sha256_source: 'trusted_uploader_assertion',
        sha256_verification_status: 'not_server_verified',
      }]);
      expect((await upgraded.query<{ present: boolean }>(`
        select
          to_regprocedure(
            'public.claim_media_upload_completion(uuid,uuid,uuid,text,timestamp with time zone)'
          ) is not null
          and to_regprocedure(
            'public.finalize_media_upload_abort(uuid,timestamp with time zone)'
          ) is not null
          as present
      `)).rows).toEqual([{ present: true }]);
    } finally {
      await upgraded.close();
    }
  }, 30_000);

  it('backfills collaborative snapshot drift created before migration 017', async () => {
    const upgraded = await createDatabaseWithMigrations(
      expectedMigrationFiles.slice(0, 16),
    );
    try {
      await seedReadyReport(upgraded);
      await upgraded.query(`
        select *
        from public.publish_report(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T09:00:00.000Z'
        )
      `);
      await upgraded.query(`
        select *
        from public.update_collaborative_field(
          '00000000-0000-0000-0000-000000000201',
          'max_daily_card',
          'Owner edit before migration 017',
          1,
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T09:01:00.000Z'
        )
      `);

      for (const file of expectedMigrationFiles.slice(16)) {
        await upgraded.exec(
          await readFile(`${migrationsDirectory}/${file}`, 'utf8'),
        );
      }

      const versions = await upgraded.query<{
        live_card: string;
        live_version: number;
        snapshot_card: string;
        snapshot_version: number;
      }>(`
        select
          ri.max_daily_card as live_card,
          ri.version as live_version,
          rpi.max_daily_card as snapshot_card,
          rpi.item_version as snapshot_version
        from public.report_items ri
        join public.reports r on r.id = ri.report_id
        join public.report_publication_items rpi
          on rpi.report_id = r.id
          and rpi.published_version = r.published_version
          and rpi.report_item_id = ri.id
        where ri.id = '00000000-0000-0000-0000-000000000201'
      `);
      expect(versions.rows).toEqual([{
        live_card: 'Owner edit before migration 017',
        live_version: 2,
        snapshot_card: 'Owner edit before migration 017',
        snapshot_version: 2,
      }]);
    } finally {
      await upgraded.close();
    }
  }, 30_000);

  it('wraps the lock-bearing snapshot backfill in an explicit transaction', async () => {
    const migration = await readFile(
      `${migrationsDirectory}/202607150018_backfill_collaboration_snapshot_drift.sql`,
      'utf8',
    );
    expect(migration).toMatch(/^begin;\s+lock table/i);
    expect(migration).toMatch(/commit;\s*$/i);
  });

  it('classifies stale collaborative writes as non-retryable conflicts', async () => {
    const functions = await db.query<{
      has_non_retryable_conflict: boolean;
      has_retryable_conflict: boolean;
      proname: string;
    }>(`
      select
        p.proname,
        p.prosrc like '%errcode = ''P0001''%' as has_non_retryable_conflict,
        p.prosrc like '%errcode = ''40001''%' as has_retryable_conflict
      from pg_catalog.pg_proc p
      join pg_catalog.pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'public'
        and p.proname in (
          'update_collaborative_field',
          'update_collaborative_field_for_link'
        )
      order by p.proname
    `);
    expect(functions.rows).toEqual([
      {
        has_non_retryable_conflict: true,
        has_retryable_conflict: false,
        proname: 'update_collaborative_field',
      },
      {
        has_non_retryable_conflict: true,
        has_retryable_conflict: false,
        proname: 'update_collaborative_field_for_link',
      },
    ]);
  });

  it('recovers a published ready object from an in-flight 004 replacement', async () => {
    const upgraded = await createDatabaseWithMigrations(
      expectedMigrationFiles.slice(0, 4),
    );
    try {
      await seedReadyReport(upgraded);
      await upgraded.query(`
        select *
        from public.publish_report(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T09:00:00.000Z'
        )
      `);
      await upgraded.query(`
        select public.begin_media_upload(
          '00000000-0000-0000-0000-000000000501',
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000201',
          '00000000-0000-0000-0000-000000000001',
          null,
          'video/mp4',
          8,
          '2026-07-10T09:01:00.000Z'
        )
      `);
      expect((await upgraded.query(`
        select object_key, byte_size, upload_status
        from public.media_objects
        where id = '00000000-0000-0000-0000-000000000301'
      `)).rows).toEqual([{
        byte_size: 0,
        object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000501',
        upload_status: 'pending',
      }]);

      await upgraded.exec(
        await readFile(
          `${migrationsDirectory}/202607100005_media_recovery.sql`,
          'utf8',
        ),
      );

      expect((await upgraded.query(`
        select object_key, byte_size, sha256, upload_status
        from public.media_objects
        where id = '00000000-0000-0000-0000-000000000301'
      `)).rows).toEqual([{
        byte_size: 1024,
        object_key: 'reports/2026-07-10/video.mp4',
        sha256: 'ready-sha256',
        upload_status: 'ready',
      }]);
      expect((await upgraded.query(`
        select object_key, status
        from public.media_uploads
        where id = '00000000-0000-0000-0000-000000000501'
      `)).rows).toEqual([{
        object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000501',
        status: 'creating',
      }]);
    } finally {
      await upgraded.close();
    }
  }, 30_000);

  it('fails closed when upgrading a published report without a trusted snapshot', async () => {
    const upgraded = await createDatabaseWithMigrations([
      migrationFiles[0]!,
      migrationFiles[1]!,
    ]);
    try {
      await upgraded.exec(`
        insert into auth.users(id, email, email_confirmed_at) values
          (
            '00000000-0000-0000-0000-000000000001',
            'chen@example.com',
            '2026-07-10T07:00:00.000Z'
          ),
          (
            '00000000-0000-0000-0000-000000000003',
            'viewer@example.com',
            '2026-07-10T07:00:00.000Z'
          );
        insert into public.profiles(id, email, global_role) values
          (
            '00000000-0000-0000-0000-000000000001',
            'chen@example.com',
            'owner'
          ),
          (
            '00000000-0000-0000-0000-000000000003',
            'viewer@example.com',
            'viewer'
          );
        insert into public.reports(
          id,
          daily_date,
          source_table_id,
          status,
          draft_version,
          published_version,
          published_at
        ) values (
          '00000000-0000-0000-0000-000000000101',
          '2026-07-10',
          'source-table',
          'published',
          2,
          1,
          '2026-07-10T08:00:00.000Z'
        );
        insert into public.report_items(
          id,
          report_id,
          local_record_id,
          max_daily_card,
          version
        ) values (
          '00000000-0000-0000-0000-000000000201',
          '00000000-0000-0000-0000-000000000101',
          'local-1',
          'edited after the historical publish',
          2
        );
        insert into public.media_objects(
          id,
          report_item_id,
          object_key,
          content_type,
          byte_size,
          sha256,
          upload_status,
          uploaded_at
        ) values (
          '00000000-0000-0000-0000-000000000301',
          '00000000-0000-0000-0000-000000000201',
          'reports/2026-07-10/edited.mp4',
          'video/mp4',
          1024,
          'edited-sha256',
          'ready',
          '2026-07-10T08:59:00.000Z'
        );
        insert into public.report_members(report_id, user_id, role, created_by)
        values (
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000003',
          'viewer',
          '00000000-0000-0000-0000-000000000001'
        );
        insert into public.share_links(
          id,
          report_id,
          token_hash,
          created_by,
          created_at
        ) values (
          '00000000-0000-0000-0000-000000000401',
          '00000000-0000-0000-0000-000000000101',
          'pre-migration-share',
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T08:00:00.000Z'
        );
      `);

      await upgraded.exec(
        await readFile(`${migrationsDirectory}/${migrationFiles[2]}`, 'utf8'),
      );

      const state = await upgraded.query<{
        publication_count: number;
        published_at: Date | null;
        published_version: number;
        status: string;
      }>(`
        select
          r.status,
          r.published_version,
          r.published_at,
          (
            select count(*)::integer
            from public.report_publications rp
            where rp.report_id = r.id
          ) as publication_count
        from public.reports r
        where r.id = '00000000-0000-0000-0000-000000000101'
      `);
      expect(state.rows).toEqual([{
        publication_count: 0,
        published_at: null,
        published_version: 1,
        status: 'draft',
      }]);

      await expect(upgraded.query(`
        select public.read_report(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000003',
          null,
          '2026-07-10T09:00:00.000Z'
        )
      `)).rejects.toMatchObject({ message: 'report_not_found' });
      await expect(upgraded.query(`
        select public.read_report(
          '00000000-0000-0000-0000-000000000101',
          null,
          '00000000-0000-0000-0000-000000000401',
          '2026-07-10T09:00:00.000Z'
        )
      `)).rejects.toMatchObject({ message: 'share_unavailable' });

      const republished = await upgraded.query<{ published_version: number }>(`
        select published_version
        from public.publish_report(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000001',
          '2026-07-10T09:01:00.000Z'
        )
      `);
      expect(republished.rows).toEqual([{ published_version: 2 }]);

      const viewerRead = await upgraded.query<{ report: {
        items: Array<{ max_daily_card: string }>;
        published_version: number;
      } }>(`
        select public.read_report(
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000003',
          null,
          '2026-07-10T09:02:00.000Z'
        ) as report
      `);
      expect(viewerRead.rows[0]?.report).toMatchObject({
        items: [{ max_daily_card: 'edited after the historical publish' }],
        published_version: 2,
      });
    } finally {
      await upgraded.close();
    }
  }, 30_000);

  it('creates the complete Task 2 schema', async () => {
    const result = await db.query<{ tablename: string }>(`
      select tablename
      from pg_tables
      where schemaname = 'public'
        and tablename = any($1::text[])
      order by tablename
    `, [expectedTables]);

    expect(result.rows.map(({ tablename }) => tablename)).toEqual(expectedTables);
  });

  it('enforces one global owner', async () => {
    await db.exec(`
      insert into auth.users(id, email) values
        ('00000000-0000-0000-0000-000000000001', 'one@example.com'),
        ('00000000-0000-0000-0000-000000000002', 'two@example.com');
      insert into public.profiles(id, email, global_role)
      values ('00000000-0000-0000-0000-000000000001', 'one@example.com', 'owner');
    `);

    await expect(db.exec(`
      insert into public.profiles(id, email, global_role)
      values ('00000000-0000-0000-0000-000000000002', 'two@example.com', 'owner');
    `)).rejects.toMatchObject({ code: '23505' });
  });

  it('defines the authorization function signatures', async () => {
    const result = await db.query<{
      function_name: string;
      arguments: string;
      result_type: string;
    }>(`
      select
        p.proname as function_name,
        pg_get_function_identity_arguments(p.oid) as arguments,
        pg_get_function_result(p.oid) as result_type
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'public'
        and p.proname = any($1::text[])
      order by p.proname
    `, [[
      'can_edit_item',
      'claim_invitation',
      'claim_owner',
      'current_role',
      'publish_report',
      'read_report',
      'restore_revision',
      'update_collaborative_field',
    ]]);

    expect(result.rows).toEqual([
      { function_name: 'can_edit_item', arguments: 'item_id uuid', result_type: 'boolean' },
      {
        function_name: 'claim_invitation',
        arguments: 'p_token_hash text, p_claimant_id uuid, p_claimant_email text, p_claimed_at timestamp with time zone',
        result_type: 'TABLE(report_id uuid, member_role app_role)',
      },
      {
        function_name: 'claim_owner',
        arguments: 'claimant_id uuid, claimant_email text',
        result_type: 'profiles',
      },
      { function_name: 'current_role', arguments: 'report_id uuid', result_type: 'app_role' },
      {
        function_name: 'publish_report',
        arguments: 'p_report_id uuid, p_actor_id uuid, p_published_at timestamp with time zone',
        result_type: 'reports',
      },
      {
        function_name: 'read_report',
        arguments: 'p_report_id uuid, p_actor_id uuid, p_share_link_id uuid, p_read_at timestamp with time zone',
        result_type: 'jsonb',
      },
      {
        function_name: 'restore_revision',
        arguments: 'p_revision_id bigint, p_expected_version integer, p_actor_id uuid, p_restored_at timestamp with time zone',
        result_type: 'TABLE(id uuid, report_id uuid, local_record_id text, title text, caption text, source_url text, max_daily_card text, max_feedback text, review_status text, item_order integer, version integer, media_id uuid)',
      },
      {
        function_name: 'update_collaborative_field',
        arguments: 'p_item_id uuid, p_field_name text, p_value text, p_expected_version integer, p_actor_id uuid, p_changed_at timestamp with time zone',
        result_type: 'TABLE(id uuid, report_id uuid, local_record_id text, title text, caption text, source_url text, max_daily_card text, max_feedback text, review_status text, item_order integer, version integer, media_id uuid)',
      },
    ]);
  });

  it('enables RLS on every business table', async () => {
    const result = await db.query<{ relname: string; relrowsecurity: boolean }>(`
      select c.relname, c.relrowsecurity
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public'
        and c.relkind = 'r'
        and c.relname = any($1::text[])
      order by c.relname
    `, [expectedTables]);

    expect(result.rows).toHaveLength(expectedTables.length);
    expect(result.rows.every(({ relrowsecurity }) => relrowsecurity)).toBe(true);
  });

  it('keeps privileged writes and owner bootstrap behind service boundaries', async () => {
    const result = await db.query<{ allowed: boolean }>(`
      select
        not has_table_privilege('authenticated', 'public.report_items', 'UPDATE')
        and not has_table_privilege('authenticated', 'public.report_members', 'INSERT,UPDATE,DELETE')
        and not has_table_privilege('authenticated', 'public.invitations', 'INSERT,UPDATE,DELETE')
        and not has_table_privilege('authenticated', 'public.share_links', 'INSERT,UPDATE,DELETE')
        and not has_function_privilege(
          'authenticated',
          'public.claim_owner(uuid,text)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.claim_owner(uuid,text)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.claim_invitation(text,uuid,text,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.claim_invitation(text,uuid,text,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.update_collaborative_field(uuid,text,text,integer,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.update_collaborative_field(uuid,text,text,integer,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.publish_report(uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.publish_report(uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.read_report(uuid,uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.read_report(uuid,uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.restore_revision(bigint,integer,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.restore_revision(bigint,integer,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_table_privilege(
          'service_role',
          'public.report_publications',
          'SELECT'
        )
        and not has_table_privilege(
          'service_role',
          'public.report_publications',
          'INSERT,UPDATE,DELETE'
        )
        and has_table_privilege(
          'service_role',
          'public.report_publication_items',
          'SELECT'
        )
        and not has_table_privilege(
          'service_role',
          'public.report_publication_items',
          'INSERT,UPDATE,DELETE'
        )
        as allowed
    `);

    expect(result.rows).toEqual([{ allowed: true }]);
  });

  it('keeps media state private and exposes only service-role media RPCs', async () => {
    const result = await db.query<{ allowed: boolean }>(`
      select
        not has_table_privilege(
          'authenticated',
          'public.media_uploads',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and not has_table_privilege(
          'authenticated',
          'public.media_upload_parts',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and not has_table_privilege(
          'service_role',
          'public.media_uploads',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and not has_table_privilege(
          'service_role',
          'public.media_upload_parts',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.authorize_media_read(uuid,uuid,uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.authorize_media_read(uuid,uuid,uuid,uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.begin_media_upload(uuid,uuid,uuid,uuid,uuid,text,bigint,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.begin_media_upload(uuid,uuid,uuid,uuid,uuid,text,bigint,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.read_media_upload(uuid,uuid,uuid)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.read_media_upload(uuid,uuid,uuid)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.claim_media_upload_completion(uuid,uuid,uuid,text,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.claim_media_upload_completion(uuid,uuid,uuid,text,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.finalize_media_upload_completion(uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.finalize_media_upload_completion(uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.claim_media_upload_abort(uuid,uuid,uuid,timestamp with time zone,boolean)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.claim_media_upload_abort(uuid,uuid,uuid,timestamp with time zone,boolean)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.claim_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.claim_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.reset_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone,text)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.reset_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone,text)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.list_media_upload_recovery_parts(uuid)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.list_media_upload_recovery_parts(uuid)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.finalize_media_upload_abort(uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.finalize_media_upload_abort(uuid,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'authenticated',
          'public.list_media_upload_recovery_page(timestamp with time zone,uuid,integer)',
          'EXECUTE'
        )
        and has_function_privilege(
          'service_role',
          'public.list_media_upload_recovery_page(timestamp with time zone,uuid,integer)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'service_role',
          'public.complete_media_upload(uuid,uuid,uuid,text,timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'service_role',
          'public.abort_media_upload(uuid,uuid,uuid,timestamp with time zone,boolean)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'service_role',
          'public.list_stale_media_uploads(timestamp with time zone)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'service_role',
          'public.assert_media_uploader(uuid,uuid)',
          'EXECUTE'
        )
        and not has_function_privilege(
          'service_role',
          'public.media_upload_payload(uuid)',
          'EXECUTE'
        )
        as allowed
    `);

    expect(result.rows).toEqual([{ allowed: true }]);
  });

  it('bootstraps an owner only from a confirmed matching auth identity', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at)
      values (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        '2026-07-10T07:00:00.000Z'
      );
    `);

    const claimed = await db.query<{
      email: string;
      global_role: string;
      id: string;
    }>(`
      select id, email, global_role
      from public.claim_owner(
        '00000000-0000-0000-0000-000000000001',
        '  CHEN@example.COM '
      )
    `);

    expect(claimed.rows).toEqual([{
      email: 'chen@example.com',
      global_role: 'owner',
      id: '00000000-0000-0000-0000-000000000001',
    }]);
  });

  it('atomically consumes an invitation exactly once through the service RPC', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at) values
        (
          '00000000-0000-0000-0000-000000000001',
          'chen@example.com',
          '2026-07-10T07:00:00.000Z'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'max@example.com',
          '2026-07-10T07:00:00.000Z'
        );
      insert into public.reports(id, daily_date, source_table_id)
      values (
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10',
        'source-table'
      );
      insert into public.invitations(
        id,
        report_id,
        email,
        role,
        token_hash,
        expires_at,
        created_by
      ) values (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000101',
        'max@example.com',
        'editor',
        'atomic-token-hash',
        '2026-07-11T08:00:00.000Z',
        '00000000-0000-0000-0000-000000000001'
      );
    `);

    const first = await db.query<{ member_role: string; report_id: string }>(`
      select *
      from public.claim_invitation(
        'atomic-token-hash',
        '00000000-0000-0000-0000-000000000002',
        'max@example.com',
        '2026-07-10T08:00:00.000Z'
      )
    `);
    expect(first.rows).toEqual([{
      member_role: 'editor',
      report_id: '00000000-0000-0000-0000-000000000101',
    }]);

    await db.exec('savepoint repeated_claim');
    await expect(db.query(`
      select *
      from public.claim_invitation(
        'atomic-token-hash',
        '00000000-0000-0000-0000-000000000002',
        'max@example.com',
        '2026-07-10T08:00:00.000Z'
      )
    `)).rejects.toMatchObject({
      code: 'P0002',
      message: 'invitation_unavailable',
    });
    await db.exec('rollback to savepoint repeated_claim');

    const state = await db.query<{
      member_count: number;
      used_count: number;
    }>(`
      select
        (select count(*)::integer from public.report_members) as member_count,
        (
          select count(*)::integer
          from public.invitations
          where used_at is not null
        ) as used_count
    `);
    expect(state.rows).toEqual([{ member_count: 1, used_count: 1 }]);
  });

  it('atomically edits one collaborative field with report-specific access and history', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at) values
        (
          '00000000-0000-0000-0000-000000000001',
          'chen@example.com',
          '2026-07-10T07:00:00.000Z'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'max@example.com',
          '2026-07-10T07:00:00.000Z'
        );
      insert into public.profiles(id, email, global_role) values
        (
          '00000000-0000-0000-0000-000000000001',
          'chen@example.com',
          'owner'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'max@example.com',
          'viewer'
        );
      insert into public.reports(id, daily_date, source_table_id)
      values (
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10',
        'source-table'
      );
      insert into public.report_items(
        id,
        report_id,
        local_record_id,
        title,
        caption,
        max_daily_card
      ) values (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000101',
        'local-1',
        'source title',
        'source caption',
        'old card'
      );
      insert into public.report_members(report_id, user_id, role, created_by)
      values (
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000002',
        'editor',
        '00000000-0000-0000-0000-000000000001'
      );
    `);

    const updated = await db.query<{
      caption: string;
      max_daily_card: string;
      title: string;
      version: number;
    }>(`
      select caption, max_daily_card, title, version
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'new card',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    expect(updated.rows).toEqual([{
      caption: 'source caption',
      max_daily_card: 'new card',
      title: 'source title',
      version: 2,
    }]);

    const history = await db.query<{
      audit_count: number;
      draft_version: number;
      field_name: string;
      item_version: number;
      new_value: string;
      old_value: string;
    }>(`
      select
        r.draft_version,
        rv.field_name,
        rv.old_value,
        rv.new_value,
        rv.item_version,
        (
          select count(*)::integer
          from public.audit_logs al
          where al.event_type = 'report_item.updated'
        ) as audit_count
      from public.reports r
      cross join public.revisions rv
      where r.id = '00000000-0000-0000-0000-000000000101'
    `);
    expect(history.rows).toEqual([{
      audit_count: 1,
      draft_version: 2,
      field_name: 'max_daily_card',
      item_version: 2,
      new_value: 'new card',
      old_value: 'old card',
    }]);

    await db.exec('savepoint stale_edit');
    await expect(db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'late',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:01:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'stale_version' });
    await db.exec('rollback to savepoint stale_edit');
  });

  it('rejects an editor from a different report inside the trusted edit RPC', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at) values
        (
          '00000000-0000-0000-0000-000000000001',
          'chen@example.com',
          '2026-07-10T07:00:00.000Z'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'max@example.com',
          '2026-07-10T07:00:00.000Z'
        );
      insert into public.profiles(id, email, global_role) values
        (
          '00000000-0000-0000-0000-000000000001',
          'chen@example.com',
          'owner'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'max@example.com',
          'editor'
        );
      insert into public.reports(id, daily_date, source_table_id) values
        (
          '00000000-0000-0000-0000-000000000101',
          '2026-07-10',
          'source-a'
        ),
        (
          '00000000-0000-0000-0000-000000000102',
          '2026-07-09',
          'source-b'
        );
      insert into public.report_items(id, report_id, local_record_id)
      values (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000101',
        'local-1'
      );
      insert into public.report_members(report_id, user_id, role, created_by)
      values (
        '00000000-0000-0000-0000-000000000102',
        '00000000-0000-0000-0000-000000000002',
        'editor',
        '00000000-0000-0000-0000-000000000001'
      );
    `);

    await expect(db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'review_status',
        'approved',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'access_denied' });
  });

  it('publishes only complete ready media and records the owner action', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at)
      values (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        '2026-07-10T07:00:00.000Z'
      );
      insert into public.profiles(id, email, global_role)
      values (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        'owner'
      );
      insert into public.reports(id, daily_date, source_table_id)
      values (
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10',
        'source-table'
      );
      insert into public.report_items(id, report_id, local_record_id)
      values (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000101',
        'local-1'
      );
    `);

    await db.exec('savepoint incomplete_publish');
    await expect(db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'report_not_ready' });
    await db.exec('rollback to savepoint incomplete_publish');

    await db.exec(`
      insert into public.media_objects(
        report_item_id,
        object_key,
        content_type,
        byte_size,
        sha256,
        upload_status,
        uploaded_at
      ) values (
        '00000000-0000-0000-0000-000000000201',
        'reports/2026-07-10/video.mp4',
        'video/mp4',
        1024,
        'ready-sha256',
        'ready',
        '2026-07-10T08:59:00.000Z'
      );
    `);

    const published = await db.query<{
      published_at: Date;
      published_version: number;
      status: string;
    }>(`
      select status, published_version, published_at
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    expect(published.rows[0]).toMatchObject({
      published_version: 1,
      status: 'published',
    });
    expect(new Date(published.rows[0]?.published_at ?? 0).toISOString())
      .toBe('2026-07-10T09:00:00.000Z');

    const audits = await db.query<{ count: number }>(`
      select count(*)::integer as count
      from public.audit_logs
      where event_type = 'report.published'
    `);
    expect(audits.rows).toEqual([{ count: 1 }]);
  });

  it('restores an old collaborative value with a new revision and audit record', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at)
      values (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        '2026-07-10T07:00:00.000Z'
      );
      insert into public.profiles(id, email, global_role)
      values (
        '00000000-0000-0000-0000-000000000001',
        'chen@example.com',
        'owner'
      );
      insert into public.reports(id, daily_date, source_table_id, draft_version)
      values (
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10',
        'source-table',
        2
      );
      insert into public.report_items(
        id,
        report_id,
        local_record_id,
        max_daily_card,
        version
      ) values (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000101',
        'local-1',
        'new card',
        2
      );
    `);
    const insertedRevision = await db.query<{ id: number }>(`
      insert into public.revisions(
        report_item_id,
        field_name,
        old_value,
        new_value,
        item_version,
        actor_id,
        created_at
      ) values (
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'old card',
        'new card',
        2,
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T08:00:00.000Z'
      )
      returning id
    `);
    const revisionId = insertedRevision.rows[0]?.id;
    expect(revisionId).toBeTypeOf('number');

    const restored = await db.query<{
      max_daily_card: string;
      version: number;
    }>(`
      select max_daily_card, version
      from public.restore_revision(
        ${revisionId},
        2,
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    expect(restored.rows).toEqual([{
      max_daily_card: 'old card',
      version: 3,
    }]);

    const history = await db.query<{
      audit_count: number;
      draft_version: number;
      revision_count: number;
    }>(`
      select
        r.draft_version,
        (select count(*)::integer from public.revisions) as revision_count,
        (
          select count(*)::integer
          from public.audit_logs
          where event_type = 'revision.restored'
        ) as audit_count
      from public.reports r
      where r.id = '00000000-0000-0000-0000-000000000101'
    `);
    expect(history.rows).toEqual([{
      audit_count: 1,
      draft_version: 3,
      revision_count: 2,
    }]);
  });

  it('updates current publication collaboration fields before republish switches versions', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'draft card',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    await db.exec(`
      update public.media_objects
      set object_key = 'reports/2026-07-10/video-v2.mp4',
          sha256 = 'ready-sha256-v2'
      where id = '00000000-0000-0000-0000-000000000301'
    `);

    const ownerRead = await db.query<{ report: {
      items: Array<{ max_daily_card: string; version: number }>;
      published_version: number;
    } }>(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        null,
        '2026-07-10T09:02:00.000Z'
      ) as report
    `);
    expect(ownerRead.rows[0]?.report).toMatchObject({
      published_version: 1,
      items: [{ max_daily_card: 'draft card', version: 2 }],
    });

    const viewerBefore = await db.query<{ report: {
      items: Array<{ max_daily_card: string; media_id: string; version: number }>;
      published_version: number;
    } }>(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000003',
        null,
        '2026-07-10T09:02:00.000Z'
      ) as report
    `);
    expect(viewerBefore.rows[0]?.report).toMatchObject({
      published_version: 1,
      items: [{
        max_daily_card: 'draft card',
        media_id: '00000000-0000-0000-0000-000000000301',
        version: 2,
      }],
    });

    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:03:00.000Z'
      )
    `);
    const viewerAfter = await db.query<{ report: {
      items: Array<{ max_daily_card: string; version: number }>;
      published_version: number;
    } }>(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000003',
        null,
        '2026-07-10T09:04:00.000Z'
      ) as report
    `);
    expect(viewerAfter.rows[0]?.report).toMatchObject({
      published_version: 2,
      items: [{ max_daily_card: 'draft card', version: 2 }],
    });

    const publications = await db.query<{ count: number }>(`
      select count(*)::integer as count
      from public.report_publications
      where report_id = '00000000-0000-0000-0000-000000000101'
    `);
    expect(publications.rows).toEqual([{ count: 2 }]);

    const mediaSnapshots = await db.query<{
      media_object_key: string;
      media_sha256: string;
      media_upload_status: string;
      published_version: number;
    }>(`
      select
        published_version,
        media_object_key,
        media_sha256,
        media_upload_status
      from public.report_publication_items
      where report_id = '00000000-0000-0000-0000-000000000101'
      order by published_version
    `);
    expect(mediaSnapshots.rows).toEqual([
      {
        media_object_key: 'reports/2026-07-10/video.mp4',
        media_sha256: 'ready-sha256',
        media_upload_status: 'ready',
        published_version: 1,
      },
      {
        media_object_key: 'reports/2026-07-10/video-v2.mp4',
        media_sha256: 'ready-sha256-v2',
        media_upload_status: 'ready',
        published_version: 2,
      },
    ]);
  });

  it('allocates a fresh version for a media-only republish', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      update public.media_objects
      set object_key = 'reports/2026-07-10/media-only-v2.mp4',
          sha256 = 'media-only-v2-sha256'
      where id = '00000000-0000-0000-0000-000000000301'
    `);

    const republished = await db.query<{
      draft_version: number;
      published_version: number;
    }>(`
      select draft_version, published_version
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    expect(republished.rows).toEqual([{
      draft_version: 1,
      published_version: 2,
    }]);

    const snapshots = await db.query<{
      media_object_key: string;
      published_version: number;
    }>(`
      select published_version, media_object_key
      from public.report_publication_items
      where report_id = '00000000-0000-0000-0000-000000000101'
      order by published_version
    `);
    expect(snapshots.rows).toEqual([
      {
        media_object_key: 'reports/2026-07-10/video.mp4',
        published_version: 1,
      },
      {
        media_object_key: 'reports/2026-07-10/media-only-v2.mp4',
        published_version: 2,
      },
    ]);
  });

  it('allocates a fresh version for a source-only republish', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      update public.report_items
      set title = 'source title v2'
      where id = '00000000-0000-0000-0000-000000000201'
    `);

    const republished = await db.query<{
      draft_version: number;
      published_version: number;
    }>(`
      select draft_version, published_version
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    expect(republished.rows).toEqual([{
      draft_version: 1,
      published_version: 2,
    }]);

    const snapshots = await db.query<{
      published_version: number;
      title: string;
    }>(`
      select published_version, title
      from public.report_publication_items
      where report_id = '00000000-0000-0000-0000-000000000101'
      order by published_version
    `);
    expect(snapshots.rows).toEqual([
      { published_version: 1, title: 'source title' },
      { published_version: 2, title: 'source title v2' },
    ]);
  });

  it('allocates a fresh version when an item is inserted', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      insert into public.report_items(
        id,
        report_id,
        local_record_id,
        title,
        item_order
      ) values (
        '00000000-0000-0000-0000-000000000202',
        '00000000-0000-0000-0000-000000000101',
        'local-2',
        'inserted title',
        20
      );
      insert into public.media_objects(
        id,
        report_item_id,
        object_key,
        content_type,
        byte_size,
        sha256,
        upload_status,
        uploaded_at
      ) values (
        '00000000-0000-0000-0000-000000000302',
        '00000000-0000-0000-0000-000000000202',
        'reports/2026-07-10/inserted.mp4',
        'video/mp4',
        2048,
        'inserted-sha256',
        'ready',
        '2026-07-10T09:00:30.000Z'
      );
    `);

    const republished = await db.query<{ published_version: number }>(`
      select published_version
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    expect(republished.rows).toEqual([{ published_version: 2 }]);

    const counts = await db.query<{
      item_count: number;
      published_version: number;
    }>(`
      select
        rp.published_version,
        count(rpi.report_item_id)::integer as item_count
      from public.report_publications rp
      left join public.report_publication_items rpi
        on rpi.report_id = rp.report_id
        and rpi.published_version = rp.published_version
      where rp.report_id = '00000000-0000-0000-0000-000000000101'
      group by rp.published_version
      order by rp.published_version
    `);
    expect(counts.rows).toEqual([
      { item_count: 1, published_version: 1 },
      { item_count: 2, published_version: 2 },
    ]);
  });

  it('allocates a fresh version when an item is deleted', async () => {
    await seedReadyReport();
    await db.exec(`
      insert into public.report_items(
        id,
        report_id,
        local_record_id,
        title,
        item_order
      ) values (
        '00000000-0000-0000-0000-000000000202',
        '00000000-0000-0000-0000-000000000101',
        'local-2',
        'delete me',
        20
      );
      insert into public.media_objects(
        id,
        report_item_id,
        object_key,
        content_type,
        byte_size,
        sha256,
        upload_status,
        uploaded_at
      ) values (
        '00000000-0000-0000-0000-000000000302',
        '00000000-0000-0000-0000-000000000202',
        'reports/2026-07-10/delete-me.mp4',
        'video/mp4',
        2048,
        'delete-me-sha256',
        'ready',
        '2026-07-10T08:59:30.000Z'
      );
    `);
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      delete from public.report_items
      where id = '00000000-0000-0000-0000-000000000202'
    `);

    const republished = await db.query<{ published_version: number }>(`
      select published_version
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    expect(republished.rows).toEqual([{ published_version: 2 }]);

    const counts = await db.query<{
      item_count: number;
      published_version: number;
    }>(`
      select
        rp.published_version,
        count(rpi.report_item_id)::integer as item_count
      from public.report_publications rp
      left join public.report_publication_items rpi
        on rpi.report_id = rp.report_id
        and rpi.published_version = rp.published_version
      where rp.report_id = '00000000-0000-0000-0000-000000000101'
      group by rp.published_version
      order by rp.published_version
    `);
    expect(counts.rows).toEqual([
      { item_count: 2, published_version: 1 },
      { item_count: 1, published_version: 2 },
    ]);
  });

  it('preserves the previous snapshot when republishing an unchanged draft', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);

    const republished = await db.query<{
      draft_version: number;
      published_version: number;
    }>(`
      select draft_version, published_version
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    expect(republished.rows).toEqual([{
      draft_version: 1,
      published_version: 2,
    }]);

    const publications = await db.query<{
      item_count: number;
      published_version: number;
    }>(`
      select
        rp.published_version,
        count(rpi.report_item_id)::integer as item_count
      from public.report_publications rp
      left join public.report_publication_items rpi
        on rpi.report_id = rp.report_id
        and rpi.published_version = rp.published_version
      where rp.report_id = '00000000-0000-0000-0000-000000000101'
      group by rp.published_version
      order by rp.published_version
    `);
    expect(publications.rows).toEqual([
      { item_count: 1, published_version: 1 },
      { item_count: 1, published_version: 2 },
    ]);
  });

  it('prevents authenticated viewers from bypassing snapshots through draft tables', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'draft-only card',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:01:00.000Z'
      )
    `);

    await db.exec(`
      select set_config(
        'request.jwt.claim.sub',
        '00000000-0000-0000-0000-000000000003',
        true
      );
      set local role authenticated;
    `);
    let directRows: Array<{ max_daily_card: string }> = [];
    try {
      directRows = (await db.query<{ max_daily_card: string }>(`
        select max_daily_card
        from public.report_items
        where report_id = '00000000-0000-0000-0000-000000000101'
      `)).rows;
    } finally {
      await db.exec('reset role');
    }

    expect(directRows).toEqual([]);
  });

  it('rolls back a failed republish and preserves the current published version', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'draft-only feedback',
        1,
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:01:00.000Z'
      )
    `);

    await db.exec('savepoint failed_republish');
    await db.exec(`
      create function public.fail_second_publish()
      returns trigger
      language plpgsql
      as $$
      begin
        if new.published_version = 2 then
          raise exception 'forced_publish_failure';
        end if;
        return new;
      end
      $$;
      create trigger fail_second_publish
      before update on public.reports
      for each row execute function public.fail_second_publish();
    `);
    await expect(db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'forced_publish_failure' });
    await db.exec('rollback to savepoint failed_republish');

    const state = await db.query<{
      publication_count: number;
      published_version: number;
    }>(`
      select
        r.published_version,
        (
          select count(*)::integer
          from public.report_publications rp
          where rp.report_id = r.id
        ) as publication_count
      from public.reports r
      where r.id = '00000000-0000-0000-0000-000000000101'
    `);
    expect(state.rows).toEqual([{
      publication_count: 1,
      published_version: 1,
    }]);

    const viewerRead = await db.query<{ report: {
      items: Array<{ max_feedback: string; version: number }>;
      published_version: number;
    } }>(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000003',
        null,
        '2026-07-10T09:03:00.000Z'
      ) as report
    `);
    expect(viewerRead.rows[0]?.report).toMatchObject({
      published_version: 1,
      items: [{ max_feedback: 'draft-only feedback', version: 2 }],
    });
  });

  it('locks the report items and media rows used to build a publication snapshot', async () => {
    const result = await db.query<{ definition: string }>(`
      select pg_get_functiondef(
        'public.publish_report(uuid,uuid,timestamp with time zone)'::regprocedure
      ) as definition
    `);
    const definition = result.rows[0]?.definition.toLowerCase() ?? '';

    expect(definition.match(/for update/g)?.length ?? 0).toBeGreaterThanOrEqual(3);
    expect(definition).toContain('public.report_items');
    expect(definition).toContain('public.media_objects');
    expect(definition).toContain('public.report_publication_items');
  });

  it('atomically denies revoked membership, revoked shares, and withdrawn reports', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      insert into public.share_links(
        id,
        report_id,
        token_hash,
        created_by,
        created_at
      ) values (
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        'public-share-hash',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      );
    `);

    expect((await db.query(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        null,
        '00000000-0000-0000-0000-000000000401',
        '2026-07-10T09:01:00.000Z'
      ) as report
    `)).rows).toHaveLength(1);

    await db.exec('savepoint revoked_membership');
    await db.exec(`
      delete from public.report_members
      where report_id = '00000000-0000-0000-0000-000000000101'
        and user_id = '00000000-0000-0000-0000-000000000003'
    `);
    await expect(db.query(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000003',
        null,
        '2026-07-10T09:01:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'report_not_found' });
    await db.exec('rollback to savepoint revoked_membership');

    await db.exec('savepoint revoked_share');
    await db.exec(`
      update public.share_links
      set revoked_at = '2026-07-10T09:01:00.000Z'
      where id = '00000000-0000-0000-0000-000000000401'
    `);
    await expect(db.query(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        null,
        '00000000-0000-0000-0000-000000000401',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'share_unavailable' });
    await db.exec('rollback to savepoint revoked_share');

    await db.exec('savepoint withdrawn_report');
    await db.exec(`
      update public.reports
      set status = 'withdrawn'
      where id = '00000000-0000-0000-0000-000000000101'
    `);
    await expect(db.query(`
      select public.read_report(
        '00000000-0000-0000-0000-000000000101',
        null,
        '00000000-0000-0000-0000-000000000401',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'share_unavailable' });
    await db.exec('rollback to savepoint withdrawn_report');
  });

  it('conceals draft items from viewers but returns read-only denial for published items', async () => {
    await seedReadyReport();

    await db.exec('savepoint hidden_draft');
    await expect(db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'review_status',
        'approved',
        1,
        '00000000-0000-0000-0000-000000000003',
        '2026-07-10T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'access_denied' });
    await db.exec('rollback to savepoint hidden_draft');

    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:01:00.000Z'
      )
    `);
    await db.exec('savepoint known_published_item');
    await expect(db.query(`
      select *
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'review_status',
        'approved',
        1,
        '00000000-0000-0000-0000-000000000003',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'edit_forbidden' });
    await db.exec('rollback to savepoint known_published_item');
  });

  it('authorizes media atomically against draft roles and immutable publications', async () => {
    await seedReadyReport();
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.exec(`
      update public.media_objects
      set object_key = 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/draft',
          content_type = 'video/webm',
          byte_size = 2048,
          sha256 = 'draft-sha256',
          upload_status = 'ready',
          uploaded_at = '2026-07-10T09:01:00.000Z'
      where id = '00000000-0000-0000-0000-000000000301';
      insert into public.reports(id, daily_date, source_table_id)
      values (
        '00000000-0000-0000-0000-000000000102',
        '2026-07-09',
        'other-source-table'
      );
      insert into public.report_items(
        id,
        report_id,
        local_record_id
      ) values (
        '00000000-0000-0000-0000-000000000203',
        '00000000-0000-0000-0000-000000000102',
        'other-local-record'
      );
      insert into public.media_objects(
        id,
        report_item_id,
        object_key,
        content_type,
        byte_size,
        sha256,
        upload_status,
        uploaded_at
      ) values (
        '00000000-0000-0000-0000-000000000303',
        '00000000-0000-0000-0000-000000000203',
        'reports/other/video.mp4',
        'video/mp4',
        512,
        'other-sha256',
        'ready',
        '2026-07-10T09:01:00.000Z'
      );
      insert into public.share_links(
        id,
        report_id,
        token_hash,
        created_by,
        created_at
      ) values (
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        'media-share-hash',
        '00000000-0000-0000-0000-000000000001',
        '2026-07-10T09:00:00.000Z'
      );
    `);

    const viewer = await db.query<{
      byte_size: number;
      content_type: string;
      daily_date: Date;
      object_key: string;
    }>(`
      select object_key, content_type, byte_size, daily_date
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        '00000000-0000-0000-0000-000000000003',
        null,
        null,
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(viewer.rows).toEqual([{
      byte_size: 1024,
      content_type: 'video/mp4',
      daily_date: new Date('2026-07-10T00:00:00.000Z'),
      object_key: 'reports/2026-07-10/video.mp4',
    }]);

    const owner = await db.query<{
      byte_size: number;
      content_type: string;
      object_key: string;
    }>(`
      select object_key, content_type, byte_size
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        '00000000-0000-0000-0000-000000000001',
        null,
        null,
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(owner.rows).toEqual([{
      byte_size: 2048,
      content_type: 'video/webm',
      object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/draft',
    }]);

    const publicRead = await db.query<{ object_key: string }>(`
      select object_key
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        null,
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(publicRead.rows).toEqual([{
      object_key: 'reports/2026-07-10/video.mp4',
    }]);

    for (const mediaId of [
      '00000000-0000-0000-0000-000000000303',
      '00000000-0000-0000-0000-000000000399',
    ]) {
      await db.exec('savepoint concealed_media');
      await expect(db.query(`
        select *
        from public.authorize_media_read(
          '${mediaId}',
          '00000000-0000-0000-0000-000000000003',
          null,
          null,
          '2026-07-10T09:02:00.000Z'
        )
      `)).rejects.toMatchObject({ message: 'media_not_found' });
      await db.exec('rollback to savepoint concealed_media');
    }

    await db.exec('savepoint revoked_media_membership');
    await db.exec(`
      delete from public.report_members
      where report_id = '00000000-0000-0000-0000-000000000101'
        and user_id = '00000000-0000-0000-0000-000000000003'
    `);
    await expect(db.query(`
      select *
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        '00000000-0000-0000-0000-000000000003',
        null,
        null,
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'media_not_found' });
    await db.exec('rollback to savepoint revoked_media_membership');

    await db.exec('savepoint wrong_public_report');
    await expect(db.query(`
      select *
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        null,
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000102',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'media_not_found' });
    await db.exec('rollback to savepoint wrong_public_report');

    await db.exec('savepoint withdrawn_viewer_media');
    await db.exec(`
      update public.reports
      set status = 'withdrawn'
      where id = '00000000-0000-0000-0000-000000000101'
    `);
    await expect(db.query(`
      select *
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        '00000000-0000-0000-0000-000000000003',
        null,
        null,
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'media_not_found' });
    await db.exec('rollback to savepoint withdrawn_viewer_media');

    await db.exec('savepoint withdrawn_public_media');
    await db.exec(`
      update public.reports
      set status = 'withdrawn'
      where id = '00000000-0000-0000-0000-000000000101'
    `);
    await expect(db.query(`
      select *
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        null,
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:02:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'share_unavailable' });
    await db.exec('rollback to savepoint withdrawn_public_media');

    await db.exec(`
      update public.share_links
      set revoked_at = '2026-07-10T09:03:00.000Z'
      where id = '00000000-0000-0000-0000-000000000401'
    `);
    await expect(db.query(`
      select *
      from public.authorize_media_read(
        '00000000-0000-0000-0000-000000000301',
        null,
        '00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:04:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'share_unavailable' });
  });

  it('keeps replacement candidates isolated and gives complete or abort one DB owner', async () => {
    await seedReadyReport();

    await db.exec('savepoint editor_upload');
    await expect(db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000002',
        null,
        'video/mp4',
        8,
        '2026-07-10T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'owner_required' });
    await db.exec('rollback to savepoint editor_upload');

    await db.exec('savepoint cross_report_upload');
    await expect(db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000102',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        8,
        '2026-07-10T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'report_item_not_found' });
    await db.exec('rollback to savepoint cross_report_upload');

    const begun = await db.query<{ upload: {
      object_key: string;
      resumed: boolean;
      status: string;
    } }>(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        8,
        '2026-07-10T09:00:00.000Z'
      ) as upload
    `);
    expect(begun.rows[0]?.upload).toMatchObject({
      object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000501',
      resumed: false,
      status: 'creating',
    });
    expect((await db.query(`
      select object_key, byte_size, sha256, upload_status
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      byte_size: 1024,
      object_key: 'reports/2026-07-10/video.mp4',
      sha256: 'ready-sha256',
      upload_status: 'ready',
    }]);

    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        'r2-upload-1',
        '2026-07-10T09:00:01.000Z'
      )
    `);
    expect((await db.query(`
      select object_key, byte_size, sha256, upload_status
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      byte_size: 1024,
      object_key: 'reports/2026-07-10/video.mp4',
      sha256: 'ready-sha256',
      upload_status: 'ready',
    }]);
    await db.query(`
      select *
      from public.record_media_upload_part(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        1,
        'etag-1',
        8,
        '2026-07-10T09:00:02.000Z'
      )
    `);

    const persisted = await db.query<{ upload: {
      parts: Array<{ byte_size: number; part_number: number }>;
      status: string;
    } }>(`
      select public.read_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null
      ) as upload
    `);
    expect(persisted.rows[0]?.upload).toMatchObject({
      parts: [{ byte_size: 8, part_number: 1 }],
      status: 'uploading',
    });

    const completing = await db.query<{ upload: {
      asserted_sha256: string;
      sha256_source: string;
      sha256_verification_status: string;
      status: string;
    } }>(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        '${'a'.repeat(64)}',
        '2026-07-10T09:00:03.000Z'
      ) as upload
    `);
    expect(completing.rows[0]?.upload).toMatchObject({
      asserted_sha256: 'a'.repeat(64),
      sha256_source: 'trusted_uploader_assertion',
      sha256_verification_status: 'not_server_verified',
      status: 'completing',
    });

    await db.exec('savepoint abort_loses_completion');
    await expect(db.query(`
      select public.claim_media_upload_abort(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        '2026-07-10T09:00:04.000Z',
        false
      )
    `)).rejects.toMatchObject({ message: 'upload_completing' });
    await db.exec('rollback to savepoint abort_loses_completion');

    await db.query(`
      select *
      from public.finalize_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:00:05.000Z'
      )
    `);
    await db.query(`
      select *
      from public.finalize_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:00:06.000Z'
      )
    `);
    expect((await db.query(`
      select
        object_key,
        byte_size,
        sha256,
        sha256_source,
        sha256_verification_status,
        upload_status
      from public.media_objects
      where id = '00000000-0000-0000-0000-000000000301'
    `)).rows).toEqual([{
      byte_size: 8,
      object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000501',
      sha256: 'a'.repeat(64),
      sha256_source: 'trusted_uploader_assertion',
      sha256_verification_status: 'not_server_verified',
      upload_status: 'ready',
    }]);

    await db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        4,
        '2026-07-10T10:00:00.000Z'
      )
    `);
    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000001',
        null,
        'r2-upload-2',
        '2026-07-10T10:00:01.000Z'
      )
    `);
    const stale = await db.query<{ id: string }>(`
      select id
      from public.list_media_upload_recovery_page(
        '2026-07-11T10:00:00.000Z',
        null,
        100
      )
    `);
    expect(stale.rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000502',
    }]);

    await db.query(`
      select public.claim_media_upload_abort(
        '00000000-0000-0000-0000-000000000502',
        null,
        null,
        '2026-07-11T10:00:00.000Z',
        true
      )
    `);
    await db.exec('savepoint completion_loses_abort');
    await expect(db.query(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000001',
        null,
        '${'b'.repeat(64)}',
        '2026-07-11T10:00:01.000Z'
      )
    `)).rejects.toMatchObject({ message: 'upload_aborting' });
    await db.exec('rollback to savepoint completion_loses_abort');
    await db.query(`
      select public.finalize_media_upload_abort(
        '00000000-0000-0000-0000-000000000502',
        '2026-07-11T10:00:02.000Z'
      )
    `);
    await db.query(`
      select public.finalize_media_upload_abort(
        '00000000-0000-0000-0000-000000000502',
        '2026-07-11T10:00:03.000Z'
      )
    `);
    const aborted = await db.query<{
      byte_size: number;
      object_key: string;
      status: string;
      upload_status: string;
    }>(`
      select mu.status, mo.object_key, mo.byte_size, mo.upload_status
      from public.media_uploads mu
      join public.media_objects mo on mo.id = mu.media_id
      where mu.id = '00000000-0000-0000-0000-000000000502'
    `);
    expect(aborted.rows).toEqual([{
      byte_size: 8,
      object_key: 'reports/00000000-0000-0000-0000-000000000101/media/00000000-0000-0000-0000-000000000301/00000000-0000-0000-0000-000000000501',
      status: 'aborted',
      upload_status: 'ready',
    }]);
  });

  it('leases and safely releases an interrupted media completion', async () => {
    await seedReadyReport();
    await db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        8,
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        'r2-upload-1',
        '2026-07-10T09:00:01.000Z'
      )
    `);
    await db.query(`
      select *
      from public.record_media_upload_part(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        1,
        'etag-1',
        8,
        '2026-07-10T09:00:02.000Z'
      )
    `);
    await db.query(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        '${'a'.repeat(64)}',
        '2026-07-10T09:00:03.000Z'
      )
    `);

    const leased = await db.query<{ upload: {
      status: string;
      transition_started_at: string;
    } }>(`
      select public.claim_stale_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:10:00.000Z',
        '2026-07-10T09:05:00.000Z'
      ) as upload
    `);
    expect(leased.rows[0]?.upload.status).toBe('completing');
    expect(
      new Date(
        leased.rows[0]?.upload.transition_started_at ?? '',
      ).toISOString(),
    ).toBe('2026-07-10T09:10:00.000Z');
    const recoveryPage = await db.query<{ id: string }>(`
      select id
      from public.list_media_upload_recovery_page(
        '2026-07-10T09:00:04.000Z',
        null,
        100
      )
    `);
    expect(recoveryPage.rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000501',
    }]);
    expect((await db.query(`
      select part_number, etag, byte_size
      from public.list_media_upload_recovery_parts(
        '00000000-0000-0000-0000-000000000501'
      )
    `)).rows).toEqual([{
      byte_size: 8,
      etag: 'etag-1',
      part_number: 1,
    }]);

    const renewed = await db.query<{ upload: {
      transition_started_at: string;
    } }>(`
      select public.claim_stale_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:20:00.000Z',
        '2026-07-10T09:15:00.000Z'
      ) as upload
    `);
    expect(
      new Date(
        renewed.rows[0]?.upload.transition_started_at ?? '',
      ).toISOString(),
    ).toBe('2026-07-10T09:20:00.000Z');

    await db.exec('savepoint stale_completion_reset');
    await expect(db.query(`
      select public.reset_stale_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:20:01.000Z',
        '2026-07-10T09:10:00.000Z',
        'r2_upload_missing'
      )
    `)).rejects.toMatchObject({ message: 'upload_not_found' });
    await db.exec('rollback to savepoint stale_completion_reset');

    const reset = await db.query<{ upload: { status: string } }>(`
      select public.reset_stale_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '2026-07-10T09:20:01.000Z',
        '2026-07-10T09:20:00.000Z',
        'r2_upload_missing'
      ) as upload
    `);
    expect(reset.rows[0]?.upload).toMatchObject({ status: 'aborted' });

    const replacement = await db.query<{ upload: {
      resumed: boolean;
      status: string;
    } }>(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000502',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        8,
        '2026-07-10T09:10:02.000Z'
      ) as upload
    `);
    expect(replacement.rows[0]?.upload).toMatchObject({
      resumed: false,
      status: 'creating',
    });
  });

  it('rejects expired and inconsistent persisted multipart state', async () => {
    await seedReadyReport();
    const fiveMiB = 5 * 1024 ** 2;
    const sixMiB = 6 * 1024 ** 2;

    await db.query(`
      select public.begin_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000001',
        null,
        'video/mp4',
        ${fiveMiB + sixMiB + 1},
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await db.query(`
      select public.attach_media_upload(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        'r2-upload-1',
        '2026-07-10T09:00:01.000Z'
      )
    `);

    await db.exec('savepoint expired_part');
    await expect(db.query(`
      select *
      from public.record_media_upload_part(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        1,
        'expired-etag',
        ${fiveMiB},
        '2026-07-11T09:00:00.000Z'
      )
    `)).rejects.toMatchObject({ message: 'upload_expired' });
    await db.exec('rollback to savepoint expired_part');

    for (const [partNumber, etag, byteSize] of [
      [1, 'etag-1', fiveMiB],
      [2, 'etag-2', sixMiB],
      [3, 'etag-3', 1],
    ] as const) {
      await db.query(`
        select *
        from public.record_media_upload_part(
          '00000000-0000-0000-0000-000000000501',
          '00000000-0000-0000-0000-000000000001',
          null,
          ${partNumber},
          '${etag}',
          ${byteSize},
          '2026-07-10T09:00:02.000Z'
        )
      `);
    }

    await db.exec('savepoint inconsistent_parts');
    await expect(db.query(`
      select public.claim_media_upload_completion(
        '00000000-0000-0000-0000-000000000501',
        '00000000-0000-0000-0000-000000000001',
        null,
        '${'a'.repeat(64)}',
        '2026-07-10T09:00:03.000Z'
      )
    `)).rejects.toMatchObject({ message: 'upload_incomplete' });
    await db.exec('rollback to savepoint inconsistent_parts');

    const state = await db.query<{ status: string; upload_status: string }>(`
      select mu.status, mo.upload_status
      from public.media_uploads mu
      join public.media_objects mo on mo.id = mu.media_id
      where mu.id = '00000000-0000-0000-0000-000000000501'
    `);
    expect(state.rows).toEqual([{
      status: 'uploading',
      upload_status: 'ready',
    }]);
  });

  it('stores only unique token hashes behind RLS and one active-link constraint', async () => {
    const columns = await db.query<{
      column_name: string;
      is_nullable: string;
    }>(`
      select column_name, is_nullable
      from information_schema.columns
      where table_schema = 'public'
        and table_name = 'portal_collaboration_links'
      order by ordinal_position
    `);
    expect(columns.rows).toEqual([
      { column_name: 'id', is_nullable: 'NO' },
      { column_name: 'token_hash', is_nullable: 'NO' },
      { column_name: 'session_version', is_nullable: 'NO' },
      { column_name: 'created_by', is_nullable: 'NO' },
      { column_name: 'created_at', is_nullable: 'NO' },
      { column_name: 'last_used_at', is_nullable: 'YES' },
      { column_name: 'revoked_at', is_nullable: 'YES' },
    ]);
    expect(columns.rows.some(({ column_name }) => (
      column_name === 'token' || column_name === 'raw_token'
    ))).toBe(false);

    const safety = await db.query<{
      active_index: string;
      created_by_fk: string;
      rls_enabled: boolean;
      session_check: string;
      token_unique: string;
    }>(`
      select
        (
          select indexdef
          from pg_indexes
          where schemaname = 'public'
            and indexname = 'one_active_portal_collaboration_link'
        ) as active_index,
        (
          select pg_get_constraintdef(c.oid)
          from pg_constraint c
          where c.conrelid = 'public.portal_collaboration_links'::regclass
            and c.contype = 'f'
        ) as created_by_fk,
        (
          select relrowsecurity
          from pg_class
          where oid = 'public.portal_collaboration_links'::regclass
        ) as rls_enabled,
        (
          select pg_get_constraintdef(c.oid)
          from pg_constraint c
          where c.conrelid = 'public.portal_collaboration_links'::regclass
            and c.contype = 'c'
        ) as session_check,
        (
          select pg_get_constraintdef(c.oid)
          from pg_constraint c
          where c.conrelid = 'public.portal_collaboration_links'::regclass
            and c.contype = 'u'
        ) as token_unique
    `);
    expect(safety.rows[0]?.active_index).toMatch(
      /UNIQUE.*\(\(revoked_at IS NULL\)\).*WHERE \(revoked_at IS NULL\)/i,
    );
    expect(safety.rows[0]?.created_by_fk).toMatch(
      /FOREIGN KEY \(created_by\) REFERENCES auth\.users\(id\) ON DELETE RESTRICT/i,
    );
    expect(safety.rows[0]?.rls_enabled).toBe(true);
    expect(safety.rows[0]?.session_check).toMatch(/session_version > 0/i);
    expect(safety.rows[0]?.token_unique).toMatch(/UNIQUE \(token_hash\)/i);
  });

  it('exposes fixed collaboration RPCs only to service_role with static definer paths', async () => {
    const signatures = [
      'public.authorize_media_for_collaboration(uuid,uuid,timestamp with time zone)',
      'public.list_published_reports_for_collaboration(uuid,timestamp with time zone)',
      'public.list_published_reports_for_owner(uuid,timestamp with time zone)',
      'public.read_report_for_collaboration(uuid,uuid,timestamp with time zone)',
      'public.replace_active_portal_collaboration_link(uuid,text,uuid,timestamp with time zone)',
      'public.revoke_portal_collaboration_link(uuid,timestamp with time zone)',
      'public.touch_portal_collaboration_link(uuid,integer,timestamp with time zone)',
      'public.update_collaborative_field_for_link(uuid,uuid,text,text,integer,timestamp with time zone)',
    ];

    for (const signature of signatures) {
      const result = await db.query<{
        authenticated_execute: boolean;
        definition: string;
        security_definer: boolean;
        public_execute: boolean;
        runtime_config: string[];
        service_execute: boolean;
      }>(`
        select
          p.prosecdef as security_definer,
          p.proconfig as runtime_config,
          pg_get_functiondef(p.oid) as definition,
          has_function_privilege('public', p.oid, 'EXECUTE') as public_execute,
          has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated_execute,
          has_function_privilege('service_role', p.oid, 'EXECUTE') as service_execute
        from pg_proc p
        where p.oid = '${signature}'::regprocedure
      `);
      expect(result.rows).toHaveLength(1);
      expect(result.rows[0]).toMatchObject({
        authenticated_execute: false,
        public_execute: false,
        runtime_config: ['search_path=pg_catalog, public'],
        security_definer: true,
        service_execute: true,
      });
      expect(result.rows[0]?.definition).not.toMatch(/set_config|current_setting/i);
    }

    const tablePrivileges = await db.query<{ allowed: boolean }>(`
      select
        not has_table_privilege(
          'anon',
          'public.portal_collaboration_links',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and not has_table_privilege(
          'authenticated',
          'public.portal_collaboration_links',
          'SELECT,INSERT,UPDATE,DELETE'
        )
        and has_table_privilege(
          'service_role',
          'public.portal_collaboration_links',
          'SELECT'
        )
        and not has_table_privilege(
          'service_role',
          'public.portal_collaboration_links',
          'INSERT,UPDATE,DELETE'
        ) as allowed
    `);
    expect(tablePrivileges.rows).toEqual([{ allowed: true }]);

    const updateDefinition = await db.query<{ definition: string }>(`
      select pg_get_functiondef(
        'public.update_collaborative_field_for_link(uuid,uuid,text,text,integer,timestamp with time zone)'::regprocedure
      ) as definition
    `);
    expect(updateDefinition.rows[0]?.definition).toMatch(/FOR UPDATE/i);
    expect(updateDefinition.rows[0]?.definition).toMatch(
      /UPDATE public\.report_publication_items/i,
    );
  });

  it('atomically replaces, touches, and revokes the one active collaboration link', async () => {
    await db.exec(`
      insert into auth.users(id, email, email_confirmed_at) values
        (
          '${ownerId}',
          'chen@example.com',
          '2026-07-10T07:00:00.000Z'
        ),
        (
          '00000000-0000-0000-0000-000000000002',
          'viewer@example.com',
          '2026-07-10T07:00:00.000Z'
        );
      insert into public.profiles(id, email, global_role) values
        ('${ownerId}', 'chen@example.com', 'owner'),
        (
          '00000000-0000-0000-0000-000000000002',
          'viewer@example.com',
          'viewer'
        );
    `);

    await db.query(`
      select *
      from public.replace_active_portal_collaboration_link(
        '${collaborationLinkId}',
        '${'a'.repeat(64)}',
        '${ownerId}',
        '2026-07-10T09:00:00.000Z'
      )
    `);
    await expectDatabaseError(
      'non_owner_replace',
      `select * from public.replace_active_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        '${'b'.repeat(64)}',
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:01:00.000Z'
      )`,
      { code: '42501', message: 'owner_required' },
    );

    await db.query(`
      select *
      from public.replace_active_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        '${'b'.repeat(64)}',
        '${ownerId}',
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect((await db.query(`
      select id, session_version, revoked_at
      from public.portal_collaboration_links
      order by created_at
    `)).rows).toEqual([
      {
        id: collaborationLinkId,
        revoked_at: new Date('2026-07-10T09:02:00.000Z'),
        session_version: 2,
      },
      {
        id: '00000000-0000-0000-0000-000000000702',
        revoked_at: null,
        session_version: 1,
      },
    ]);

    await expectDatabaseError(
      'replace_rolls_back',
      `select * from public.replace_active_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000703',
        '${'a'.repeat(64)}',
        '${ownerId}',
        '2026-07-10T09:03:00.000Z'
      )`,
      { code: '23505', message: 'collaboration_link_conflict' },
    );
    expect((await db.query(`
      select id, session_version
      from public.portal_collaboration_links
      where revoked_at is null
    `)).rows).toEqual([{
      id: '00000000-0000-0000-0000-000000000702',
      session_version: 1,
    }]);

    expect((await db.query(`
      select public.touch_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        1,
        '2026-07-10T09:04:00.000Z'
      ) as touched
    `)).rows).toEqual([{ touched: true }]);
    expect((await db.query(`
      select public.touch_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        2,
        '2026-07-10T09:04:30.000Z'
      ) as touched
    `)).rows).toEqual([{ touched: false }]);
    expect((await db.query(`
      select public.revoke_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        '2026-07-10T09:05:00.000Z'
      ) as revoked
    `)).rows).toEqual([{ revoked: true }]);
    expect((await db.query(`
      select public.touch_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        1,
        '2026-07-10T09:05:30.000Z'
      ) as touched
    `)).rows).toEqual([{ touched: false }]);
    expect((await db.query(`
      select public.revoke_portal_collaboration_link(
        '00000000-0000-0000-0000-000000000702',
        '2026-07-10T09:06:00.000Z'
      ) as revoked
    `)).rows).toEqual([{ revoked: false }]);
    expect((await db.query(`
      select last_used_at, revoked_at, session_version
      from public.portal_collaboration_links
      where id = '00000000-0000-0000-0000-000000000702'
    `)).rows).toEqual([{
      last_used_at: new Date('2026-07-10T09:04:00.000Z'),
      revoked_at: new Date('2026-07-10T09:05:00.000Z'),
      session_version: 2,
    }]);
  });

  it('lists only published reports and reads their current publication snapshots', async () => {
    await seedPublishedCollaborationReports();
    await db.exec(`
      update public.report_items
      set title = 'unpublished live title',
          max_daily_card = 'unpublished live card'
      where id = '00000000-0000-0000-0000-000000000202'
    `);

    const reports = await db.query<{
      daily_date: Date;
      id: string;
      item_count: number;
      published_at: Date;
    }>(`
      select *
      from public.list_published_reports_for_collaboration(
        '${collaborationLinkId}',
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(reports.rows).toEqual([
      {
        daily_date: new Date('2026-07-10T00:00:00.000Z'),
        id: '00000000-0000-0000-0000-000000000101',
        item_count: 1,
        published_at: new Date('2026-07-10T09:00:00.000Z'),
      },
      {
        daily_date: new Date('2026-07-09T00:00:00.000Z'),
        id: '00000000-0000-0000-0000-000000000102',
        item_count: 1,
        published_at: new Date('2026-07-09T09:00:00.000Z'),
      },
    ]);

    const read = await db.query<{ report: {
      items: Array<{
        max_daily_card: string;
        media_id: string;
        title: string;
      }>;
      published_version: number;
      status: string;
    } }>(`
      select public.read_report_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000102',
        '2026-07-10T09:02:00.000Z'
      ) as report
    `);
    expect(read.rows[0]?.report).toMatchObject({
      items: [{
        max_daily_card: 'older published card',
        media_id: '00000000-0000-0000-0000-000000000302',
        title: 'older published title',
      }],
      published_version: 1,
      status: 'published',
    });

    await expectDatabaseError(
      'draft_report_read',
      `select public.read_report_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000103',
        '2026-07-10T09:02:00.000Z'
      )`,
      { code: 'P0002', message: 'report_not_found' },
    );
    await db.query(`
      select public.revoke_portal_collaboration_link(
        '${collaborationLinkId}',
        '2026-07-10T09:03:00.000Z'
      )
    `);
    await expectDatabaseError(
      'revoked_report_list',
      `select * from public.list_published_reports_for_collaboration(
        '${collaborationLinkId}',
        '2026-07-10T09:04:00.000Z'
      )`,
      { code: 'P0002', message: 'collaboration_link_unavailable' },
    );
    await expectDatabaseError(
      'revoked_report_read',
      `select public.read_report_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:04:00.000Z'
      )`,
      { code: 'P0002', message: 'collaboration_link_unavailable' },
    );
  });

  it('lists published reports for an Owner without depending on collaboration-link state', async () => {
    await seedPublishedCollaborationReports();
    await db.query(`
      select public.revoke_portal_collaboration_link(
        '${collaborationLinkId}',
        '2026-07-10T09:03:00.000Z'
      )
    `);

    const reports = await db.query(`
      select *
      from public.list_published_reports_for_owner(
        '${ownerId}',
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(reports.rows).toEqual([
      {
        daily_date: new Date('2026-07-10T00:00:00.000Z'),
        id: '00000000-0000-0000-0000-000000000101',
        item_count: 1,
        published_at: new Date('2026-07-10T09:00:00.000Z'),
      },
      {
        daily_date: new Date('2026-07-09T00:00:00.000Z'),
        id: '00000000-0000-0000-0000-000000000102',
        item_count: 1,
        published_at: new Date('2026-07-09T09:00:00.000Z'),
      },
    ]);

    await expectDatabaseError(
      'non_owner_report_index',
      `select * from public.list_published_reports_for_owner(
        '00000000-0000-0000-0000-000000000002',
        '2026-07-10T09:04:00.000Z'
      )`,
      { code: '42501', message: 'owner_required' },
    );
    await expectDatabaseError(
      'invalid_owner_report_index',
      `select * from public.list_published_reports_for_owner(
        null,
        '2026-07-10T09:04:00.000Z'
      )`,
      { code: '22023', message: 'invalid_owner_report_input' },
    );
  });

  it.each([
    [
      'NULL expected version',
      'null_expected_version',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'blocked mutation',
        null,
        '2026-07-10T09:02:00.000Z'`,
    ],
    [
      'NULL link id',
      'null_link_id',
      `null,
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'blocked mutation',
        1,
        '2026-07-10T09:02:00.000Z'`,
    ],
    [
      'NULL item id',
      'null_item_id',
      `'${collaborationLinkId}',
        null,
        'max_feedback',
        'blocked mutation',
        1,
        '2026-07-10T09:02:00.000Z'`,
    ],
    [
      'NULL field name',
      'null_field_name',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        null,
        'blocked mutation',
        1,
        '2026-07-10T09:02:00.000Z'`,
    ],
    [
      'NULL value',
      'null_value',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        null,
        1,
        '2026-07-10T09:02:00.000Z'`,
    ],
    [
      'NULL changed timestamp',
      'null_changed_at',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'blocked mutation',
        1,
        null`,
    ],
    [
      'non-finite changed timestamp',
      'non_finite_changed_at',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'blocked mutation',
        1,
        'infinity'::timestamptz`,
    ],
    [
      'non-positive expected version',
      'invalid_expected_version',
      `'${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'blocked mutation',
        0,
        '2026-07-10T09:02:00.000Z'`,
    ],
  ] as const)(
    'rejects %s before mutating collaboration state',
    async (_label, savepoint, argumentsSql) => {
      await seedPublishedCollaborationReports();
      const before = await readCollaborationMutationState();

      await expectDatabaseError(
        savepoint,
        `select * from public.update_collaborative_field_for_link(
          ${argumentsSql}
        )`,
        { code: '22023', message: 'invalid_collaboration_input' },
      );

      expect(await readCollaborationMutationState()).toEqual(before);
    },
  );

  it('enforces Unicode character limits while allowing exact limits and empty clears', async () => {
    await seedPublishedCollaborationReports();

    const card = await db.query<{ value_length: number; version: number }>(`
      select length(max_daily_card)::integer as value_length, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        repeat('界', 50000),
        1,
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(card.rows).toEqual([{ value_length: 50000, version: 2 }]);

    const feedback = await db.query<{ value_length: number; version: number }>(`
      select length(max_feedback)::integer as value_length, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        repeat('评', 20000),
        2,
        '2026-07-10T09:03:00.000Z'
      )
    `);
    expect(feedback.rows).toEqual([{ value_length: 20000, version: 3 }]);

    const status = await db.query<{ value_length: number; version: number }>(`
      select length(review_status)::integer as value_length, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'review_status',
        repeat('审', 128),
        3,
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(status.rows).toEqual([{ value_length: 128, version: 4 }]);

    const cleared = await db.query<{ value_length: number; version: number }>(`
      select length(max_daily_card)::integer as value_length, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        '',
        4,
        '2026-07-10T09:05:00.000Z'
      )
    `);
    expect(cleared.rows).toEqual([{ value_length: 0, version: 5 }]);

    const beforeRejectedValues = await readCollaborationMutationState();
    for (const [savepoint, fieldName, valueExpression] of [
      ['card_too_long', 'max_daily_card', `repeat('界', 50001)`],
      ['feedback_too_long', 'max_feedback', `repeat('评', 20001)`],
      ['status_too_long', 'review_status', `repeat('审', 129)`],
    ] as const) {
      await expectDatabaseError(
        savepoint,
        `select * from public.update_collaborative_field_for_link(
          '${collaborationLinkId}',
          '00000000-0000-0000-0000-000000000201',
          '${fieldName}',
          ${valueExpression},
          5,
          '2026-07-10T09:06:00.000Z'
        )`,
        { code: '22001', message: 'collaborative_value_too_long' },
      );
      expect(await readCollaborationMutationState()).toEqual(
        beforeRejectedValues,
      );
    }
  });

  it('updates only collaborative fields with version locks and link-attributed history', async () => {
    await seedPublishedCollaborationReports();
    const updated = await db.query<{
      max_daily_card: string;
      version: number;
    }>(`
      select max_daily_card, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'collaborator card',
        1,
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(updated.rows).toEqual([{
      max_daily_card: 'collaborator card',
      version: 2,
    }]);

    const synchronized = await db.query<{
      actor_id: string | null;
      audit_actor_id: string | null;
      audit_link_id: string;
      live_value: string;
      live_version: number;
      snapshot_value: string;
      snapshot_version: number;
    }>(`
      select
        ri.max_daily_card as live_value,
        ri.version as live_version,
        rpi.max_daily_card as snapshot_value,
        rpi.item_version as snapshot_version,
        rv.actor_id,
        al.actor_id as audit_actor_id,
        al.details ->> 'collaboration_link_id' as audit_link_id
      from public.report_items ri
      join public.report_publication_items rpi
        on rpi.report_item_id = ri.id
        and rpi.report_id = ri.report_id
      join public.reports r
        on r.id = ri.report_id
        and r.published_version = rpi.published_version
      join public.revisions rv
        on rv.report_item_id = ri.id
        and rv.item_version = ri.version
      join public.audit_logs al
        on al.target_id = ri.id
        and al.event_type = 'report_item.updated'
      where ri.id = '00000000-0000-0000-0000-000000000201'
    `);
    expect(synchronized.rows).toEqual([{
      actor_id: null,
      audit_actor_id: null,
      audit_link_id: collaborationLinkId,
      live_value: 'collaborator card',
      live_version: 2,
      snapshot_value: 'collaborator card',
      snapshot_version: 2,
    }]);

    await expectDatabaseError(
      'fixed_field_rejected',
      `select * from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'title',
        'forbidden title',
        2,
        '2026-07-10T09:03:00.000Z'
      )`,
      { code: '42501', message: 'field_not_editable' },
    );
    await expectDatabaseError(
      'fixed_stale_version',
      `select * from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'stale feedback',
        1,
        '2026-07-10T09:04:00.000Z'
      )`,
      { code: 'P0001', message: 'stale_version' },
    );
    await db.query(`
      select public.revoke_portal_collaboration_link(
        '${collaborationLinkId}',
        '2026-07-10T09:05:00.000Z'
      )
    `);
    await expectDatabaseError(
      'revoked_fixed_update',
      `select * from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'revoked feedback',
        2,
        '2026-07-10T09:06:00.000Z'
      )`,
      { code: 'P0002', message: 'collaboration_link_unavailable' },
    );
  });

  it('makes an Owner published edit immediately visible and editable to the collaborator', async () => {
    await seedPublishedCollaborationReports();
    const ownerUpdated = await db.query<{
      max_daily_card: string;
      version: number;
    }>(`
      select max_daily_card, version
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'Owner card',
        1,
        '${ownerId}',
        '2026-07-10T09:02:00.000Z'
      )
    `);
    expect(ownerUpdated.rows).toEqual([{
      max_daily_card: 'Owner card',
      version: 2,
    }]);

    const collaboratorRead = await db.query<{ report: {
      items: Array<{ max_daily_card: string; version: number }>;
    } }>(`
      select public.read_report_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:03:00.000Z'
      ) as report
    `);
    expect(collaboratorRead.rows[0]?.report.items).toEqual([
      expect.objectContaining({ max_daily_card: 'Owner card', version: 2 }),
    ]);

    const collaboratorUpdated = await db.query<{
      max_feedback: string;
      version: number;
    }>(`
      select max_feedback, version
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'collaborator follow-up',
        2,
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(collaboratorUpdated.rows).toEqual([{
      max_feedback: 'collaborator follow-up',
      version: 3,
    }]);

    const synchronized = await db.query<{
      live_card: string;
      live_feedback: string;
      live_version: number;
      snapshot_card: string;
      snapshot_feedback: string;
      snapshot_version: number;
    }>(`
      select
        ri.max_daily_card as live_card,
        ri.max_feedback as live_feedback,
        ri.version as live_version,
        rpi.max_daily_card as snapshot_card,
        rpi.max_feedback as snapshot_feedback,
        rpi.item_version as snapshot_version
      from public.reports r
      join public.report_items ri on ri.report_id = r.id
      join public.report_publication_items rpi
        on rpi.report_id = r.id
        and rpi.published_version = r.published_version
        and rpi.report_item_id = ri.id
      where ri.id = '00000000-0000-0000-0000-000000000201'
    `);
    expect(synchronized.rows).toEqual([{
      live_card: 'Owner card',
      live_feedback: 'collaborator follow-up',
      live_version: 3,
      snapshot_card: 'Owner card',
      snapshot_feedback: 'collaborator follow-up',
      snapshot_version: 3,
    }]);
  });

  it('keeps collaborator-to-Owner concurrency on one published version clock', async () => {
    await seedPublishedCollaborationReports();
    await db.query(`
      select *
      from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'max_daily_card',
        'collaborator first',
        1,
        '2026-07-10T09:02:00.000Z'
      )
    `);

    await expectDatabaseError(
      'stale_owner_after_collaborator',
      `select * from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'stale Owner feedback',
        1,
        '${ownerId}',
        '2026-07-10T09:03:00.000Z'
      )`,
      { code: 'P0001', message: 'stale_version' },
    );

    const ownerUpdated = await db.query<{
      max_feedback: string;
      version: number;
    }>(`
      select max_feedback, version
      from public.update_collaborative_field(
        '00000000-0000-0000-0000-000000000201',
        'max_feedback',
        'Owner follow-up',
        2,
        '${ownerId}',
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(ownerUpdated.rows).toEqual([{
      max_feedback: 'Owner follow-up',
      version: 3,
    }]);

    const collaboratorRead = await db.query<{ report: {
      items: Array<{
        max_daily_card: string;
        max_feedback: string;
        version: number;
      }>;
    } }>(`
      select public.read_report_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000101',
        '2026-07-10T09:05:00.000Z'
      ) as report
    `);
    expect(collaboratorRead.rows[0]?.report.items).toEqual([
      expect.objectContaining({
        max_daily_card: 'collaborator first',
        max_feedback: 'Owner follow-up',
        version: 3,
      }),
    ]);

    await expectDatabaseError(
      'stale_collaborator_after_owner',
      `select * from public.update_collaborative_field_for_link(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000201',
        'review_status',
        'late review',
        2,
        '2026-07-10T09:06:00.000Z'
      )`,
      { code: 'P0001', message: 'stale_version' },
    );

    const history = await db.query<{
      audit_count: number;
      revision_count: number;
    }>(`
      select
        count(distinct rv.id)::integer as revision_count,
        count(distinct al.id)::integer as audit_count
      from public.revisions rv
      cross join public.audit_logs al
      where rv.report_item_id = '00000000-0000-0000-0000-000000000201'
        and al.target_id = rv.report_item_id
        and al.event_type = 'report_item.updated'
    `);
    expect(history.rows).toEqual([{ audit_count: 2, revision_count: 2 }]);
  });

  it('authorizes only media referenced by the current published snapshot', async () => {
    await seedPublishedCollaborationReports();
    await db.exec(`
      update public.report_items
      set publisher_removed_at = '2026-07-10T09:02:00.000Z'
      where id = '00000000-0000-0000-0000-000000000201';
      insert into public.report_items(
        id,
        report_id,
        local_record_id,
        title,
        source_url,
        item_order
      ) values (
        '00000000-0000-0000-0000-000000000204',
        '00000000-0000-0000-0000-000000000101',
        'replacement-local',
        'replacement title',
        'https://example.com/replacement',
        20
      );
      insert into public.media_objects(
        id,
        report_item_id,
        object_key,
        content_type,
        byte_size,
        sha256,
        upload_status,
        uploaded_at
      ) values (
        '00000000-0000-0000-0000-000000000304',
        '00000000-0000-0000-0000-000000000204',
        'reports/2026-07-10/replacement.mp4',
        'video/mp4',
        8192,
        'replacement-ready-sha256',
        'ready',
        '2026-07-10T09:02:00.000Z'
      );
    `);
    await db.query(`
      select *
      from public.publish_report(
        '00000000-0000-0000-0000-000000000101',
        '${ownerId}',
        '2026-07-10T09:03:00.000Z'
      )
    `);

    const authorized = await db.query<{
      byte_size: number;
      content_type: string;
      daily_date: Date;
      id: string;
      object_key: string;
      report_id: string;
    }>(`
      select *
      from public.authorize_media_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000304',
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(authorized.rows).toEqual([{
      byte_size: 8192,
      content_type: 'video/mp4',
      daily_date: new Date('2026-07-10T00:00:00.000Z'),
      id: '00000000-0000-0000-0000-000000000304',
      object_key: 'reports/2026-07-10/replacement.mp4',
      report_id: '00000000-0000-0000-0000-000000000101',
    }]);

    const historicalDateMedia = await db.query<{
      daily_date: Date;
      id: string;
      report_id: string;
    }>(`
      select id, report_id, daily_date
      from public.authorize_media_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000302',
        '2026-07-10T09:04:00.000Z'
      )
    `);
    expect(historicalDateMedia.rows).toEqual([{
      daily_date: new Date('2026-07-09T00:00:00.000Z'),
      id: '00000000-0000-0000-0000-000000000302',
      report_id: '00000000-0000-0000-0000-000000000102',
    }]);

    for (const [savepoint, mediaId] of [
      ['historical_media', '00000000-0000-0000-0000-000000000301'],
      ['draft_media', '00000000-0000-0000-0000-000000000303'],
    ] as const) {
      await expectDatabaseError(
        savepoint,
        `select * from public.authorize_media_for_collaboration(
          '${collaborationLinkId}',
          '${mediaId}',
          '2026-07-10T09:04:00.000Z'
        )`,
        { code: 'P0002', message: 'media_not_found' },
      );
    }

    await db.query(`
      select public.revoke_portal_collaboration_link(
        '${collaborationLinkId}',
        '2026-07-10T09:05:00.000Z'
      )
    `);
    await expectDatabaseError(
      'revoked_media',
      `select * from public.authorize_media_for_collaboration(
        '${collaborationLinkId}',
        '00000000-0000-0000-0000-000000000304',
        '2026-07-10T09:06:00.000Z'
      )`,
      { code: 'P0002', message: 'collaboration_link_unavailable' },
    );
  });
});
