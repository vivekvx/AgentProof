package db

import (
	"context"
	"embed"
	"github.com/jackc/pgx/v5/pgxpool"
	"io/fs"
	"sort"
)

//go:embed migrations/*.sql
var migrations embed.FS

func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(810011)"); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)"); err != nil {
		return err
	}
	paths, err := fs.Glob(migrations, "migrations/*.sql")
	if err != nil {
		return err
	}
	sort.Strings(paths)
	for _, path := range paths {
		var applied bool
		if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)", path).Scan(&applied); err != nil {
			return err
		}
		if applied {
			continue
		}
		data, readErr := migrations.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		if _, err = tx.Exec(ctx, string(data)); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, "INSERT INTO schema_migrations VALUES($1)", path); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
