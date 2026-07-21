create or replace function public.set_media_purge_after()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  if new.upload_status = 'ready'
    and new.uploaded_at is not null
  then
    new.purge_after := case
      when tg_op = 'UPDATE'
        and new.purge_after is not distinct from old.purge_after
      then coalesce(old.purge_after, new.uploaded_at + interval '72 hours')
      when new.purge_after is not null
      then new.purge_after
      else new.uploaded_at + interval '72 hours'
    end;
  end if;
  return new;
end
$$;

create or replace function public.finalize_media_upload_completion(
  p_upload_id uuid,
  p_completed_at timestamptz
)
returns table (
  id uuid,
  report_id uuid,
  object_key text,
  content_type text,
  byte_size bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  media_record public.media_objects%rowtype;
  upload_record public.media_uploads%rowtype;
begin
  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  if upload_record.status = 'completed' then
    return query
    select
      mo.id,
      upload_record.report_id,
      mo.object_key,
      mo.content_type,
      mo.byte_size
    from public.media_objects mo
    where mo.id = upload_record.media_id
      and mo.report_item_id = upload_record.report_item_id
      and mo.object_key = upload_record.object_key
      and mo.upload_status = 'ready';
    if not found then
      raise exception using
        errcode = '55000',
        message = 'upload_not_active';
    end if;
    return;
  end if;

  if upload_record.status <> 'completing'
    or upload_record.asserted_sha256 is null
  then
    raise exception using
      errcode = '55000',
      message = 'upload_not_active';
  end if;

  select mo.*
  into media_record
  from public.media_objects mo
  where mo.report_item_id = upload_record.report_item_id
  for update;

  if found then
    if media_record.id <> upload_record.media_id then
      raise exception using
        errcode = '55000',
        message = 'upload_not_active';
    end if;
    update public.media_objects mo
    set object_key = upload_record.object_key,
        content_type = upload_record.content_type,
        byte_size = upload_record.expected_byte_size,
        sha256 = upload_record.asserted_sha256,
        sha256_source = upload_record.sha256_source,
        sha256_verification_status =
          upload_record.sha256_verification_status,
        upload_status = 'ready',
        uploaded_at = p_completed_at,
        purge_after = p_completed_at + interval '72 hours',
        purged_at = null,
        purge_attempted_at = null,
        purge_error = null
    where mo.id = media_record.id
    returning mo.* into media_record;
  else
    insert into public.media_objects (
      id,
      report_item_id,
      object_key,
      content_type,
      byte_size,
      sha256,
      sha256_source,
      sha256_verification_status,
      upload_status,
      uploaded_at,
      created_at
    ) values (
      upload_record.media_id,
      upload_record.report_item_id,
      upload_record.object_key,
      upload_record.content_type,
      upload_record.expected_byte_size,
      upload_record.asserted_sha256,
      upload_record.sha256_source,
      upload_record.sha256_verification_status,
      'ready',
      p_completed_at,
      upload_record.created_at
    )
    returning * into media_record;
  end if;

  update public.media_uploads mu
  set status = 'completed',
      completed_at = p_completed_at,
      updated_at = p_completed_at
  where mu.id = upload_record.id;

  update public.reports r
  set draft_version = r.draft_version + 1
  where r.id = upload_record.report_id;

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
    'media.upload_completed',
    upload_record.report_id,
    'media_object',
    upload_record.media_id,
    jsonb_build_object(
      'upload_id', upload_record.id,
      'publisher_device_id', upload_record.publisher_device_id,
      'byte_size', upload_record.expected_byte_size,
      'sha256_source', upload_record.sha256_source,
      'sha256_verification_status',
        upload_record.sha256_verification_status
    ),
    p_completed_at
  );

  return query
  select
    media_record.id,
    upload_record.report_id,
    media_record.object_key,
    media_record.content_type,
    media_record.byte_size;
end
$$;

create or replace function public.read_report(
  p_report_id uuid,
  p_actor_id uuid,
  p_share_link_id uuid,
  p_read_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  actor_role public.app_role;
  items_json jsonb;
  report_record public.reports%rowtype;
  share_record public.share_links%rowtype;
  use_published_snapshot boolean := false;
begin
  if (p_actor_id is null) = (p_share_link_id is null) then
    raise exception using
      errcode = 'P0002',
      message = 'report_not_found';
  end if;

  select r.*
  into report_record
  from public.reports r
  where r.id = p_report_id
  for share;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'report_not_found';
  end if;

  if p_actor_id is not null then
    select 'owner'::public.app_role
    into actor_role
    from public.profiles p
    where p.id = p_actor_id
      and p.global_role = 'owner'
    for share;

    if actor_role is null then
      select rm.role
      into actor_role
      from public.report_members rm
      where rm.report_id = report_record.id
        and rm.user_id = p_actor_id
      for share;
    end if;

    if actor_role is null then
      raise exception using
        errcode = 'P0002',
        message = 'report_not_found';
    end if;

    if actor_role = 'viewer' then
      if report_record.status <> 'published' then
        raise exception using
          errcode = 'P0002',
          message = 'report_not_found';
      end if;
      use_published_snapshot := true;
    end if;
  else
    select sl.*
    into share_record
    from public.share_links sl
    where sl.id = p_share_link_id
    for share;

    if not found
      or share_record.revoked_at is not null
      or (
        share_record.expires_at is not null
        and share_record.expires_at <= p_read_at
      )
    then
      raise exception using
        errcode = 'P0002',
        message = 'share_unavailable';
    end if;

    if share_record.report_id <> report_record.id then
      raise exception using
        errcode = 'P0002',
        message = 'report_not_found';
    end if;

    if report_record.status <> 'published' then
      raise exception using
        errcode = 'P0002',
        message = 'share_unavailable';
    end if;

    use_published_snapshot := true;
  end if;

  if use_published_snapshot then
    if not exists (
      select 1
      from public.report_publications rp
      where rp.report_id = report_record.id
        and rp.published_version = report_record.published_version
    ) then
      if p_share_link_id is not null then
        raise exception using
          errcode = 'P0002',
          message = 'share_unavailable';
      end if;

      raise exception using
        errcode = 'P0002',
        message = 'report_not_found';
    end if;

    select coalesce(
      jsonb_agg(
        jsonb_build_object(
          'id', rpi.report_item_id,
          'local_record_id', rpi.local_record_id,
          'title', rpi.title,
          'caption', rpi.caption,
          'source_url', rpi.source_url,
          'max_daily_card', rpi.max_daily_card,
          'max_feedback', rpi.max_feedback,
          'review_status', rpi.review_status,
          'item_order', rpi.item_order,
          'version', rpi.item_version,
          'media_id', rpi.media_id
        )
        order by rpi.item_order, rpi.report_item_id
      ),
      '[]'::jsonb
    )
    into items_json
    from public.report_publication_items rpi
    where rpi.report_id = report_record.id
      and rpi.published_version = report_record.published_version;
  else
    select coalesce(
      jsonb_agg(
        jsonb_build_object(
          'id', ri.id,
          'local_record_id', ri.local_record_id,
          'title', ri.title,
          'caption', ri.caption,
          'source_url', ri.source_url,
          'max_daily_card', ri.max_daily_card,
          'max_feedback', ri.max_feedback,
          'review_status', ri.review_status,
          'item_order', ri.item_order,
          'version', ri.version,
          'media_id', mo.id
        )
        order by ri.item_order, ri.id
      ),
      '[]'::jsonb
    )
    into items_json
    from public.report_items ri
    left join public.media_objects mo
      on mo.report_item_id = ri.id
    where ri.report_id = report_record.id
      and ri.publisher_removed_at is null;
  end if;

  return jsonb_build_object(
    'id', report_record.id,
    'daily_date', report_record.daily_date,
    'status', report_record.status,
    'draft_version', report_record.draft_version,
    'published_version', report_record.published_version,
    'published_at', report_record.published_at,
    'items', items_json
  );
end
$$;

revoke all on function public.set_media_purge_after() from public;
revoke all on function public.finalize_media_upload_completion(
  uuid,
  timestamptz
) from public;
revoke all on function public.read_report(
  uuid,
  uuid,
  uuid,
  timestamptz
) from public;

grant execute on function public.finalize_media_upload_completion(
  uuid,
  timestamptz
) to service_role;
grant execute on function public.read_report(
  uuid,
  uuid,
  uuid,
  timestamptz
) to service_role;
