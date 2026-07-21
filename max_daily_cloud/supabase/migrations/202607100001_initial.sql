create type public.app_role as enum ('owner', 'editor', 'viewer');
create type public.report_status as enum ('draft', 'published', 'withdrawn');

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null unique check (email = lower(trim(email))),
  global_role public.app_role not null default 'viewer',
  created_at timestamptz not null default now()
);
create unique index one_owner_only on public.profiles ((global_role))
  where global_role = 'owner';

create table public.reports (
  id uuid primary key default gen_random_uuid(),
  daily_date date not null,
  source_table_id text not null,
  status public.report_status not null default 'draft',
  draft_version integer not null default 1,
  published_version integer not null default 0,
  published_at timestamptz,
  unique (daily_date, source_table_id)
);

create table public.report_items (
  id uuid primary key default gen_random_uuid(),
  report_id uuid not null references public.reports(id) on delete cascade,
  local_record_id text not null,
  title text not null default '',
  caption text not null default '',
  source_url text not null default '',
  max_daily_card text not null default '',
  max_feedback text not null default '',
  review_status text not null default '',
  item_order integer not null default 0,
  version integer not null default 1,
  unique (report_id, local_record_id)
);

create table public.media_objects (
  id uuid primary key default gen_random_uuid(),
  report_item_id uuid not null unique
    references public.report_items(id) on delete cascade,
  object_key text not null unique,
  content_type text not null,
  byte_size bigint not null default 0 check (byte_size >= 0),
  sha256 text not null default '',
  upload_status text not null default 'pending'
    check (upload_status in ('pending', 'uploading', 'ready', 'failed')),
  uploaded_at timestamptz,
  created_at timestamptz not null default now()
);

create table public.report_members (
  report_id uuid not null references public.reports(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role public.app_role not null check (role in ('editor', 'viewer')),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  primary key (report_id, user_id)
);

create table public.invitations (
  id uuid primary key default gen_random_uuid(),
  report_id uuid not null references public.reports(id) on delete cascade,
  email text not null check (email = lower(trim(email))),
  role public.app_role not null check (role in ('editor', 'viewer')),
  token_hash text not null unique,
  expires_at timestamptz not null,
  used_at timestamptz,
  revoked_at timestamptz,
  created_by uuid not null references auth.users(id) on delete cascade,
  claimed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table public.share_links (
  id uuid primary key default gen_random_uuid(),
  report_id uuid not null references public.reports(id) on delete cascade,
  token_hash text not null unique,
  expires_at timestamptz,
  revoked_at timestamptz,
  created_by uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

create table public.revisions (
  id bigint generated always as identity primary key,
  report_item_id uuid not null references public.report_items(id) on delete cascade,
  field_name text not null
    check (field_name in ('max_daily_card', 'max_feedback', 'review_status')),
  old_value text not null default '',
  new_value text not null default '',
  item_version integer not null check (item_version > 0),
  actor_id uuid not null references auth.users(id) on delete restrict,
  created_at timestamptz not null default now()
);

create table public.audit_logs (
  id bigint generated always as identity primary key,
  actor_id uuid references auth.users(id) on delete set null,
  event_type text not null,
  report_id uuid references public.reports(id) on delete set null,
  target_type text not null default '',
  target_id uuid,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.publisher_devices (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  token_hash text not null unique,
  created_by uuid not null references auth.users(id) on delete cascade,
  last_used_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create index report_items_report_order_idx
  on public.report_items (report_id, item_order, id);
create index report_members_user_idx
  on public.report_members (user_id, report_id);
create index invitations_report_idx
  on public.invitations (report_id, created_at desc);
create index share_links_report_idx
  on public.share_links (report_id, created_at desc);
create index revisions_item_idx
  on public.revisions (report_item_id, created_at desc);
create index audit_logs_report_idx
  on public.audit_logs (report_id, created_at desc);

create function public.current_role(report_id uuid)
returns public.app_role
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when exists (
      select 1
      from public.profiles p
      where p.id = auth.uid()
        and p.global_role = 'owner'
    ) then 'owner'::public.app_role
    else (
      select rm.role
      from public.report_members rm
      where rm.report_id = $1
        and rm.user_id = auth.uid()
    )
  end
$$;

create function public.claim_owner()
returns public.profiles
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  claimant_id uuid := auth.uid();
  claimant_email text;
  existing_owner uuid;
  claimed_profile public.profiles;
begin
  if claimant_id is null then
    raise exception using
      errcode = '28000',
      message = 'authentication required';
  end if;

  select lower(trim(u.email))
  into claimant_email
  from auth.users u
  where u.id = claimant_id;

  if claimant_email is null or claimant_email = '' then
    raise exception using
      errcode = '22023',
      message = 'verified email required';
  end if;

  lock table public.profiles in share row exclusive mode;

  select p.id
  into existing_owner
  from public.profiles p
  where p.global_role = 'owner';

  if existing_owner is not null and existing_owner <> claimant_id then
    raise exception using
      errcode = '42501',
      message = 'owner already claimed';
  end if;

  insert into public.profiles (id, email, global_role)
  values (claimant_id, claimant_email, 'owner')
  on conflict (id) do update
    set email = excluded.email,
        global_role = 'owner'
  returning *
  into claimed_profile;

  return claimed_profile;
end
$$;

create function public.can_edit_item(item_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select coalesce((
    select public.current_role(ri.report_id) in ('owner', 'editor')
    from public.report_items ri
    where ri.id = $1
  ), false)
$$;

alter table public.profiles enable row level security;
alter table public.reports enable row level security;
alter table public.report_items enable row level security;
alter table public.media_objects enable row level security;
alter table public.report_members enable row level security;
alter table public.invitations enable row level security;
alter table public.share_links enable row level security;
alter table public.revisions enable row level security;
alter table public.audit_logs enable row level security;
alter table public.publisher_devices enable row level security;

create policy profiles_select_self
on public.profiles
for select
to authenticated
using (id = auth.uid());

create policy reports_select_members
on public.reports
for select
to authenticated
using (
  public.current_role(id) in ('owner', 'editor')
  or (
    status = 'published'
    and public.current_role(id) = 'viewer'
  )
);

create policy report_items_select_members
on public.report_items
for select
to authenticated
using (
  public.current_role(report_id) in ('owner', 'editor')
  or (
    public.current_role(report_id) = 'viewer'
    and exists (
      select 1
      from public.reports r
      where r.id = report_items.report_id
        and r.status = 'published'
    )
  )
);

create policy media_objects_select_members
on public.media_objects
for select
to authenticated
using (
  exists (
    select 1
    from public.report_items ri
    join public.reports r on r.id = ri.report_id
    where ri.id = media_objects.report_item_id
      and (
        public.current_role(ri.report_id) in ('owner', 'editor')
        or (
          public.current_role(ri.report_id) = 'viewer'
          and r.status = 'published'
        )
      )
  )
);

create policy report_members_select_members
on public.report_members
for select
to authenticated
using (
  user_id = auth.uid()
  or public.current_role(report_id) = 'owner'
);

create policy invitations_select_owner
on public.invitations
for select
to authenticated
using (public.current_role(report_id) = 'owner');

create policy share_links_select_owner
on public.share_links
for select
to authenticated
using (public.current_role(report_id) = 'owner');

create policy revisions_select_members
on public.revisions
for select
to authenticated
using (
  exists (
    select 1
    from public.report_items ri
    join public.reports r on r.id = ri.report_id
    where ri.id = revisions.report_item_id
      and (
        public.current_role(ri.report_id) in ('owner', 'editor')
        or (
          public.current_role(ri.report_id) = 'viewer'
          and r.status = 'published'
        )
      )
  )
);

create policy audit_logs_select_owner
on public.audit_logs
for select
to authenticated
using (
  exists (
    select 1
    from public.profiles p
    where p.id = auth.uid()
      and p.global_role = 'owner'
  )
);

create policy publisher_devices_select_owner
on public.publisher_devices
for select
to authenticated
using (
  exists (
    select 1
    from public.profiles p
    where p.id = auth.uid()
      and p.global_role = 'owner'
  )
);

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on function public.current_role(uuid) from public;
revoke all on function public.claim_owner() from public;
revoke all on function public.can_edit_item(uuid) from public;

grant usage on schema public to authenticated, service_role;
grant usage on type public.app_role, public.report_status to authenticated, service_role;
grant select on
  public.profiles,
  public.reports,
  public.report_items,
  public.media_objects,
  public.report_members,
  public.invitations,
  public.share_links,
  public.revisions,
  public.audit_logs,
  public.publisher_devices
to authenticated;
grant execute on function public.current_role(uuid) to authenticated, service_role;
grant execute on function public.claim_owner() to service_role;
grant execute on function public.can_edit_item(uuid) to authenticated, service_role;

grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
