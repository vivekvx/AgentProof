package db

import (
	"context"
	"errors"
	"net/url"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestLocalRuntimeRoleCannotChangeSchemaOrMigrationHistory(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL")
	}
	ctx := context.Background()
	owner, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	if err := Migrate(ctx, owner); err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	parsed.User = url.UserPassword("agentproof_runtime", "agentproof_runtime_local_only")
	runtime, err := pgxpool.New(ctx, parsed.String())
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	var super, createDB, createRole bool
	if err := runtime.QueryRow(ctx, "SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user").Scan(&super, &createDB, &createRole); err != nil {
		t.Fatal(err)
	}
	if super || createDB || createRole {
		t.Fatal("runtime role has administrative privileges")
	}
	var count int
	if err := runtime.QueryRow(ctx, "SELECT count(*) FROM benchmark_trials").Scan(&count); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"CREATE TABLE public.agentproof_role_escape(id int)",
		"SELECT version FROM schema_migrations LIMIT 1",
	} {
		_, err := runtime.Exec(ctx, statement)
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "42501" {
			t.Fatalf("runtime statement was not denied: %q: %v", statement, err)
		}
	}
}
