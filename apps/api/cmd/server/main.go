package main

import (
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/apps/api/internal/server"
	"github.com/vivekvx/AgentProof/db"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.temporal.io/sdk/client"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	options := []sdktrace.TracerProviderOption{sdktrace.WithResource(resource.NewWithAttributes("", attribute.String("service.name", "agentproof-api")))}
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
		exporter, err := otlptracehttp.New(ctx)
		if err != nil {
			return err
		}
		options = append(options, sdktrace.WithBatcher(exporter))
	}
	provider := sdktrace.NewTracerProvider(options...)
	otel.SetTracerProvider(provider)
	defer provider.Shutdown(context.Background())
	otel.SetTextMapPropagator(propagation.TraceContext{})
	pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		return err
	}
	defer pool.Close()
	if err = db.Migrate(ctx, pool); err != nil {
		return err
	}
	opts, err := server.TemporalOptions(env("TEMPORAL_ADDRESS", "localhost:7233"))
	if err != nil {
		return err
	}
	temporal, err := client.Dial(opts)
	if err != nil {
		return err
	}
	defer temporal.Close()
	api := &server.Server{Pool: pool, Starter: server.TemporalStarter{Client: temporal}}
	go api.Dispatch(ctx)
	go api.DispatchBenchmarks(ctx)
	httpServer := &http.Server{Addr: env("API_ADDRESS", "127.0.0.1:8080"), Handler: api.Routes(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdown)
	}()
	slog.Info("AgentProof API ready", "address", httpServer.Addr)
	err = httpServer.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}
func main() {
	if err := run(); err != nil {
		slog.Error("API stopped", "error", err)
		os.Exit(1)
	}
}
