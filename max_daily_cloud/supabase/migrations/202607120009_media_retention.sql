alter table public.media_objects
  add column purge_after timestamptz,
  add column purged_at timestamptz,
  add column purge_attempted_at timestamptz,
  add column purge_error text;

update public.media_objects
set purge_after = uploaded_at + interval '72 hours'
where upload_status = 'ready'
  and uploaded_at is not null;

create function public.set_media_purge_after()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  if new.upload_status = 'ready'
    and new.uploaded_at is not null
  then
    new.purge_after := case
      when tg_op = 'UPDATE' then
        coalesce(old.purge_after, new.uploaded_at + interval '72 hours')
      else new.uploaded_at + interval '72 hours'
    end;
  end if;
  return new;
end
$$;

create trigger set_media_purge_after
before insert or update on public.media_objects
for each row execute function public.set_media_purge_after();

create index media_objects_purge_due_idx
on public.media_objects (purge_after, id)
where purged_at is null;

create function public.claim_expired_media_objects(
  p_now timestamptz,
  p_limit integer
)
returns table (
  id uuid,
  object_key text
)
language sql
security definer
set search_path = pg_catalog, public
as $$
  with due as (
    select mo.id
    from public.media_objects mo
    where mo.purge_after <= p_now
      and mo.purged_at is null
      and (
        mo.purge_attempted_at is null
        or mo.purge_attempted_at <= p_now - interval '1 hour'
      )
    order by mo.purge_after, mo.id
    limit least(greatest(p_limit, 0), 100)
    for update skip locked
  )
  update public.media_objects mo
  set purge_attempted_at = p_now
  from due
  where mo.id = due.id
  returning mo.id, mo.object_key
$$;

create function public.finish_media_object_purge(
  p_media_id uuid,
  p_purged_at timestamptz,
  p_error text
)
returns void
language sql
security definer
set search_path = pg_catalog, public
as $$
  update public.media_objects
  set purged_at = case
        when p_error is null and p_purged_at is not null then p_purged_at
        else null
      end,
      purge_error = p_error
  where id = p_media_id
    and purged_at is null
$$;

revoke all on function public.set_media_purge_after() from public;
revoke all on function public.claim_expired_media_objects(
  timestamptz,
  integer
) from public, anon, authenticated;
revoke all on function public.finish_media_object_purge(
  uuid,
  timestamptz,
  text
) from public, anon, authenticated;

grant execute on function public.claim_expired_media_objects(
  timestamptz,
  integer
) to service_role;
grant execute on function public.finish_media_object_purge(
  uuid,
  timestamptz,
  text
) to service_role;

drop function public.authorize_media_read(
  uuid,
  uuid,
  uuid,
  uuid,
  timestamptz
);

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
  byte_size bigint,
  daily_date date
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
        mo.byte_size,
        r.daily_date
      from public.media_objects mo
      join public.report_items ri
        on ri.id = mo.report_item_id
      join public.reports r
        on r.id = ri.report_id
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
      mo.byte_size,
      r.daily_date
    from public.media_objects mo
    join public.report_items ri
      on ri.id = mo.report_item_id
    join public.reports r
      on r.id = ri.report_id
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
      rpi.media_byte_size,
      r.daily_date
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
    rpi.media_byte_size,
    r.daily_date
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

revoke all on function public.authorize_media_read(
  uuid,
  uuid,
  uuid,
  uuid,
  timestamptz
) from public, anon, authenticated;

grant execute on function public.authorize_media_read(
  uuid,
  uuid,
  uuid,
  uuid,
  timestamptz
) to service_role;
