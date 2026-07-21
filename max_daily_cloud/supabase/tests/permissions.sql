begin;
select plan(25);

select has_table('public', 'reports', 'reports table exists');
select has_table('public', 'report_members', 'report_members table exists');
select has_table(
  'public',
  'report_publications',
  'immutable report publication table exists'
);
select has_table(
  'public',
  'report_publication_items',
  'immutable publication item table exists'
);
select has_table(
  'public',
  'media_uploads',
  'persistent multipart upload table exists'
);
select has_table(
  'public',
  'media_upload_parts',
  'persistent multipart part table exists'
);
select throws_ok(
  $$ insert into profiles(id, email, global_role) values
     ('00000000-0000-0000-0000-000000000001', 'one@example.com', 'owner'),
     ('00000000-0000-0000-0000-000000000002', 'two@example.com', 'owner') $$,
  '23505',
  null,
  'only one owner is allowed'
);
select function_returns('public', 'can_edit_item', array['uuid'], 'boolean');
select has_function(
  'public',
  'claim_invitation',
  array['text', 'uuid', 'text', 'timestamptz'],
  'atomic invitation claim RPC exists'
);
select has_function(
  'public',
  'update_collaborative_field',
  array['uuid', 'text', 'text', 'integer', 'uuid', 'timestamptz'],
  'atomic collaborative edit RPC exists'
);
select has_function(
  'public',
  'publish_report',
  array['uuid', 'uuid', 'timestamptz'],
  'atomic report publish RPC exists'
);
select has_function(
  'public',
  'restore_revision',
  array['bigint', 'integer', 'uuid', 'timestamptz'],
  'atomic revision restore RPC exists'
);
select has_function(
  'public',
  'read_report',
  array['uuid', 'uuid', 'uuid', 'timestamptz'],
  'atomic authorized report read RPC exists'
);
select has_function(
  'public',
  'authorize_media_read',
  array['uuid', 'uuid', 'uuid', 'uuid', 'timestamptz'],
  'atomic authorized media read RPC exists'
);
select has_function(
  'public',
  'begin_media_upload',
  array[
    'uuid',
    'uuid',
    'uuid',
    'uuid',
    'uuid',
    'text',
    'bigint',
    'timestamptz'
  ],
  'atomic media upload start RPC exists'
);
select has_function(
  'public',
  'claim_media_upload_completion',
  array['uuid', 'uuid', 'uuid', 'text', 'timestamptz'],
  'atomic media completion claim RPC exists'
);
select has_function(
  'public',
  'finalize_media_upload_completion',
  array['uuid', 'timestamptz'],
  'idempotent media completion finalizer exists'
);
select has_function(
  'public',
  'claim_media_upload_abort',
  array['uuid', 'uuid', 'uuid', 'timestamptz', 'boolean'],
  'atomic media abort claim RPC exists'
);
select has_function(
  'public',
  'claim_stale_media_upload_completion',
  array['uuid', 'timestamptz', 'timestamptz'],
  'stale media completion lease claim RPC exists'
);
select has_function(
  'public',
  'reset_stale_media_upload_completion',
  array['uuid', 'timestamptz', 'timestamptz', 'text'],
  'stale media completion reset RPC exists'
);
select has_function(
  'public',
  'list_media_upload_recovery_parts',
  array['uuid'],
  'stale media completion part recovery RPC exists'
);
select has_function(
  'public',
  'finalize_media_upload_abort',
  array['uuid', 'timestamptz'],
  'idempotent media abort finalizer exists'
);
select has_function(
  'public',
  'list_media_upload_recovery_page',
  array['timestamptz', 'uuid', 'integer'],
  'paginated media recovery RPC exists'
);
select ok(
  not has_function_privilege(
    'authenticated',
    'public.claim_owner(uuid,text)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.claim_owner(uuid,text)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.claim_invitation(text,uuid,text,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.claim_invitation(text,uuid,text,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.update_collaborative_field(uuid,text,text,integer,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.update_collaborative_field(uuid,text,text,integer,uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.publish_report(uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.publish_report(uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.read_report(uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.read_report(uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.restore_revision(bigint,integer,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.restore_revision(bigint,integer,uuid,timestamp with time zone)',
    'execute'
  ),
  'only the trusted service may run privileged collaboration RPCs'
);
select ok(
  not has_function_privilege(
    'authenticated',
    'public.read_report_with_access_role(uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.read_report_with_access_role(uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  ),
  'only the trusted service may read report access roles'
);
select ok(
  not has_table_privilege('authenticated', 'public.report_items', 'update')
  and not has_table_privilege(
    'authenticated',
    'public.report_publication_items',
    'select,insert,update,delete'
  )
  and not has_table_privilege(
    'authenticated',
    'public.report_publications',
    'select,insert,update,delete'
  )
  and has_table_privilege(
    'service_role',
    'public.report_publication_items',
    'select'
  )
  and not has_table_privilege(
    'service_role',
    'public.report_publication_items',
    'insert,update,delete'
  )
  and has_table_privilege(
    'service_role',
    'public.report_publications',
    'select'
  )
  and not has_table_privilege(
    'service_role',
    'public.report_publications',
    'insert,update,delete'
  )
  and not has_table_privilege('authenticated', 'public.revisions', 'insert')
  and not has_table_privilege('authenticated', 'public.audit_logs', 'insert')
  and not has_table_privilege('authenticated', 'public.reports', 'update'),
  'authenticated users have no direct collaboration writes'
);
select ok(
  not has_table_privilege(
    'authenticated',
    'public.media_uploads',
    'select,insert,update,delete'
  )
  and not has_table_privilege(
    'authenticated',
    'public.media_upload_parts',
    'select,insert,update,delete'
  )
  and not has_table_privilege(
    'service_role',
    'public.media_uploads',
    'select,insert,update,delete'
  )
  and not has_table_privilege(
    'service_role',
    'public.media_upload_parts',
    'select,insert,update,delete'
  )
  and not has_function_privilege(
    'authenticated',
    'public.authorize_media_read(uuid,uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.authorize_media_read(uuid,uuid,uuid,uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.begin_media_upload(uuid,uuid,uuid,uuid,uuid,text,bigint,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.begin_media_upload(uuid,uuid,uuid,uuid,uuid,text,bigint,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.claim_media_upload_completion(uuid,uuid,uuid,text,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.claim_media_upload_completion(uuid,uuid,uuid,text,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.finalize_media_upload_completion(uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.finalize_media_upload_completion(uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.claim_media_upload_abort(uuid,uuid,uuid,timestamp with time zone,boolean)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.claim_media_upload_abort(uuid,uuid,uuid,timestamp with time zone,boolean)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.claim_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.claim_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.reset_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone,text)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.reset_stale_media_upload_completion(uuid,timestamp with time zone,timestamp with time zone,text)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.list_media_upload_recovery_parts(uuid)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.list_media_upload_recovery_parts(uuid)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.finalize_media_upload_abort(uuid,timestamp with time zone)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.finalize_media_upload_abort(uuid,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'authenticated',
    'public.list_media_upload_recovery_page(timestamp with time zone,uuid,integer)',
    'execute'
  )
  and has_function_privilege(
    'service_role',
    'public.list_media_upload_recovery_page(timestamp with time zone,uuid,integer)',
    'execute'
  )
  and not has_function_privilege(
    'service_role',
    'public.complete_media_upload(uuid,uuid,uuid,text,timestamp with time zone)',
    'execute'
  )
  and not has_function_privilege(
    'service_role',
    'public.abort_media_upload(uuid,uuid,uuid,timestamp with time zone,boolean)',
    'execute'
  )
  and not has_function_privilege(
    'service_role',
    'public.list_stale_media_uploads(timestamp with time zone)',
    'execute'
  ),
  'media state stays private behind trusted service RPCs'
);

select * from finish();
rollback;
