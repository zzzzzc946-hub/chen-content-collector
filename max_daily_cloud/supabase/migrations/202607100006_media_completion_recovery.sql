create function public.claim_stale_media_upload_completion(
  p_upload_id uuid,
  p_claimed_at timestamptz,
  p_stale_before timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  if p_stale_before > p_claimed_at then
    raise exception using
      errcode = '22023',
      message = 'invalid_upload';
  end if;

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found
    or upload_record.status <> 'completing'
    or upload_record.transition_started_at is null
    or upload_record.transition_started_at > p_stale_before
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  update public.media_uploads mu
  set transition_started_at = p_claimed_at,
      updated_at = p_claimed_at
  where mu.id = upload_record.id;

  insert into public.audit_logs (
    actor_id,
    event_type,
    report_id,
    target_type,
    target_id,
    details,
    created_at
  ) values (
    upload_record.created_by,
    'media.upload_completion_recovery_claimed',
    upload_record.report_id,
    'media_upload',
    upload_record.id,
    jsonb_build_object(
      'publisher_device_id', upload_record.publisher_device_id
    ),
    p_claimed_at
  );

  return public.media_upload_payload(upload_record.id);
end
$$;

create function public.list_media_upload_recovery_parts(p_upload_id uuid)
returns table (
  part_number integer,
  etag text,
  byte_size bigint
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
begin
  if not exists (
    select 1
    from public.media_uploads mu
    where mu.id = p_upload_id
      and mu.status = 'completing'
  ) then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  return query
  select
    mup.part_number,
    mup.etag,
    mup.byte_size
  from public.media_upload_parts mup
  where mup.upload_id = p_upload_id
  order by mup.part_number;
end
$$;

create function public.reset_stale_media_upload_completion(
  p_upload_id uuid,
  p_reset_at timestamptz,
  p_expected_claimed_at timestamptz,
  p_reason text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  if p_reason <> 'r2_upload_missing'
    or p_expected_claimed_at > p_reset_at
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_upload';
  end if;

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found
    or upload_record.status <> 'completing'
    or upload_record.transition_started_at is distinct from
      p_expected_claimed_at
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  update public.media_uploads mu
  set status = 'aborted',
      aborted_at = p_reset_at,
      transition_started_at = null,
      abort_cleanup = true,
      updated_at = p_reset_at
  where mu.id = upload_record.id;

  insert into public.audit_logs (
    actor_id,
    event_type,
    report_id,
    target_type,
    target_id,
    details,
    created_at
  ) values (
    upload_record.created_by,
    'media.upload_completion_reset',
    upload_record.report_id,
    'media_upload',
    upload_record.id,
    jsonb_build_object(
      'publisher_device_id', upload_record.publisher_device_id,
      'reason', p_reason
    ),
    p_reset_at
  );

  return public.media_upload_payload(upload_record.id);
end
$$;

create or replace function public.list_media_upload_recovery_page(
  p_stale_at timestamptz,
  p_after_id uuid,
  p_limit integer
)
returns table (
  id uuid,
  report_id uuid,
  report_item_id uuid,
  media_id uuid,
  object_key text,
  r2_upload_id text,
  content_type text,
  expected_byte_size bigint,
  status text,
  created_at timestamptz,
  expires_at timestamptz,
  asserted_sha256 text,
  sha256_source text,
  sha256_verification_status text,
  transition_started_at timestamptz,
  abort_cleanup boolean
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
begin
  if p_limit not between 1 and 100 then
    raise exception using
      errcode = '22023',
      message = 'invalid_upload';
  end if;

  return query
  select
    mu.id,
    mu.report_id,
    mu.report_item_id,
    mu.media_id,
    mu.object_key,
    mu.r2_upload_id,
    mu.content_type,
    mu.expected_byte_size,
    mu.status,
    mu.created_at,
    mu.expires_at,
    mu.asserted_sha256,
    mu.sha256_source,
    mu.sha256_verification_status,
    mu.transition_started_at,
    mu.abort_cleanup
  from public.media_uploads mu
  where (p_after_id is null or mu.id > p_after_id)
    and (
      (
        mu.status in ('creating', 'uploading')
        and mu.expires_at <= p_stale_at
      )
      or mu.status = 'completing'
      or mu.status = 'aborting'
    )
  order by mu.id
  limit p_limit;
end
$$;

revoke all on function public.claim_stale_media_upload_completion(
  uuid,
  timestamptz,
  timestamptz
) from public;
revoke all on function public.list_media_upload_recovery_parts(uuid)
from public;
revoke all on function public.reset_stale_media_upload_completion(
  uuid,
  timestamptz,
  timestamptz,
  text
) from public;

grant execute on function public.claim_stale_media_upload_completion(
  uuid,
  timestamptz,
  timestamptz
) to service_role;
grant execute on function public.list_media_upload_recovery_parts(uuid)
to service_role;
grant execute on function public.reset_stale_media_upload_completion(
  uuid,
  timestamptz,
  timestamptz,
  text
) to service_role;
