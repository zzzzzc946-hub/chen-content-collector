update storage.buckets
set file_size_limit = 209715200,
    public = false
where id = 'max-daily-media';
