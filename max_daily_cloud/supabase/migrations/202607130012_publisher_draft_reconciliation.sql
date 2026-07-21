alter table public.report_items
  add column publisher_removed_at timestamptz;

create or replace function public.publisher_report_payload(p_report_id uuid)
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
        and ri.publisher_removed_at is null
    ), '[]'::jsonb)
  )
  from public.reports r
  where r.id = p_report_id
$$;

create or replace function public.upsert_publisher_draft(
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

  with incoming_items as (
    select distinct trim(item->>'local_record_id') as local_record_id
    from jsonb_array_elements(p_items) as item
    where length(trim(coalesce(item->>'local_record_id', ''))) > 0
  )
  update public.report_items ri
  set publisher_removed_at = coalesce(ri.publisher_removed_at, p_upserted_at)
  where ri.report_id = report_record.id
    and ri.publisher_removed_at is null
    and not exists (
      select 1
      from incoming_items incoming
      where incoming.local_record_id = ri.local_record_id
    );

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
      item_order,
      publisher_removed_at
    ) values (
      report_record.id,
      item_record->>'local_record_id',
      coalesce(item_record->>'title', ''),
      coalesce(item_record->>'caption', ''),
      coalesce(item_record->>'source_url', ''),
      coalesce(item_record->>'max_daily_card', ''),
      coalesce(item_record->>'max_feedback', ''),
      coalesce(item_record->>'review_status', ''),
      coalesce((item_record->>'item_order')::integer, 0),
      null
    )
    on conflict (report_id, local_record_id) do update
      set title = excluded.title,
          caption = excluded.caption,
          source_url = excluded.source_url,
          item_order = excluded.item_order,
          publisher_removed_at = null
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

create or replace function public.publish_report(
  p_report_id uuid,
  p_actor_id uuid,
  p_published_at timestamptz
)
returns public.reports
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  next_published_version integer;
  report_record public.reports%rowtype;
begin
  lock table public.report_items in share mode;
  lock table public.media_objects in share mode;

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

  select r.*
  into report_record
  from public.reports r
  where r.id = p_report_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'report_not_found';
  end if;

  perform 1
  from public.report_items ri
  where ri.report_id = report_record.id
    and ri.publisher_removed_at is null
  order by ri.item_order, ri.id
  for update;

  if not found then
    raise exception using
      errcode = '23514',
      message = 'report_not_ready';
  end if;

  perform 1
  from public.media_objects mo
  join public.report_items ri
    on ri.id = mo.report_item_id
  where ri.report_id = report_record.id
    and ri.publisher_removed_at is null
  order by ri.item_order, ri.id
  for update of mo;

  if exists (
    select 1
    from public.report_items ri
    left join public.media_objects mo
      on mo.report_item_id = ri.id
    where ri.report_id = report_record.id
      and ri.publisher_removed_at is null
      and (
        mo.id is null
        or mo.upload_status <> 'ready'
        or mo.byte_size <= 0
        or trim(mo.object_key) = ''
        or trim(mo.content_type) = ''
        or trim(mo.sha256) = ''
        or mo.uploaded_at is null
      )
  ) then
    raise exception using
      errcode = '23514',
      message = 'report_not_ready';
  end if;

  next_published_version := report_record.published_version + 1;

  insert into public.report_publications (
    report_id,
    published_version,
    published_at,
    published_by
  ) values (
    report_record.id,
    next_published_version,
    p_published_at,
    p_actor_id
  );

  insert into public.report_publication_items (
    report_id,
    published_version,
    report_item_id,
    local_record_id,
    title,
    caption,
    source_url,
    max_daily_card,
    max_feedback,
    review_status,
    item_order,
    item_version,
    media_id,
    media_object_key,
    media_content_type,
    media_byte_size,
    media_sha256,
    media_upload_status,
    media_uploaded_at,
    media_created_at
  )
  select
    report_record.id,
    next_published_version,
    ri.id,
    ri.local_record_id,
    ri.title,
    ri.caption,
    ri.source_url,
    ri.max_daily_card,
    ri.max_feedback,
    ri.review_status,
    ri.item_order,
    ri.version,
    mo.id,
    mo.object_key,
    mo.content_type,
    mo.byte_size,
    mo.sha256,
    mo.upload_status,
    mo.uploaded_at,
    mo.created_at
  from public.report_items ri
  join public.media_objects mo
    on mo.report_item_id = ri.id
  where ri.report_id = report_record.id
    and ri.publisher_removed_at is null
  order by ri.item_order, ri.id;

  update public.reports r
  set status = 'published',
      published_version = next_published_version,
      published_at = p_published_at
  where r.id = report_record.id
  returning r.* into report_record;

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
    'report.published',
    report_record.id,
    'report',
    report_record.id,
    jsonb_build_object(
      'published_version', report_record.published_version
    ),
    p_published_at
  );

  return report_record;
end
$$;

grant execute on function public.publisher_report_payload(uuid) to service_role;
grant execute on function public.upsert_publisher_draft(date, text, jsonb, uuid, timestamptz)
to service_role;
grant execute on function public.publish_report(uuid, uuid, timestamptz)
to service_role;
