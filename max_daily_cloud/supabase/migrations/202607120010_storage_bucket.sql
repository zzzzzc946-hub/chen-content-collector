insert into storage.buckets (id, name, public)
values ('max-daily-media', 'max-daily-media', false)
on conflict (id) do update
set name = excluded.name,
    public = false;
