package main

import (
	"strings"
	"testing"
)

func TestRunRejectsMissingControlTokenBeforeDatabaseAccess(t *testing.T) {
	t.Setenv("API_ADDRESS", "127.0.0.1:0")
	t.Setenv("AGENTPROOF_API_TOKEN", "")
	t.Setenv("DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/missing")
	err := run()
	if err == nil || !strings.Contains(err.Error(), "AGENTPROOF_API_TOKEN") {
		t.Fatalf("startup did not fail closed on missing control token: %v", err)
	}
}

func TestRunRejectsIncompleteOIDCConfigurationBeforeDatabaseAccess(t *testing.T) {
	t.Setenv("API_ADDRESS", "127.0.0.1:0")
	t.Setenv("AGENTPROOF_AUTH_MODE", "oidc")
	t.Setenv("AGENTPROOF_OIDC_ISSUER", "")
	t.Setenv("AGENTPROOF_OIDC_CLIENT_ID", "")
	t.Setenv("DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/missing")
	err := run()
	if err == nil || !strings.Contains(err.Error(), "OIDC mode requires") {
		t.Fatalf("OIDC startup did not fail closed: %v", err)
	}
}
