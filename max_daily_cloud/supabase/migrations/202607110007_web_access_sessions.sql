create function public.read_report_with_access_role(
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
  report_json jsonb;
begin
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
      where rm.report_id = p_report_id
        and rm.user_id = p_actor_id
      for share;
    end if;

    if actor_role is null then
      raise exception using
        errcode = 'P0002',
        message = 'report_not_found';
    end if;
  end if;

  report_json := public.read_report(
    p_report_id,
    p_actor_id,
    p_share_link_id,
    p_read_at
  );

  return report_json || jsonb_build_object(
    'access_role',
    case
      when p_actor_id is null then 'public_reader'
      else actor_role::text
    end
  );
end
$$;

revoke all on function public.read_report_with_access_role(
  uuid,
  uuid,
  uuid,
  timestamptz
) from public;

grant execute on function public.read_report_with_access_role(
  uuid,
  uuid,
  uuid,
  timestamptz
) to service_role;
