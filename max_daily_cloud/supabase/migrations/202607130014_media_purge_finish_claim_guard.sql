create or replace function public.finish_media_object_purge(
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
    and purge_attempted_at is not null
    and purge_after <= purge_attempted_at
$$;

revoke all on function public.finish_media_object_purge(
  uuid,
  timestamptz,
  text
) from public, anon, authenticated;

grant execute on function public.finish_media_object_purge(
  uuid,
  timestamptz,
  text
) to service_role;
