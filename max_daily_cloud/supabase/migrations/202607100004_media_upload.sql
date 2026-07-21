create table public.media_uploads (
  id uuid primary key,
  report_id uuid not null references public.reports(id) on delete cascade,
  report_item_id uuid not null references public.report_items(id) on delete cascade,
  media_id uuid not null references public.media_objects(id) on delete cascade,
  object_key text not null unique
    check (
      object_key ~
      '^reports/[0-9a-f-]{36}/media/[0-9a-f-]{36}/[0-9a-f-]{36}$'
    ),
  r2_upload_id text
    check (r2_upload_id is null or length(trim(r2_upload_id)) between 1 and 512),
  content_type text not null
    check (
      content_type in (
        'video/mp4',
        'video/quicktime',
        'video/webm',
        'video/x-m4v'
      )
    ),
  expected_byte_size bigint not null
    check (expected_byte_size between 1 and 5497558138880),
  status text not null default 'creating'
    check (status in ('creating', 'uploading', 'completed', 'aborted')),
  created_by uuid references auth.users(id) on delete set null,
  publisher_device_id uuid
    references public.publisher_devices(id) on delete set null,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  expires_at timestamptz not null,
  completed_at timestamptz,
  aborted_at timestamptz,
  check ((created_by is null) <> (publisher_device_id is null)),
  check (expires_at = created_at + interval '24 hours')
);

create table public.media_upload_parts (
  upload_id uuid not null
    references public.media_uploads(id) on delete cascade,
  part_number integer not null check (part_number between 1 and 10000),
  etag text not null check (length(trim(etag)) between 1 and 512),
  byte_size bigint not null check (byte_size between 1 and 5368709120),
  uploaded_at timestamptz not null,
  primary key (upload_id, part_number)
);

create unique index media_uploads_one_active_item_idx
  on public.media_uploads (report_item_id)
  where status in ('creating', 'uploading');
create index media_uploads_stale_idx
  on public.media_uploads (expires_at, id)
  where status in ('creating', 'uploading');

create function public.assert_media_uploader(
  p_actor_id uuid,
  p_publisher_device_id uuid
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if (p_actor_id is null) = (p_publisher_device_id is null) then
    raise exception using
      errcode = '42501',
      message = 'owner_required';
  end if;

  if p_actor_id is not null then
    perform 1
    from public.profiles p
    where p.id = p_actor_id
      and p.global_role = 'owner'
    for share;

    if not found then
      raise exception using
        errcode = '42501',
        message = 'owner_required';
    end if;
    return;
  end if;

  perform 1
  from public.publisher_devices pd
  join public.profiles p
    on p.id = pd.created_by
    and p.global_role = 'owner'
  where pd.id = p_publisher_device_id
    and pd.revoked_at is null
  for share of pd;

  if not found then
    raise exception using
      errcode = '42501',
      message = 'publisher_device_unavailable';
  end if;
end
$$;

create function public.media_upload_payload(p_upload_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', mu.id,
    'report_id', mu.report_id,
    'report_item_id', mu.report_item_id,
    'media_id', mu.media_id,
    'object_key', mu.object_key,
    'r2_upload_id', mu.r2_upload_id,
    'content_type', mu.content_type,
    'expected_byte_size', mu.expected_byte_size,
    'status', mu.status,
    'created_at', mu.created_at,
    'expires_at', mu.expires_at,
    'parts', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'part_number', mup.part_number,
          'etag', mup.etag,
          'byte_size', mup.byte_size
        )
        order by mup.part_number
      )
      from public.media_upload_parts mup
      where mup.upload_id = mu.id
    ), '[]'::jsonb)
  )
  from public.media_uploads mu
  where mu.id = p_upload_id
$$;

create function public.authorize_media_read(
  p_media_id uuid,
  p_actor_id uuid,
  p_share_link_id uuid,
  p_public_report_id uuid,
  p_read_at timestamptz
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
  share_record public.share_links%rowtype;
begin
  if (p_actor_id is null) = (p_share_link_id is null) then
    raise exception using
      errcode = 'P0002',
      message = 'media_not_found';
  end if;

  if p_actor_id is not null then
    if exists (
      select 1
      from public.profiles p
      where p.id = p_actor_id
        and p.global_role = 'owner'
    ) then
      return query
      select
        mo.id,
        ri.report_id,
        mo.object_key,
        mo.content_type,
        mo.byte_size
      from public.media_objects mo
      join public.report_items ri
        on ri.id = mo.report_item_id
      where mo.id = p_media_id
        and mo.upload_status = 'ready'
        and mo.byte_size > 0;
      if found then return; end if;
    end if;

    return query
    select
      mo.id,
      ri.report_id,
      mo.object_key,
      mo.content_type,
      mo.byte_size
    from public.media_objects mo
    join public.report_items ri
      on ri.id = mo.report_item_id
    join public.report_members rm
      on rm.report_id = ri.report_id
      and rm.user_id = p_actor_id
      and rm.role = 'editor'
    where mo.id = p_media_id
      and mo.upload_status = 'ready'
      and mo.byte_size > 0;
    if found then return; end if;

    return query
    select
      rpi.media_id,
      rpi.report_id,
      rpi.media_object_key,
      rpi.media_content_type,
      rpi.media_byte_size
    from public.report_publication_items rpi
    join public.reports r
      on r.id = rpi.report_id
      and r.status = 'published'
      and r.published_version = rpi.published_version
    join public.report_members rm
      on rm.report_id = rpi.report_id
      and rm.user_id = p_actor_id
      and rm.role = 'viewer'
    where rpi.media_id = p_media_id
      and rpi.media_upload_status = 'ready'
      and rpi.media_byte_size > 0;
    if found then return; end if;

    raise exception using
      errcode = 'P0002',
      message = 'media_not_found';
  end if;

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

  if p_public_report_id is null
    or share_record.report_id <> p_public_report_id
  then
    raise exception using
      errcode = 'P0002',
      message = 'media_not_found';
  end if;

  if not exists (
    select 1
    from public.reports r
    join public.report_publications rp
      on rp.report_id = r.id
      and rp.published_version = r.published_version
    where r.id = share_record.report_id
      and r.status = 'published'
  ) then
    raise exception using
      errcode = 'P0002',
      message = 'share_unavailable';
  end if;

  return query
  select
    rpi.media_id,
    rpi.report_id,
    rpi.media_object_key,
    rpi.media_content_type,
    rpi.media_byte_size
  from public.report_publication_items rpi
  join public.reports r
    on r.id = rpi.report_id
    and r.status = 'published'
    and r.published_version = rpi.published_version
  where rpi.report_id = share_record.report_id
    and rpi.media_id = p_media_id
    and rpi.media_upload_status = 'ready'
    and rpi.media_byte_size > 0;
  if found then return; end if;

  raise exception using
    errcode = 'P0002',
    message = 'media_not_found';
end
$$;

create function public.begin_media_upload(
  p_upload_id uuid,
  p_report_id uuid,
  p_report_item_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid,
  p_content_type text,
  p_expected_byte_size bigint,
  p_started_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  active_upload public.media_uploads%rowtype;
  media_record public.media_objects%rowtype;
  object_key_value text;
begin
  perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);

  if p_content_type not in (
    'video/mp4',
    'video/quicktime',
    'video/webm',
    'video/x-m4v'
  ) or p_expected_byte_size not between 1 and 5497558138880 then
    raise exception using
      errcode = '22023',
      message = 'invalid_upload';
  end if;

  perform 1
  from public.report_items ri
  where ri.id = p_report_item_id
    and ri.report_id = p_report_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'report_item_not_found';
  end if;

  select mu.*
  into active_upload
  from public.media_uploads mu
  where mu.report_item_id = p_report_item_id
    and mu.status in ('creating', 'uploading')
  order by mu.created_at desc, mu.id
  limit 1
  for update;

  if found then
    if active_upload.expires_at <= p_started_at then
      raise exception using
        errcode = '55000',
        message = 'upload_in_progress';
    end if;
    if p_actor_id is null
      and active_upload.publisher_device_id <> p_publisher_device_id
    then
      raise exception using
        errcode = '55000',
        message = 'upload_in_progress';
    end if;
    if active_upload.content_type <> p_content_type
      or active_upload.expected_byte_size <> p_expected_byte_size
    then
      raise exception using
        errcode = '55000',
        message = 'upload_in_progress';
    end if;
    return public.media_upload_payload(active_upload.id)
      || jsonb_build_object('resumed', true);
  end if;

  select mo.*
  into media_record
  from public.media_objects mo
  where mo.report_item_id = p_report_item_id
  for update;

  if not found then
    media_record.id := gen_random_uuid();
  end if;

  object_key_value := format(
    'reports/%s/media/%s/%s',
    p_report_id,
    media_record.id,
    p_upload_id
  );

  if media_record.report_item_id is null then
    insert into public.media_objects (
      id,
      report_item_id,
      object_key,
      content_type,
      byte_size,
      sha256,
      upload_status,
      uploaded_at,
      created_at
    ) values (
      media_record.id,
      p_report_item_id,
      object_key_value,
      p_content_type,
      0,
      '',
      'pending',
      null,
      p_started_at
    )
    returning * into media_record;
  else
    update public.media_objects mo
    set object_key = object_key_value,
        content_type = p_content_type,
        byte_size = 0,
        sha256 = '',
        upload_status = 'pending',
        uploaded_at = null
    where mo.id = media_record.id
    returning mo.* into media_record;
  end if;

  insert into public.media_uploads (
    id,
    report_id,
    report_item_id,
    media_id,
    object_key,
    content_type,
    expected_byte_size,
    status,
    created_by,
    publisher_device_id,
    created_at,
    updated_at,
    expires_at
  ) values (
    p_upload_id,
    p_report_id,
    p_report_item_id,
    media_record.id,
    object_key_value,
    p_content_type,
    p_expected_byte_size,
    'creating',
    p_actor_id,
    p_publisher_device_id,
    p_started_at,
    p_started_at,
    p_started_at + interval '24 hours'
  );

  insert into public.audit_logs (
    actor_id,
    event_type,
    report_id,
    target_type,
    target_id,
    details,
    created_at
  ) values (
    p_actor_id,
    'media.upload_started',
    p_report_id,
    'media_upload',
    p_upload_id,
    jsonb_build_object(
      'media_id', media_record.id,
      'publisher_device_id', p_publisher_device_id,
      'expected_byte_size', p_expected_byte_size
    ),
    p_started_at
  );

  return public.media_upload_payload(p_upload_id)
    || jsonb_build_object('resumed', false);
end
$$;

create function public.attach_media_upload(
  p_upload_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid,
  p_r2_upload_id text,
  p_attached_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);
  if trim(p_r2_upload_id) = '' or length(p_r2_upload_id) > 512 then
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
    or (
      p_actor_id is null
      and upload_record.publisher_device_id <> p_publisher_device_id
    )
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  if upload_record.status not in ('creating', 'uploading') then
    raise exception using
      errcode = '55000',
      message = 'upload_not_active';
  end if;
  if upload_record.expires_at <= p_attached_at then
    raise exception using
      errcode = '55000',
      message = 'upload_expired';
  end if;

  if upload_record.r2_upload_id is null then
    update public.media_uploads mu
    set r2_upload_id = p_r2_upload_id,
        status = 'uploading',
        updated_at = p_attached_at
    where mu.id = upload_record.id
    returning mu.* into upload_record;

    update public.media_objects mo
    set upload_status = 'uploading'
    where mo.id = upload_record.media_id
      and mo.object_key = upload_record.object_key;
  end if;

  return public.media_upload_payload(upload_record.id);
end
$$;

create function public.read_media_upload(
  p_upload_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id;

  if not found
    or (
      p_actor_id is null
      and upload_record.publisher_device_id <> p_publisher_device_id
    )
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  return public.media_upload_payload(upload_record.id);
end
$$;

create function public.record_media_upload_part(
  p_upload_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid,
  p_part_number integer,
  p_etag text,
  p_byte_size bigint,
  p_uploaded_at timestamptz
)
returns table (
  part_number integer,
  etag text,
  byte_size bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);

  if p_part_number not between 1 and 10000
    or p_byte_size not between 1 and 5368709120
    or trim(p_etag) = ''
    or length(p_etag) > 512
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_upload_part';
  end if;

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found
    or (
      p_actor_id is null
      and upload_record.publisher_device_id <> p_publisher_device_id
    )
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  if upload_record.status <> 'uploading'
    or upload_record.r2_upload_id is null
    or p_byte_size > upload_record.expected_byte_size
  then
    raise exception using
      errcode = '55000',
      message = 'upload_not_active';
  end if;
  if upload_record.expires_at <= p_uploaded_at then
    raise exception using
      errcode = '55000',
      message = 'upload_expired';
  end if;

  insert into public.media_upload_parts (
    upload_id,
    part_number,
    etag,
    byte_size,
    uploaded_at
  ) values (
    upload_record.id,
    p_part_number,
    p_etag,
    p_byte_size,
    p_uploaded_at
  )
  on conflict on constraint media_upload_parts_pkey do update
    set etag = excluded.etag,
        byte_size = excluded.byte_size,
        uploaded_at = excluded.uploaded_at;

  update public.media_uploads mu
  set updated_at = p_uploaded_at
  where mu.id = upload_record.id;

  return query
  select
    mup.part_number,
    mup.etag,
    mup.byte_size
  from public.media_upload_parts mup
  where mup.upload_id = upload_record.id
    and mup.part_number = p_part_number;
end
$$;

create function public.complete_media_upload(
  p_upload_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid,
  p_sha256 text,
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
  affected_rows integer;
  part_count integer;
  largest_part integer;
  total_bytes bigint;
  upload_record public.media_uploads%rowtype;
begin
  perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);
  if p_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception using
      errcode = '22023',
      message = 'invalid_sha256';
  end if;

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found
    or (
      p_actor_id is null
      and upload_record.publisher_device_id <> p_publisher_device_id
    )
  then
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
      and mo.object_key = upload_record.object_key
      and mo.upload_status = 'ready';
    return;
  end if;

  if upload_record.status <> 'uploading'
    or upload_record.r2_upload_id is null
  then
    raise exception using
      errcode = '55000',
      message = 'upload_not_active';
  end if;
  if upload_record.expires_at <= p_completed_at then
    raise exception using
      errcode = '55000',
      message = 'upload_expired';
  end if;

  select
    count(*)::integer,
    max(mup.part_number),
    coalesce(sum(mup.byte_size), 0)
  into part_count, largest_part, total_bytes
  from public.media_upload_parts mup
  where mup.upload_id = upload_record.id;

  if part_count = 0
    or largest_part <> part_count
    or total_bytes <> upload_record.expected_byte_size
    or exists (
      select 1
      from public.media_upload_parts mup
      where mup.upload_id = upload_record.id
        and mup.part_number < largest_part
        and mup.byte_size < 5242880
    )
    or exists (
      select 1
      from public.media_upload_parts mup
      where mup.upload_id = upload_record.id
        and mup.part_number < largest_part
        and mup.byte_size <> (
          select first_part.byte_size
          from public.media_upload_parts first_part
          where first_part.upload_id = upload_record.id
            and first_part.part_number = 1
        )
    )
    or exists (
      select 1
      from public.media_upload_parts final_part
      join public.media_upload_parts first_part
        on first_part.upload_id = final_part.upload_id
        and first_part.part_number = 1
      where final_part.upload_id = upload_record.id
        and final_part.part_number = largest_part
        and largest_part > 1
        and final_part.byte_size > first_part.byte_size
    )
  then
    raise exception using
      errcode = '55000',
      message = 'upload_incomplete';
  end if;

  update public.media_objects mo
  set byte_size = upload_record.expected_byte_size,
      sha256 = p_sha256,
      upload_status = 'ready',
      uploaded_at = p_completed_at
  where mo.id = upload_record.media_id
    and mo.report_item_id = upload_record.report_item_id
    and mo.object_key = upload_record.object_key;

  get diagnostics affected_rows = row_count;
  if affected_rows <> 1 then
    raise exception using
      errcode = '55000',
      message = 'upload_not_active';
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
    p_actor_id,
    'media.upload_completed',
    upload_record.report_id,
    'media_object',
    upload_record.media_id,
    jsonb_build_object(
      'upload_id', upload_record.id,
      'publisher_device_id', p_publisher_device_id,
      'byte_size', upload_record.expected_byte_size
    ),
    p_completed_at
  );

  return query
  select
    mo.id,
    upload_record.report_id,
    mo.object_key,
    mo.content_type,
    mo.byte_size
  from public.media_objects mo
  where mo.id = upload_record.media_id;
end
$$;

create function public.abort_media_upload(
  p_upload_id uuid,
  p_actor_id uuid,
  p_publisher_device_id uuid,
  p_aborted_at timestamptz,
  p_cleanup boolean
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  upload_record public.media_uploads%rowtype;
begin
  if not p_cleanup then
    perform public.assert_media_uploader(p_actor_id, p_publisher_device_id);
  elsif p_actor_id is not null or p_publisher_device_id is not null then
    raise exception using
      errcode = '42501',
      message = 'owner_required';
  end if;

  select mu.*
  into upload_record
  from public.media_uploads mu
  where mu.id = p_upload_id
  for update;

  if not found
    or (
      not p_cleanup
      and p_actor_id is null
      and upload_record.publisher_device_id <> p_publisher_device_id
    )
    or (
      p_cleanup
      and (
        upload_record.expires_at > p_aborted_at
        or upload_record.status not in ('creating', 'uploading')
      )
    )
  then
    raise exception using
      errcode = 'P0002',
      message = 'upload_not_found';
  end if;

  if upload_record.status = 'completed' then
    raise exception using
      errcode = '55000',
      message = 'upload_completed';
  end if;

  if upload_record.status = 'aborted' then
    return public.media_upload_payload(upload_record.id);
  end if;

  update public.media_uploads mu
  set status = 'aborted',
      aborted_at = p_aborted_at,
      updated_at = p_aborted_at
  where mu.id = upload_record.id;

  update public.media_objects mo
  set upload_status = 'failed'
  where mo.id = upload_record.media_id
    and mo.object_key = upload_record.object_key
    and mo.upload_status in ('pending', 'uploading');

  insert into public.audit_logs (
    actor_id,
    event_type,
    report_id,
    target_type,
    target_id,
    details,
    created_at
  ) values (
    p_actor_id,
    'media.upload_aborted',
    upload_record.report_id,
    'media_upload',
    upload_record.id,
    jsonb_build_object(
      'cleanup', p_cleanup,
      'publisher_device_id', upload_record.publisher_device_id
    ),
    p_aborted_at
  );

  return public.media_upload_payload(upload_record.id);
end
$$;

create function public.list_stale_media_uploads(p_stale_at timestamptz)
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
  expires_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
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
    mu.expires_at
  from public.media_uploads mu
  where mu.status in ('creating', 'uploading')
    and mu.expires_at <= p_stale_at
  order by mu.expires_at, mu.id
  limit 100
$$;

alter table public.media_uploads enable row level security;
alter table public.media_upload_parts enable row level security;

revoke all on public.media_uploads from anon, authenticated, service_role;
revoke all on public.media_upload_parts from anon, authenticated, service_role;
revoke all on function public.assert_media_uploader(uuid, uuid) from public;
revoke all on function public.media_upload_payload(uuid) from public;
revoke all on function public.authorize_media_read(
  uuid,
  uuid,
  uuid,
  uuid,
  timestamptz
) from public;
revoke all on function public.begin_media_upload(
  uuid,
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  bigint,
  timestamptz
) from public;
revoke all on function public.attach_media_upload(
  uuid,
  uuid,
  uuid,
  text,
  timestamptz
) from public;
revoke all on function public.read_media_upload(uuid, uuid, uuid) from public;
revoke all on function public.record_media_upload_part(
  uuid,
  uuid,
  uuid,
  integer,
  text,
  bigint,
  timestamptz
) from public;
revoke all on function public.complete_media_upload(
  uuid,
  uuid,
  uuid,
  text,
  timestamptz
) from public;
revoke all on function public.abort_media_upload(
  uuid,
  uuid,
  uuid,
  timestamptz,
  boolean
) from public;
revoke all on function public.list_stale_media_uploads(timestamptz)
from public;

grant execute on function public.authorize_media_read(
  uuid,
  uuid,
  uuid,
  uuid,
  timestamptz
) to service_role;
grant execute on function public.begin_media_upload(
  uuid,
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  bigint,
  timestamptz
) to service_role;
grant execute on function public.attach_media_upload(
  uuid,
  uuid,
  uuid,
  text,
  timestamptz
) to service_role;
grant execute on function public.read_media_upload(uuid, uuid, uuid)
to service_role;
grant execute on function public.record_media_upload_part(
  uuid,
  uuid,
  uuid,
  integer,
  text,
  bigint,
  timestamptz
) to service_role;
grant execute on function public.complete_media_upload(
  uuid,
  uuid,
  uuid,
  text,
  timestamptz
) to service_role;
grant execute on function public.abort_media_upload(
  uuid,
  uuid,
  uuid,
  timestamptz,
  boolean
) to service_role;
grant execute on function public.list_stale_media_uploads(timestamptz)
to service_role;
