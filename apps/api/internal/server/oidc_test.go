package server

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
)

func TestOIDCVerifierAcceptsOnlySignedCurrentAudienceToken(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	var issuer string
	jwks, _ := json.Marshal(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "test-key", Algorithm: "RS256", Use: "sig"}}})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/keys" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(jwks)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"issuer": issuer, "jwks_uri": issuer + "/keys", "id_token_signing_alg_values_supported": []string{"RS256"}})
	}))
	defer provider.Close()
	issuer = provider.URL
	verifier, err := NewOIDCVerifier(context.Background(), issuer, "agentproof-test-client")
	if err != nil {
		t.Fatal(err)
	}
	sign := func(audience string, expires time.Time) string {
		signer, e := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithHeader(jose.HeaderKey("kid"), "test-key"))
		if e != nil {
			t.Fatal(e)
		}
		raw, e := signer.Sign([]byte(`{"iss":"` + issuer + `","sub":"developer-a","aud":"` + audience + `","exp":` + jsonNumber(expires.Unix()) + `}`))
		if e != nil {
			t.Fatal(e)
		}
		out, e := raw.CompactSerialize()
		if e != nil {
			t.Fatal(e)
		}
		return out
	}
	if p, err := verifier.Verify(context.Background(), sign("agentproof-test-client", time.Now().Add(time.Minute))); err != nil || p.Subject != "developer-a" {
		t.Fatalf("valid token rejected: %#v %v", p, err)
	}
	if _, err := verifier.Verify(context.Background(), sign("wrong", time.Now().Add(time.Minute))); err == nil {
		t.Fatal("wrong audience accepted")
	}
	if _, err := verifier.Verify(context.Background(), sign("agentproof-test-client", time.Now().Add(-time.Minute))); err == nil {
		t.Fatal("expired token accepted")
	}
}
func jsonNumber(v int64) string   { return json.Number(stringifyInt(v)).String() }
func stringifyInt(v int64) string { return fmt.Sprintf("%d", v) }
