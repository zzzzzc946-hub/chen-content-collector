revoke all on function public.claim_owner() from public;
drop function public.claim_owner();

create function public.claim_owner(
  claimant_id uuid,
  claimant_email text
)
returns public.profiles
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  normalized_email text := lower(trim(claimant_email));
  verified_email text;
  existing_owner uuid;
  claimed_profile public.profiles;
begin
  if claimant_id is null then
    raise exception using
      errcode = '28000',
      message = 'confirmed_email_required';
  end if;

  select lower(trim(u.email))
  into verified_email
  from auth.users u
  where u.id = claimant_id
    and u.email_confirmed_at is not null;

  if verified_email is null or verified_email = '' then
    raise exception using
      errcode = '28000',
      message = 'confirmed_email_required';
  end if;

  if normalized_email = '' or normalized_email <> verified_email then
    raise exception using
      errcode = '42501',
      message = 'owner_email_mismatch';
  end if;

  lock table public.profiles in share row exclusive mode;

  select p.id
  into existing_owner
  from public.profiles p
  where p.global_role = 'owner';

  if existing_owner is not null and existing_owner <> claimant_id then
    raise exception using
      errcode = '42501',
      message = 'owner_already_claimed';
  end if;

  insert into public.profiles (id, email, global_role)
  values (claimant_id, verified_email, 'owner')
  on conflict (id) do update
    set email = excluded.email,
        global_role = 'owner'
  returning *
  into claimed_profile;

  return claimed_profile;
end
$$;

create function public.claim_invitation(
  p_token_hash text,
  p_claimant_id uuid,
  p_claimant_email text,
  p_claimed_at timestamptz
)
returns table (
  report_id uuid,
  member_role public.app_role
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  affected_rows integer;
  invitation_record public.invitations%rowtype;
  normalized_email text := lower(trim(p_claimant_email));
  verified_email text;
begin
  select lower(trim(u.email))
  into verified_email
  from auth.users u
  where u.id = p_claimant_id
    and u.email_confirmed_at is not null;

  if verified_email is null or verified_email = '' then
    raise exception using
      errcode = '28000',
      message = 'confirmed_email_required';
  end if;

  if normalized_email = '' or normalized_email <> verified_email then
    raise exception using
      errcode = '42501',
      message = 'identity_email_mismatch';
  end if;

  select i.*
  into invitation_record
  from public.invitations i
  where i.token_hash = p_token_hash
  for update;

  if not found
    or invitation_record.revoked_at is not null
    or invitation_record.used_at is not null
    or invitation_record.expires_at <= p_claimed_at
  then
    raise exception using
      errcode = 'P0002',
      message = 'invitation_unavailable';
  end if;

  if invitation_record.email <> verified_email then
    raise exception using
      errcode = '42501',
      message = 'invitation_email_mismatch';
  end if;

  insert into public.report_members (
    report_id,
    user_id,
    role,
    created_by
  )
  values (
    invitation_record.report_id,
    p_claimant_id,
    invitation_record.role,
    invitation_record.created_by
  )
  on conflict on constraint report_members_pkey do update
    set role = excluded.role,
        created_by = excluded.created_by;

  update public.invitations i
  set used_at = p_claimed_at,
      claimed_by = p_claimant_id
  where i.id = invitation_record.id
    and i.used_at is null
    and i.revoked_at is null
    and i.expires_at > p_claimed_at;

  get diagnostics affected_rows = row_count;
  if affected_rows <> 1 then
    raise exception using
      errcode = 'P0002',
      message = 'invitation_unavailable';
  end if;

  return query
  select invitation_record.report_id, invitation_record.role;
end
$$;

revoke all on function public.claim_owner(uuid, text) from public;
revoke all on function public.claim_invitation(
  text,
  uuid,
  text,
  timestamptz
) from public;

grant execute on function public.claim_owner(uuid, text) to service_role;
grant execute on function public.claim_invitation(
  text,
  uuid,
  text,
  timestamptz
) to service_role;
