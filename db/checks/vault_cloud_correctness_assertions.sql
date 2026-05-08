DO $$
DECLARE
  required_entry_columns text[] := ARRAY[
    'file_hash',
    'chunk_count',
    'embedding_model',
    'chunker_version',
    'index_error',
    'uploaded_at',
    'indexed_at',
    'owner_user_id',
    'organization_id',
    'project_id'
  ];
  required_chunk_columns text[] := ARRAY['chunk_hash'];
  required_tables text[] := ARRAY[
    'vault.organizations',
    'vault.organization_members',
    'vault.projects',
    'vault.project_members',
    'vault.entry_access'
  ];
  required_column text;
  required_table text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    RAISE EXCEPTION 'Missing required extension: vector';
  END IF;

  FOREACH required_column IN ARRAY required_entry_columns LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = 'vault'
        AND table_name = 'entries'
        AND column_name = required_column
    ) THEN
      RAISE EXCEPTION 'Missing required vault.entries column: %', required_column;
    END IF;
  END LOOP;

  FOREACH required_column IN ARRAY required_chunk_columns LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = 'vault'
        AND table_name = 'chunks'
        AND column_name = required_column
    ) THEN
      RAISE EXCEPTION 'Missing required vault.chunks column: %', required_column;
    END IF;
  END LOOP;

  FOREACH required_table IN ARRAY required_tables LOOP
    IF to_regclass(required_table) IS NULL THEN
      RAISE EXCEPTION 'Missing required sharing table: %', required_table;
    END IF;
  END LOOP;
END $$;
