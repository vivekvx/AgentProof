-- Local synthetic deployment only. The application role has no schema or role authority.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentproof_runtime') THEN
    CREATE ROLE agentproof_runtime LOGIN PASSWORD 'agentproof_runtime_local_only';
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO agentproof_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agentproof_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentproof_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO agentproof_runtime;
REVOKE ALL ON TABLE schema_migrations FROM agentproof_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentproof_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO agentproof_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO agentproof_runtime;
