create table public.portal_collaboration_links (
  id uuid primary key default gen_random_uuid(),
  token_hash text not null unique,
  session_version integer not null default 1 check (session_version > 0),
  created_by uuid not null references auth.users(id) on delete restrict,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);

create unique index one_active_portal_collaboration_link
  on public.portal_collaboration_links ((revoked_at is null))
  where revoked_at is null;

alter table public.portal_collaboration_links enable row level security;

-- Fixed-link edits are system actors and are attributed by link id in audit details.
alter table public.revisions
  alter column actor_id drop not null;

create function public.replace_active_portal_collaboration_link(
  p_id uuid,
  p_token_hash text,
  p_created_by uuid,
  p_created_at timestamptz
)
returns public.portal_collaboration_links
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  created_link public.portal_collaboration_links%rowtype;
begin
  if p_id is null
    or p_token_hash is null
    or p_token_hash = ''
    or p_created_by is null
    or p_created_at is null
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_collaboration_link';
  end if;

  if not exists (
    select 1
    from public.profiles p
    where p.id = p_created_by
      and p.global_role = 'owner'
  ) then
    raise exception using
      errcode = '42501',
      message = 'owner_required';
  end if;

  lock table public.portal_collaboration_links in share row exclusive mode;

  update public.portal_collaboration_links pcl
  set revoked_at = p_created_at,
      session_version = pcl.session_version + 1
  where pcl.revoked_at is null;

  insert into public.portal_collaboration_links (
    id,
    token_hash,
    created_by,
    created_at
  ) values (
    p_id,
    p_token_hash,
    p_created_by,
    p_created_at
  )
  returning * into created_link;

  return created_link;
exception
  when unique_violation then
    raise exception using
      errcode = '23505',
      message = 'collaboration_link_conflict';
end
$$;

create function public.revoke_portal_collaboration_link(
  p_id uuid,
  p_revoked_at timestamptz
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  affected_rows integer;
begin
  if p_id is null or p_revoked_at is null then
    raise exception using
      errcode = '22023',
      message = 'invalid_collaboration_link';
  end if;

  update public.portal_collaboration_links pcl
  set revoked_at = p_revoked_at,
      session_version = pcl.session_version + 1
  where pcl.id = p_id
    and pcl.revoked_at is null;

  get diagnostics affected_rows = row_count;
  return affected_rows = 1;
end
$$;

create function public.touch_portal_collaboration_link(
  p_id uuid,
  p_expected_session_version integer,
  p_used_at timestamptz
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  affected_rows integer;
begin
  if p_id is null
    or p_expected_session_version is null
    or p_expected_session_version <= 0
    or p_used_at is null
    or not isfinite(p_used_at)
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_collaboration_link';
  end if;

  update public.portal_collaboration_links pcl
  set last_used_at = case
    when pcl.last_used_at is null or pcl.last_used_at < p_used_at
      then p_used_at
    else pcl.last_used_at
  end
  where pcl.id = p_id
    and pcl.revoked_at is null
    and pcl.session_version = p_expected_session_version;

  get diagnostics affected_rows = row_count;
  return affected_rows = 1;
end
$$;

create function public.list_published_reports_for_collaboration(
  p_link_id uuid,
  p_read_at timestamptz
)
returns table (
  id uuid,
  daily_date date,
  published_at timestamptz,
  item_count integer
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  perform pcl.id
  from public.portal_collaboration_links pcl
  where pcl.id = p_link_id
    and pcl.revoked_at is null
  for share;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'collaboration_link_unavailable';
  end if;

  return query
  select
    r.id,
    r.daily_date,
    rp.published_at,
    count(rpi.report_item_id)::integer
  from public.reports r
  join public.report_publications rp
    on rp.report_id = r.id
    and rp.published_version = r.published_version
  left join public.report_publication_items rpi
    on rpi.report_id = rp.report_id
    and rpi.published_version = rp.published_version
  where r.status = 'published'
  group by r.id, r.daily_date, rp.published_at
  order by r.daily_date desc, rp.published_at desc, r.id;
end
$$;

create function public.list_published_reports_for_owner(
  p_actor_id uuid,
  p_read_at timestamptz
)
returns table (
  id uuid,
  daily_date date,
  published_at timestamptz,
  item_count integer
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if p_actor_id is null
    or p_read_at is null
    or not isfinite(p_read_at)
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_owner_report_input';
  end if;

  if not exists (
    select 1
    from public.profiles p
    where p.id = p_actor_id
      and p.global_role = 'owner'
  ) then
    raise exception using
      errcode = '42501',
      message = 'owner_required';
  end if;

  return query
  select
    r.id,
    r.daily_date,
    rp.published_at,
    count(rpi.report_item_id)::integer
  from public.reports r
  join public.report_publications rp
    on rp.report_id = r.id
    and rp.published_version = r.published_version
  left join public.report_publication_items rpi
    on rpi.report_id = rp.report_id
    and rpi.published_version = rp.published_version
  where r.status = 'published'
  group by r.id, r.daily_date, rp.published_at
  order by r.daily_date desc, rp.published_at desc, r.id;
end
$$;

create function public.read_report_for_collaboration(
  p_link_id uuid,
  p_report_id uuid,
  p_read_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  items_json jsonb;
  report_record public.reports%rowtype;
begin
  perform pcl.id
  from public.portal_collaboration_links pcl
  where pcl.id = p_link_id
    and pcl.revoked_at is null
  for share;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'collaboration_link_unavailable';
  end if;

  select r.*
  into report_record
  from public.reports r
  join public.report_publications rp
    on rp.report_id = r.id
    and rp.published_version = r.published_version
  where r.id = p_report_id
    and r.status = 'published'
  for share of r;

  if not found then
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

create or replace function public.update_collaborative_field(
  p_item_id uuid,
  p_field_name text,
  p_value text,
  p_expected_version integer,
  p_actor_id uuid,
  p_changed_at timestamptz
)
returns table (
  id uuid,
  report_id uuid,
  local_record_id text,
  title text,
  caption text,
  source_url text,
  max_daily_card text,
  max_feedback text,
  review_status text,
  item_order integer,
  version integer,
  media_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  actor_role public.app_role;
  item_record public.report_items%rowtype;
  item_report_id uuid;
  old_value text;
  publication_item public.report_publication_items%rowtype;
  report_record public.reports%rowtype;
begin
  if p_field_name not in ('max_daily_card', 'max_feedback', 'review_status') then
    raise exception using
      errcode = '42501',
      message = 'field_not_editable';
  end if;

  lock table public.report_items in row exclusive mode;

  select ri.report_id
  into item_report_id
  from public.report_items ri
  where ri.id = p_item_id;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select r.*
  into report_record
  from public.reports r
  where r.id = item_report_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select ri.*
  into item_record
  from public.report_items ri
  where ri.id = p_item_id
    and ri.report_id = report_record.id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

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

  if actor_role = 'viewer' then
    if report_record.status = 'published'
      and exists (
        select 1
        from public.report_publication_items rpi
        where rpi.report_id = report_record.id
          and rpi.published_version = report_record.published_version
          and rpi.report_item_id = item_record.id
      )
    then
      raise exception using
        errcode = '42501',
        message = 'edit_forbidden';
    end if;

    raise exception using
      errcode = '42501',
      message = 'access_denied';
  end if;

  if actor_role is null or actor_role <> 'owner' and actor_role <> 'editor' then
    raise exception using
      errcode = '42501',
      message = 'access_denied';
  end if;

  if report_record.status = 'published' then
    select rpi.*
    into publication_item
    from public.report_publication_items rpi
    where rpi.report_id = report_record.id
      and rpi.published_version = report_record.published_version
      and rpi.report_item_id = item_record.id
    for update;

    if not found then
      raise exception using
        errcode = 'P0002',
        message = 'item_not_found';
    end if;
  end if;

  if item_record.version <> p_expected_version
    or (
      report_record.status = 'published'
      and publication_item.item_version <> p_expected_version
    )
  then
    raise exception using
      errcode = '40001',
      message = 'stale_version';
  end if;

  if p_field_name = 'max_daily_card' then
    old_value := item_record.max_daily_card;
    update public.report_items ri
    set max_daily_card = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    if report_record.status = 'published' then
      update public.report_publication_items rpi
      set max_daily_card = p_value,
          item_version = item_record.version
      where rpi.report_id = report_record.id
        and rpi.published_version = report_record.published_version
        and rpi.report_item_id = item_record.id;
    end if;
  elsif p_field_name = 'max_feedback' then
    old_value := item_record.max_feedback;
    update public.report_items ri
    set max_feedback = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    if report_record.status = 'published' then
      update public.report_publication_items rpi
      set max_feedback = p_value,
          item_version = item_record.version
      where rpi.report_id = report_record.id
        and rpi.published_version = report_record.published_version
        and rpi.report_item_id = item_record.id;
    end if;
  else
    old_value := item_record.review_status;
    update public.report_items ri
    set review_status = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    if report_record.status = 'published' then
      update public.report_publication_items rpi
      set review_status = p_value,
          item_version = item_record.version
      where rpi.report_id = report_record.id
        and rpi.published_version = report_record.published_version
        and rpi.report_item_id = item_record.id;
    end if;
  end if;

  insert into public.revisions (
    report_item_id,
    field_name,
    old_value,
    new_value,
    item_version,
    actor_id,
    created_at
  ) values (
    item_record.id,
    p_field_name,
    old_value,
    p_value,
    item_record.version,
    p_actor_id,
    p_changed_at
  );

  update public.reports r
  set draft_version = r.draft_version + 1
  where r.id = report_record.id;

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
    'report_item.updated',
    report_record.id,
    'report_item',
    item_record.id,
    jsonb_build_object(
      'field_name', p_field_name,
      'item_version', item_record.version
    ),
    p_changed_at
  );

  return query
  select
    item_record.id,
    item_record.report_id,
    item_record.local_record_id,
    item_record.title,
    item_record.caption,
    item_record.source_url,
    item_record.max_daily_card,
    item_record.max_feedback,
    item_record.review_status,
    item_record.item_order,
    item_record.version,
    (
      select mo.id
      from public.media_objects mo
      where mo.report_item_id = item_record.id
    );
end
$$;

create function public.update_collaborative_field_for_link(
  p_link_id uuid,
  p_item_id uuid,
  p_field_name text,
  p_value text,
  p_expected_version integer,
  p_changed_at timestamptz
)
returns table (
  id uuid,
  report_id uuid,
  local_record_id text,
  title text,
  caption text,
  source_url text,
  max_daily_card text,
  max_feedback text,
  review_status text,
  item_order integer,
  version integer,
  media_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  item_record public.report_items%rowtype;
  item_report_id uuid;
  old_value text;
  publication_item public.report_publication_items%rowtype;
  report_record public.reports%rowtype;
begin
  if p_link_id is null
    or p_item_id is null
    or p_field_name is null
    or p_value is null
    or p_expected_version is null
    or p_expected_version <= 0
    or p_changed_at is null
    or not isfinite(p_changed_at)
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_collaboration_input';
  end if;

  if p_field_name not in ('max_daily_card', 'max_feedback', 'review_status') then
    raise exception using
      errcode = '42501',
      message = 'field_not_editable';
  end if;

  if p_field_name = 'max_daily_card' and length(p_value) > 50000
    or p_field_name = 'max_feedback' and length(p_value) > 20000
    or p_field_name = 'review_status' and length(p_value) > 128
  then
    raise exception using
      errcode = '22001',
      message = 'collaborative_value_too_long';
  end if;

  perform pcl.id
  from public.portal_collaboration_links pcl
  where pcl.id = p_link_id
    and pcl.revoked_at is null
  for share;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'collaboration_link_unavailable';
  end if;

  lock table public.report_items in row exclusive mode;

  select ri.report_id
  into item_report_id
  from public.report_items ri
  where ri.id = p_item_id;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select r.*
  into report_record
  from public.reports r
  where r.id = item_report_id
  for update;

  if not found or report_record.status <> 'published' then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select ri.*
  into item_record
  from public.report_items ri
  where ri.id = p_item_id
    and ri.report_id = report_record.id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select rpi.*
  into publication_item
  from public.report_publication_items rpi
  where rpi.report_id = report_record.id
    and rpi.published_version = report_record.published_version
    and rpi.report_item_id = item_record.id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  if item_record.version <> p_expected_version
    or publication_item.item_version <> p_expected_version
  then
    raise exception using
      errcode = '40001',
      message = 'stale_version';
  end if;

  if p_field_name = 'max_daily_card' then
    old_value := item_record.max_daily_card;
    update public.report_items ri
    set max_daily_card = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    update public.report_publication_items rpi
    set max_daily_card = p_value,
        item_version = item_record.version
    where rpi.report_id = report_record.id
      and rpi.published_version = report_record.published_version
      and rpi.report_item_id = item_record.id;
  elsif p_field_name = 'max_feedback' then
    old_value := item_record.max_feedback;
    update public.report_items ri
    set max_feedback = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    update public.report_publication_items rpi
    set max_feedback = p_value,
        item_version = item_record.version
    where rpi.report_id = report_record.id
      and rpi.published_version = report_record.published_version
      and rpi.report_item_id = item_record.id;
  else
    old_value := item_record.review_status;
    update public.report_items ri
    set review_status = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;

    update public.report_publication_items rpi
    set review_status = p_value,
        item_version = item_record.version
    where rpi.report_id = report_record.id
      and rpi.published_version = report_record.published_version
      and rpi.report_item_id = item_record.id;
  end if;

  insert into public.revisions (
    report_item_id,
    field_name,
    old_value,
    new_value,
    item_version,
    actor_id,
    created_at
  ) values (
    item_record.id,
    p_field_name,
    old_value,
    p_value,
    item_record.version,
    null,
    p_changed_at
  );

  update public.reports r
  set draft_version = r.draft_version + 1
  where r.id = report_record.id;

  insert into public.audit_logs (
    actor_id,
    event_type,
    report_id,
    target_type,
    target_id,
    details,
    created_at
  ) values (
    null,
    'report_item.updated',
    report_record.id,
    'report_item',
    item_record.id,
    jsonb_build_object(
      'field_name', p_field_name,
      'item_version', item_record.version,
      'collaboration_link_id', p_link_id
    ),
    p_changed_at
  );

  return query
  select
    item_record.id,
    item_record.report_id,
    item_record.local_record_id,
    item_record.title,
    item_record.caption,
    item_record.source_url,
    item_record.max_daily_card,
    item_record.max_feedback,
    item_record.review_status,
    item_record.item_order,
    item_record.version,
    publication_item.media_id;
end
$$;

create function public.authorize_media_for_collaboration(
  p_link_id uuid,
  p_media_id uuid,
  p_read_at timestamptz
)
returns table (
  id uuid,
  report_id uuid,
  object_key text,
  content_type text,
  byte_size bigint,
  daily_date date
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  perform pcl.id
  from public.portal_collaboration_links pcl
  where pcl.id = p_link_id
    and pcl.revoked_at is null
  for share;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'collaboration_link_unavailable';
  end if;

  return query
  select
    rpi.media_id,
    rpi.report_id,
    rpi.media_object_key,
    rpi.media_content_type,
    rpi.media_byte_size,
    r.daily_date
  from public.report_publication_items rpi
  join public.reports r
    on r.id = rpi.report_id
    and r.status = 'published'
    and r.published_version = rpi.published_version
  join public.report_publications rp
    on rp.report_id = rpi.report_id
    and rp.published_version = rpi.published_version
  where rpi.media_id = p_media_id
    and rpi.media_upload_status = 'ready'
    and rpi.media_byte_size > 0;

  if found then return; end if;

  raise exception using
    errcode = 'P0002',
    message = 'media_not_found';
end
$$;

revoke all on public.portal_collaboration_links
from public, anon, authenticated, service_role;
grant select on public.portal_collaboration_links to service_role;

revoke all on function public.replace_active_portal_collaboration_link(
  uuid,
  text,
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.revoke_portal_collaboration_link(
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.touch_portal_collaboration_link(
  uuid,
  integer,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.list_published_reports_for_collaboration(
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.list_published_reports_for_owner(
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.read_report_for_collaboration(
  uuid,
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.update_collaborative_field_for_link(
  uuid,
  uuid,
  text,
  text,
  integer,
  timestamptz
) from public, anon, authenticated, service_role;
revoke all on function public.authorize_media_for_collaboration(
  uuid,
  uuid,
  timestamptz
) from public, anon, authenticated, service_role;

grant execute on function public.replace_active_portal_collaboration_link(
  uuid,
  text,
  uuid,
  timestamptz
) to service_role;
grant execute on function public.revoke_portal_collaboration_link(
  uuid,
  timestamptz
) to service_role;
grant execute on function public.touch_portal_collaboration_link(
  uuid,
  integer,
  timestamptz
) to service_role;
grant execute on function public.list_published_reports_for_collaboration(
  uuid,
  timestamptz
) to service_role;
grant execute on function public.list_published_reports_for_owner(
  uuid,
  timestamptz
) to service_role;
grant execute on function public.read_report_for_collaboration(
  uuid,
  uuid,
  timestamptz
) to service_role;
grant execute on function public.update_collaborative_field_for_link(
  uuid,
  uuid,
  text,
  text,
  integer,
  timestamptz
) to service_role;
grant execute on function public.authorize_media_for_collaboration(
  uuid,
  uuid,
  timestamptz
) to service_role;
