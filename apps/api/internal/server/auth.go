package server

import (
	"context"
	"errors"
	"net/http"
	"strings"
)

type Principal struct {
	Issuer  string
	Subject string
}
type TokenVerifier interface {
	Verify(context.Context, string) (Principal, error)
}
type principalKey struct{}

func principalFrom(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(principalKey{}).(Principal)
	return p, ok
}
func oidcAuthentication(verifier TokenVerifier) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			header := r.Header.Get("Authorization")
			if !strings.HasPrefix(header, "Bearer ") {
				w.Header().Set("WWW-Authenticate", "Bearer")
				problem(w, http.StatusUnauthorized, "authentication required")
				return
			}
			p, err := verifier.Verify(r.Context(), strings.TrimPrefix(header, "Bearer "))
			if err != nil || p.Issuer == "" || p.Subject == "" {
				w.Header().Set("WWW-Authenticate", "Bearer error=\"invalid_token\"")
				problem(w, http.StatusUnauthorized, "authentication required")
				return
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey{}, p)))
		})
	}
}

func (s *Server) hostedPrincipal(w http.ResponseWriter, r *http.Request) (Principal, bool) {
	if s.HostedAuth == nil {
		return Principal{}, true
	}
	p, ok := principalFrom(r.Context())
	if !ok {
		problem(w, http.StatusUnauthorized, "authentication required")
		return Principal{}, false
	}
	return p, true
}
func (s *Server) authorizedProject(w http.ResponseWriter, r *http.Request, write bool) (string, bool) {
	if s.HostedAuth == nil {
		return "", true
	}
	p, ok := s.hostedPrincipal(w, r)
	if !ok {
		return "", false
	}
	id := r.Header.Get("X-AgentProof-Project")
	if id == "" {
		problem(w, http.StatusBadRequest, "project selector required")
		return "", false
	}
	role := "viewer"
	if write {
		role = "member"
	}
	var exists bool
	err := s.Pool.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM project_memberships m JOIN developer_identities d ON d.id=m.developer_id WHERE m.project_id=$1 AND d.issuer=$2 AND d.subject=$3 AND CASE WHEN $4='member' THEN m.role IN ('owner','member') ELSE true END)`, id, p.Issuer, p.Subject, role).Scan(&exists)
	if err != nil {
		problem(w, 503, "cannot verify project authorization")
		return "", false
	}
	if !exists {
		problem(w, http.StatusNotFound, "project not found")
		return "", false
	}
	return id, true
}

var ErrInvalidToken = errors.New("invalid OIDC token")
