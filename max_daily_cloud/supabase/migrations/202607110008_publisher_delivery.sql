create function public.authenticate_publisher_device(
  p_token_hash text,
  p_used_at timestamptz
)
returns table (
  device_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  device_record public.publisher_devices;
begin
  if length(trim(coalesce(p_token_hash, ''))) <> 64 then
    raise exception using
      errcode = '42501',
      message = 'publisher_device_unavailable';
  end if;

  select pd.*
  into device_record
  from public.publisher_devices pd
  join public.profiles p
    on p.id = pd.created_by
    and p.global_role = 'owner'
  where pd.token_hash = p_token_hash
    and pd.revoked_at is null
  for update of pd;

  if not found then
    raise exception using
      errcode = '42501',
      message = 'publisher_device_unavailable';
  end if;

  update public.publisher_devices
  set last_used_at = p_used_at
  where id = device_record.id;

  device_id := device_record.id;
  return next;
end
$$;

grant execute on function public.authenticate_publisher_device(text, timestamptz)
to service_role;

create function public.publisher_report_payload(p_report_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', r.id,
    'daily_date', r.daily_date,
    'status', r.status,
    'draft_version', r.draft_version,
    'published_version', r.published_version,
    'published_at', r.published_at,
    'items', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'id', ri.id,
          'local_record_id', ri.local_record_id,
          'title', ri.title,
          'caption', ri.caption,
          'source_url', ri.source_url,
          'max_daily_card', ri.max_daily_card,
          'max_feedback', ri.max_feedback,
          'review_status', ri.review_status,
          'version', ri.version,
          'media_id', mo.id
        )
        order by ri.item_order, ri.id
      )
      from public.report_items ri
      left join public.media_objects mo on mo.report_item_id = ri.id
      where ri.report_id = r.id
    ), '[]'::jsonb)
  )
  from public.reports r
  where r.id = p_report_id
$$;

create function public.upsert_publisher_draft(
  p_daily_date date,
  p_source_table_id text,
  p_items jsonb,
  p_publisher_device_id uuid,
  p_upserted_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  item_record jsonb;
  report_record public.reports%rowtype;
  report_item_record public.report_items%rowtype;
  media_id_value uuid;
begin
  perform public.assert_media_uploader(null, p_publisher_device_id);

  if p_daily_date is null
    or length(trim(coalesce(p_source_table_id, ''))) = 0
    or jsonb_typeof(p_items) <> 'array'
    or jsonb_array_length(p_items) = 0
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_publisher_report';
  end if;

  insert into public.reports(daily_date, source_table_id)
  values (p_daily_date, p_source_table_id)
  on conflict (daily_date, source_table_id) do update
    set draft_version = public.reports.draft_version + 1,
        status = case
          when public.reports.status = 'withdrawn' then 'draft'::public.report_status
          else public.reports.status
        end
  returning * into report_record;

  for item_record in select * from jsonb_array_elements(p_items)
  loop
    if length(trim(coalesce(item_record->>'local_record_id', ''))) = 0 then
      raise exception using
        errcode = '22023',
        message = 'invalid_publisher_item';
    end if;

    insert into public.report_items(
      report_id,
      local_record_id,
      title,
      caption,
      source_url,
      max_daily_card,
      max_feedback,
      review_status,
      item_order
    ) values (
      report_record.id,
      item_record->>'local_record_id',
      coalesce(item_record->>'title', ''),
      coalesce(item_record->>'caption', ''),
      coalesce(item_record->>'source_url', ''),
      coalesce(item_record->>'max_daily_card', ''),
      coalesce(item_record->>'max_feedback', ''),
      coalesce(item_record->>'review_status', ''),
      coalesce((item_record->>'item_order')::integer, 0)
    )
    on conflict (report_id, local_record_id) do update
      set title = excluded.title,
          caption = excluded.caption,
          source_url = excluded.source_url,
          item_order = excluded.item_order
    returning * into report_item_record;

    select mo.id
    into media_id_value
    from public.media_objects mo
    where mo.report_item_id = report_item_record.id;

    if media_id_value is null then
      media_id_value := gen_random_uuid();
      insert into public.media_objects(
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
        media_id_value,
        report_item_record.id,
        format(
          'reports/%s/media/%s/pending',
          report_record.id,
          media_id_value
        ),
        'video/mp4',
        0,
        '',
        'pending',
        null,
        p_upserted_at
      );
    end if;
  end loop;

  return public.publisher_report_payload(report_record.id);
end
$$;

create function public.publish_publisher_report(
  p_report_id uuid,
  p_publisher_device_id uuid,
  p_expected_draft_version integer,
  p_published_at timestamptz
)
returns public.reports
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  owner_id uuid;
  report_record public.reports%rowtype;
begin
  perform public.assert_media_uploader(null, p_publisher_device_id);

  select pd.created_by
  into owner_id
  from public.publisher_devices pd
  join public.profiles p
    on p.id = pd.created_by
    and p.global_role = 'owner'
  where pd.id = p_publisher_device_id
    and pd.revoked_at is null;

  if owner_id is null then
    raise exception using
      errcode = '42501',
      message = 'publisher_device_unavailable';
  end if;

  select *
  into report_record
  from public.reports
  where id = p_report_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'report_not_found';
  end if;

  if report_record.draft_version <> p_expected_draft_version then
    raise exception using
      errcode = '55000',
      message = 'stale_version';
  end if;

  if report_record.status = 'published'
    and report_record.published_version > 0
    and exists (
      select 1
      from public.report_publications rp
      where rp.report_id = report_record.id
        and rp.published_version = report_record.published_version
        and rp.published_at <= p_published_at
    )
  then
    return report_record;
  end if;

  return (
    select published
    from public.publish_report(p_report_id, owner_id, p_published_at) as published
  );
end
$$;

grant execute on function public.publisher_report_payload(uuid) to service_role;
grant execute on function public.upsert_publisher_draft(date, text, jsonb, uuid, timestamptz)
to service_role;
grant execute on function public.publish_publisher_report(uuid, uuid, integer, timestamptz)
to service_role;
