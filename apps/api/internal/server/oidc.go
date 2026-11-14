package server

import (
	"context"
	"github.com/coreos/go-oidc/v3/oidc"
)

// OIDCVerifier verifies tokens through the issuer discovery document and JWKS.
// It is constructed only when hosted mode has complete configuration.
type OIDCVerifier struct {
	issuer   string
	verifier *oidc.IDTokenVerifier
}

func NewOIDCVerifier(ctx context.Context, issuer, clientID string) (*OIDCVerifier, error) {
	provider, err := oidc.NewProvider(ctx, issuer)
	if err != nil {
		return nil, err
	}
	return &OIDCVerifier{issuer: issuer, verifier: provider.Verifier(&oidc.Config{ClientID: clientID})}, nil
}
func (v *OIDCVerifier) Verify(ctx context.Context, raw string) (Principal, error) {
	token, err := v.verifier.Verify(ctx, raw)
	if err != nil {
		return Principal{}, ErrInvalidToken
	}
	var claims struct {
		Subject string `json:"sub"`
		Issuer  string `json:"iss"`
	}
	if err = token.Claims(&claims); err != nil || claims.Subject == "" || claims.Issuer != v.issuer {
		return Principal{}, ErrInvalidToken
	}
	return Principal{Issuer: claims.Issuer, Subject: claims.Subject}, nil
}
