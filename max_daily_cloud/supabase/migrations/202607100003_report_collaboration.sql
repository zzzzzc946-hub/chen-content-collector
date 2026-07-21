create table public.report_publications (
  report_id uuid not null references public.reports(id) on delete cascade,
  published_version integer not null check (published_version > 0),
  published_at timestamptz not null,
  published_by uuid references auth.users(id) on delete set null,
  primary key (report_id, published_version)
);

create table public.report_publication_items (
  report_id uuid not null,
  published_version integer not null,
  report_item_id uuid not null,
  local_record_id text not null,
  title text not null default '',
  caption text not null default '',
  source_url text not null default '',
  max_daily_card text not null default '',
  max_feedback text not null default '',
  review_status text not null default '',
  item_order integer not null default 0,
  item_version integer not null check (item_version > 0),
  media_id uuid not null,
  media_object_key text not null,
  media_content_type text not null,
  media_byte_size bigint not null check (media_byte_size > 0),
  media_sha256 text not null,
  media_upload_status text not null check (media_upload_status = 'ready'),
  media_uploaded_at timestamptz not null,
  media_created_at timestamptz not null,
  primary key (report_id, published_version, report_item_id),
  foreign key (report_id, published_version)
    references public.report_publications(report_id, published_version)
    on delete cascade
);

create index report_publication_items_order_idx
  on public.report_publication_items (
    report_id,
    published_version,
    item_order,
    report_item_id
  );

-- Historical snapshots cannot be reconstructed from mutable legacy draft rows.
update public.reports
set status = 'draft',
    published_at = null
where status = 'published';

create function public.update_collaborative_field(
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

  if item_record.version <> p_expected_version then
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
  elsif p_field_name = 'max_feedback' then
    old_value := item_record.max_feedback;
    update public.report_items ri
    set max_feedback = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;
  else
    old_value := item_record.review_status;
    update public.report_items ri
    set review_status = p_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;
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

create function public.publish_report(
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
  order by ri.item_order, ri.id
  for update of mo;

  if exists (
    select 1
    from public.report_items ri
    left join public.media_objects mo
      on mo.report_item_id = ri.id
    where ri.report_id = report_record.id
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

create function public.restore_revision(
  p_revision_id bigint,
  p_expected_version integer,
  p_actor_id uuid,
  p_restored_at timestamptz
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
  old_value text;
  report_record public.reports%rowtype;
  revision_record public.revisions%rowtype;
begin
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

  lock table public.report_items in row exclusive mode;

  select rv.*
  into revision_record
  from public.revisions rv
  where rv.id = p_revision_id;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'revision_not_found';
  end if;

  select r.*
  into report_record
  from public.reports r
  join public.report_items ri
    on ri.report_id = r.id
  where ri.id = revision_record.report_item_id
  for update of r;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  select ri.*
  into item_record
  from public.report_items ri
  where ri.id = revision_record.report_item_id
    and ri.report_id = report_record.id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'item_not_found';
  end if;

  if item_record.version <> p_expected_version then
    raise exception using
      errcode = '40001',
      message = 'stale_version';
  end if;

  if revision_record.field_name = 'max_daily_card' then
    old_value := item_record.max_daily_card;
    update public.report_items ri
    set max_daily_card = revision_record.old_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;
  elsif revision_record.field_name = 'max_feedback' then
    old_value := item_record.max_feedback;
    update public.report_items ri
    set max_feedback = revision_record.old_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;
  else
    old_value := item_record.review_status;
    update public.report_items ri
    set review_status = revision_record.old_value,
        version = ri.version + 1
    where ri.id = item_record.id
    returning ri.* into item_record;
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
    revision_record.field_name,
    old_value,
    revision_record.old_value,
    item_record.version,
    p_actor_id,
    p_restored_at
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
    'revision.restored',
    report_record.id,
    'report_item',
    item_record.id,
    jsonb_build_object(
      'field_name', revision_record.field_name,
      'item_version', item_record.version,
      'source_revision_id', revision_record.id
    ),
    p_restored_at
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

create function public.read_report(
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
    where ri.report_id = report_record.id;
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

drop policy report_items_select_members on public.report_items;
create policy report_items_select_editors
on public.report_items
for select
to authenticated
using (public.current_role(report_id) in ('owner', 'editor'));

drop policy media_objects_select_members on public.media_objects;
create policy media_objects_select_editors
on public.media_objects
for select
to authenticated
using (
  exists (
    select 1
    from public.report_items ri
    where ri.id = media_objects.report_item_id
      and public.current_role(ri.report_id) in ('owner', 'editor')
  )
);

drop policy revisions_select_members on public.revisions;
create policy revisions_select_editors
on public.revisions
for select
to authenticated
using (
  exists (
    select 1
    from public.report_items ri
    where ri.id = revisions.report_item_id
      and public.current_role(ri.report_id) in ('owner', 'editor')
  )
);

alter table public.report_publications enable row level security;
alter table public.report_publication_items enable row level security;

revoke all on public.report_publications
from anon, authenticated, service_role;
revoke all on public.report_publication_items
from anon, authenticated, service_role;
revoke all on function public.update_collaborative_field(
  uuid,
  text,
  text,
  integer,
  uuid,
  timestamptz
) from public;
revoke all on function public.publish_report(uuid, uuid, timestamptz) from public;
revoke all on function public.restore_revision(
  bigint,
  integer,
  uuid,
  timestamptz
) from public;
revoke all on function public.read_report(
  uuid,
  uuid,
  uuid,
  timestamptz
) from public;

grant select on
  public.report_publications,
  public.report_publication_items
to service_role;
grant execute on function public.update_collaborative_field(
  uuid,
  text,
  text,
  integer,
  uuid,
  timestamptz
) to service_role;
grant execute on function public.publish_report(uuid, uuid, timestamptz)
to service_role;
grant execute on function public.restore_revision(
  bigint,
  integer,
  uuid,
  timestamptz
) to service_role;
grant execute on function public.read_report(
  uuid,
  uuid,
  uuid,
  timestamptz
) to service_role;
