begin;

do $$
declare
  function_definition text;
  function_signature text;
  old_fragment constant text := 'errcode = ''40001''';
  new_fragment constant text := 'errcode = ''P0001''';
  occurrence_count integer;
begin
  foreach function_signature in array array[
    'public.update_collaborative_field(uuid,text,text,integer,uuid,timestamptz)',
    'public.update_collaborative_field_for_link(uuid,uuid,text,text,integer,timestamptz)'
  ] loop
    select pg_catalog.pg_get_functiondef(function_signature::regprocedure)
    into function_definition;

    occurrence_count := (
      length(function_definition)
      - length(replace(function_definition, old_fragment, ''))
    ) / length(old_fragment);

    if occurrence_count <> 1 then
      raise exception using
        errcode = 'P0001',
        message = 'unexpected_collaboration_function_definition';
    end if;

    execute replace(function_definition, old_fragment, new_fragment);
  end loop;
end
$$;

commit;
