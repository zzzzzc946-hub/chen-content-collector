import { afterEach, describe, expect, it, vi } from 'vitest';
import { MediaError } from '../src/media';
import { ReportError } from '../src/reports';
import { SupabaseRestStorage } from '../src/storage';

const LINK_ID = '00000000-0000-0000-0000-000000000501';
const REPORT_ID = '00000000-0000-0000-0000-000000000101';
const ITEM_ID = '00000000-0000-0000-0000-000000000201';
const MEDIA_ID = '00000000-0000-0000-0000-000000000301';
const OWNER_ID = '00000000-0000-0000-0000-000000000001';
const NOW = new Date('2026-07-15T02:30:00.000Z');

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    headers: { 'content-type': 'application/json' },
    status,
  });
}

function portalRow() {
  return {
    created_at: '2026-07-14T02:00:00.000Z',
    created_by: OWNER_ID,
    id: LINK_ID,
    last_used_at: null,
    revoked_at: null,
    session_version: 3,
    token_hash: 'token-hash',
  };
}

function reportItemRow() {
  return {
    caption: 'caption',
    id: ITEM_ID,
    local_record_id: 'local-1',
    max_daily_card: 'card',
    max_feedback: '',
    media_id: MEDIA_ID,
    review_status: 'pending',
    source_url: 'https://example.com/source',
    title: 'title',
    version: 4,
  };
}

function reportRow() {
  return {
    daily_date: '2026-07-15',
    draft_version: 4,
    id: REPORT_ID,
    items: [reportItemRow()],
    published_at: '2026-07-15T01:00:00.000Z',
    published_version: 2,
    status: 'published',
  };
}

function mediaRow() {
  return {
    byte_size: 100,
    content_type: 'video/mp4',
    daily_date: '2026-07-15',
    id: MEDIA_ID,
    object_key: `reports/${REPORT_ID}/published.mp4`,
    report_id: REPORT_ID,
  };
}

type CollaborationOperation = 'index' | 'item' | 'media' | 'report';

function runCollaborationOperation(
  storage: SupabaseRestStorage,
  operation: CollaborationOperation,
): Promise<unknown> {
  if (operation === 'index') {
    return storage.listReportsForCollaborationAtomic({
      linkId: LINK_ID,
      readAt: NOW,
    });
  }
  if (operation === 'report') {
    return storage.readReportForCollaborationAtomic({
      linkId: LINK_ID,
      readAt: NOW,
      reportId: REPORT_ID,
    });
  }
  if (operation === 'item') {
    return storage.updateCollaborativeFieldForLinkAtomic({
      changedAt: NOW,
      expectedVersion: 4,
      field: 'max_feedback',
      itemId: ITEM_ID,
      linkId: LINK_ID,
      value: '',
    });
  }
  return storage.authorizeMediaForCollaborationAtomic({
    linkId: LINK_ID,
    mediaId: MEDIA_ID,
    readAt: NOW,
  });
}

describe('SupabaseRestStorage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('calls the default global fetch with the runtime global as this', async () => {
    let fetchThis: unknown;
    vi.stubGlobal('fetch', function (this: unknown) {
      fetchThis = this;
      return Promise.resolve(new Response(
        JSON.stringify([{ global_role: 'owner' }]),
        {
          headers: { 'content-type': 'application/json' },
          status: 200,
        },
      ));
    });
    const storage = new SupabaseRestStorage({
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.getIdentityRole('owner-id')).resolves.toBe('owner');
    expect(fetchThis).toBe(globalThis);
  });

  it('uses atomic service-role RPCs for collaboration link mutations', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(jsonResponse(portalRow()))
      .mockResolvedValueOnce(jsonResponse(true))
      .mockResolvedValueOnce(jsonResponse(true));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });
    const record = {
      createdAt: new Date('2026-07-14T02:00:00.000Z'),
      createdBy: OWNER_ID,
      id: LINK_ID,
      lastUsedAt: null,
      revokedAt: null,
      sessionVersion: 3,
      tokenHash: 'token-hash',
    };

    await expect(storage.replaceActivePortalLink(record)).resolves.toBeUndefined();
    await expect(storage.revokePortalLink(LINK_ID, NOW)).resolves.toBe(true);
    await expect(storage.touchPortalLink(LINK_ID, 3, NOW)).resolves.toBe(true);

    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/replace_active_portal_collaboration_link',
      'https://project.supabase.co/rest/v1/rpc/revoke_portal_collaboration_link',
      'https://project.supabase.co/rest/v1/rpc/touch_portal_collaboration_link',
    ]);
    expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body))))
      .toEqual([
        {
          p_created_at: record.createdAt.toISOString(),
          p_created_by: OWNER_ID,
          p_id: LINK_ID,
          p_token_hash: 'token-hash',
        },
        { p_id: LINK_ID, p_revoked_at: NOW.toISOString() },
        {
          p_expected_session_version: 3,
          p_id: LINK_ID,
          p_used_at: NOW.toISOString(),
        },
      ]);
  });

  it('finds collaboration links through defensive service-role reads', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(jsonResponse([portalRow()]))
      .mockResolvedValueOnce(jsonResponse([portalRow()]));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.findPortalLinkById(LINK_ID)).resolves.toMatchObject({
      createdAt: new Date('2026-07-14T02:00:00.000Z'),
      id: LINK_ID,
      sessionVersion: 3,
    });
    await expect(storage.findPortalLinkByTokenHash('token-hash'))
      .resolves.toMatchObject({ id: LINK_ID, tokenHash: 'token-hash' });

    const urls = fetcher.mock.calls.map(([url]) => new URL(String(url)));
    expect(urls[0]?.pathname).toBe('/rest/v1/portal_collaboration_links');
    expect(urls[0]?.searchParams.get('id')).toBe(`eq.${LINK_ID}`);
    expect(urls[1]?.searchParams.get('token_hash')).toBe('eq.token-hash');
  });

  it('adapts collaboration RPCs with exact URLs and snake_case bodies', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(jsonResponse([{
        daily_date: '2026-07-15',
        id: REPORT_ID,
        item_count: 1,
        published_at: '2026-07-15T01:00:00.000Z',
      }]))
      .mockResolvedValueOnce(jsonResponse(reportRow()))
      .mockResolvedValueOnce(jsonResponse([reportItemRow()]))
      .mockResolvedValueOnce(jsonResponse([mediaRow()]));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.listReportsForCollaborationAtomic({
      linkId: LINK_ID,
      readAt: NOW,
    })).resolves.toEqual([{
      dailyDate: '2026-07-15',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: '2026-07-15T01:00:00.000Z',
    }]);
    await expect(storage.readReportForCollaborationAtomic({
      linkId: LINK_ID,
      readAt: NOW,
      reportId: REPORT_ID,
    })).resolves.toMatchObject({ accessRole: 'collaborator', id: REPORT_ID });
    await expect(storage.updateCollaborativeFieldForLinkAtomic({
      changedAt: NOW,
      expectedVersion: 4,
      field: 'max_feedback',
      itemId: ITEM_ID,
      linkId: LINK_ID,
      value: '',
    })).resolves.toMatchObject({ id: ITEM_ID, maxFeedback: '' });
    await expect(storage.authorizeMediaForCollaborationAtomic({
      linkId: LINK_ID,
      mediaId: MEDIA_ID,
      readAt: NOW,
    })).resolves.toMatchObject({ dailyDate: '2026-07-15', id: MEDIA_ID });

    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      'https://project.supabase.co/rest/v1/rpc/list_published_reports_for_collaboration',
      'https://project.supabase.co/rest/v1/rpc/read_report_for_collaboration',
      'https://project.supabase.co/rest/v1/rpc/update_collaborative_field_for_link',
      'https://project.supabase.co/rest/v1/rpc/authorize_media_for_collaboration',
    ]);
    expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body))))
      .toEqual([
        { p_link_id: LINK_ID, p_read_at: NOW.toISOString() },
        {
          p_link_id: LINK_ID,
          p_read_at: NOW.toISOString(),
          p_report_id: REPORT_ID,
        },
        {
          p_changed_at: NOW.toISOString(),
          p_expected_version: 4,
          p_field_name: 'max_feedback',
          p_item_id: ITEM_ID,
          p_link_id: LINK_ID,
          p_value: '',
        },
        {
          p_link_id: LINK_ID,
          p_media_id: MEDIA_ID,
          p_read_at: NOW.toISOString(),
        },
      ]);
  });

  it('adapts the Owner report index RPC with an exact URL and body', async () => {
    const fetcher = vi.fn(async () => jsonResponse([{
      daily_date: '2026-07-15',
      id: REPORT_ID,
      item_count: 1,
      published_at: '2026-07-15T01:00:00.000Z',
    }]));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.listReportsForOwnerAtomic({
      actorId: OWNER_ID,
      readAt: NOW,
    })).resolves.toEqual([{
      dailyDate: '2026-07-15',
      id: REPORT_ID,
      itemCount: 1,
      publishedAt: '2026-07-15T01:00:00.000Z',
    }]);
    expect(fetcher).toHaveBeenCalledWith(
      'https://project.supabase.co/rest/v1/rpc/list_published_reports_for_owner',
      expect.objectContaining({
        body: JSON.stringify({
          p_actor_id: OWNER_ID,
          p_read_at: NOW.toISOString(),
        }),
        method: 'POST',
      }),
    );
  });

  it('fails closed on a malformed Owner report index payload', async () => {
    const storage = new SupabaseRestStorage({
      fetch: vi.fn(async () => jsonResponse([{
        daily_date: '2026-07-15',
        id: REPORT_ID,
        item_count: 'private_payload_marker',
        published_at: '2026-07-15T01:00:00.000Z',
      }])),
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    const error = await storage.listReportsForOwnerAtomic({
      actorId: OWNER_ID,
      readAt: NOW,
    }).catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      code: 'storage_error',
      message: 'storage request failed',
      status: 500,
    });
    expect(String(error)).not.toContain('private_payload_marker');
  });

  it.each([
    ['owner_required', 403, 'owner_required', 'owner access required'],
    ['invalid_owner_report_input', 400, 'invalid_owner_report_input',
      'owner report input is invalid'],
    ['private_provider_error', 500, 'storage_error', 'storage request failed'],
  ] as const)(
    'maps %s from the Owner report RPC to a sanitized error',
    async (providerMessage, status, code, message) => {
      const storage = new SupabaseRestStorage({
        fetch: vi.fn(async () => jsonResponse({
          code: 'PRIVATE_PROVIDER_CODE',
          details: 'private_payload_marker',
          message: providerMessage,
        }, 400)),
        serviceRoleKey: 'service-role-key',
        supabaseUrl: 'https://project.supabase.co',
      });

      const error = await storage.listReportsForOwnerAtomic({
        actorId: OWNER_ID,
        readAt: NOW,
      }).catch((caught: unknown) => caught);

      expect(error).toBeInstanceOf(ReportError);
      expect(error).toMatchObject({ code, message, status });
      expect(JSON.stringify(error)).not.toContain('PRIVATE_PROVIDER_CODE');
      expect(JSON.stringify(error)).not.toContain('private_payload_marker');
    },
  );

  it('does not widen authenticated identity roles to collaborator', async () => {
    const fetcher = vi.fn(async () =>
      jsonResponse([{ global_role: 'collaborator' }]));
    const storage = new SupabaseRestStorage({
      fetch: fetcher,
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    await expect(storage.getIdentityRole('collaborator-id')).rejects.toMatchObject({
      code: 'storage_error',
      message: 'storage request failed',
    });
  });

  it('rejects malformed collaboration link rows', async () => {
    const malformedStorage = new SupabaseRestStorage({
      fetch: vi.fn(async () => jsonResponse([{ ...portalRow(), session_version: 0 }])),
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });
    await expect(malformedStorage.findPortalLinkById(LINK_ID)).rejects
      .toMatchObject({ code: 'storage_error', message: 'storage request failed' });
  });

  it.each([
    {
      ErrorType: ReportError,
      operation: 'index' as const,
      payload: [{
        daily_date: '2026-07-15',
        id: REPORT_ID,
        item_count: 'private_payload_marker',
        published_at: '2026-07-15T01:00:00.000Z',
      }],
    },
    {
      ErrorType: ReportError,
      operation: 'report' as const,
      payload: { ...reportRow(), draft_version: 'private_payload_marker' },
    },
    {
      ErrorType: ReportError,
      operation: 'item' as const,
      payload: [{ ...reportItemRow(), version: 'private_payload_marker' }],
    },
    {
      ErrorType: MediaError,
      operation: 'media' as const,
      payload: [{ ...mediaRow(), byte_size: 'private_payload_marker' }],
    },
  ])('fails closed on malformed collaboration $operation payloads', async ({
    ErrorType,
    operation,
    payload,
  }) => {
    const storage = new SupabaseRestStorage({
      fetch: vi.fn(async () => jsonResponse(payload)),
      serviceRoleKey: 'service-role-key',
      supabaseUrl: 'https://project.supabase.co',
    });

    const error = await runCollaborationOperation(storage, operation)
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ErrorType);
    expect(error).toMatchObject({
      code: 'storage_error',
      message: 'storage request failed',
      status: 500,
    });
    expect(String(error)).not.toContain('private_payload_marker');
  });

  it.each([
    ['index', 'collaboration_link_unavailable', ReportError, 410,
      'collaboration_link_unavailable', 'collaboration link is unavailable'],
    ['report', 'report_not_found', ReportError, 404,
      'report_not_found', 'report is unavailable'],
    ['item', 'item_not_found', ReportError, 404,
      'item_not_found', 'item is unavailable'],
    ['item', 'stale_version', ReportError, 409,
      'stale_version', 'item version is stale'],
    ['item', 'field_not_editable', ReportError, 403,
      'field_not_editable', 'field is not collaboratively editable'],
    ['item', 'collaborative_value_too_long', ReportError, 400,
      'collaborative_value_too_long', 'collaborative field value is too long'],
    ['item', 'invalid_collaboration_input', ReportError, 400,
      'invalid_collaboration_input', 'collaboration input is invalid'],
    ['media', 'collaboration_link_unavailable', MediaError, 410,
      'collaboration_link_unavailable', 'collaboration link is unavailable'],
    ['media', 'media_not_found', MediaError, 404,
      'media_not_found', 'media is unavailable'],
    ['index', 'private_provider_report_error', ReportError, 500,
      'storage_error', 'storage request failed'],
    ['media', 'private_provider_media_error', MediaError, 500,
      'storage_error', 'storage request failed'],
  ] as const)(
    'maps $1 from the $0 collaboration RPC to a sanitized domain error',
    async (
      operation,
      providerMessage,
      ErrorType,
      status,
      code,
      message,
    ) => {
      const storage = new SupabaseRestStorage({
        fetch: vi.fn(async () => jsonResponse({
          code: 'PRIVATE_PROVIDER_CODE',
          details: 'private_payload_marker',
          message: providerMessage,
        }, 400)),
        serviceRoleKey: 'service-role-key',
        supabaseUrl: 'https://project.supabase.co',
      });

      const error = await runCollaborationOperation(storage, operation)
        .catch((caught: unknown) => caught);

      expect(error).toBeInstanceOf(ErrorType);
      expect(error).toMatchObject({ code, message, status });
      expect(JSON.stringify(error)).not.toContain('PRIVATE_PROVIDER_CODE');
      expect(JSON.stringify(error)).not.toContain('private_payload_marker');
    },
  );
});
