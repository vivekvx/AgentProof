package server

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"io"
	"net/http"
	"strings"
)

func (s *Server) projectRoutes(r chi.Router) {
	r.Post("/api/v2/projects", s.createProject)
	r.Get("/api/v2/projects", s.listProjects)
}
func projectInput(w http.ResponseWriter, r *http.Request) (string, error) {
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	var in struct {
		Name string `json:"name"`
	}
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(&in); err != nil {
		return "", err
	}
	if d.Decode(&struct{}{}) != io.EOF {
		return "", errors.New("one JSON object required")
	}
	in.Name = strings.TrimSpace(in.Name)
	if len(in.Name) < 1 || len(in.Name) > 120 {
		return "", errors.New("invalid project name")
	}
	return in.Name, nil
}
func setDeveloper(ctx context.Context, tx pgx.Tx, id string) error {
	_, err := tx.Exec(ctx, "SELECT set_config('app.developer_id',$1,true)", id)
	return err
}
func (s *Server) identity(ctx context.Context, tx pgx.Tx, p Principal) (string, error) {
	var id string
	err := tx.QueryRow(ctx, "SELECT id FROM developer_identities WHERE issuer=$1 AND subject=$2", p.Issuer, p.Subject).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		id = uuid.NewString()
		_, err = tx.Exec(ctx, "INSERT INTO developer_identities(id,issuer,subject) VALUES($1,$2,$3)", id, p.Issuer, p.Subject)
	}
	return id, err
}
func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	if s.HostedAuth == nil {
		problem(w, 404, "projects require hosted authentication")
		return
	}
	p, ok := s.hostedPrincipal(w, r)
	if !ok {
		return
	}
	name, err := projectInput(w, r)
	if err != nil {
		problem(w, 400, "invalid project")
		return
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "database unavailable")
		return
	}
	defer tx.Rollback(r.Context())
	developer, err := s.identity(r.Context(), tx, p)
	if err == nil {
		err = setDeveloper(r.Context(), tx, developer)
	}
	id := uuid.NewString()
	if err == nil {
		_, err = tx.Exec(r.Context(), "INSERT INTO projects(id,name,created_by) VALUES($1,$2,$3)", id, name, developer)
	}
	if err == nil {
		_, err = tx.Exec(r.Context(), "INSERT INTO project_memberships(project_id,developer_id,role) VALUES($1,$2,'owner')", id, developer)
	}
	if err != nil {
		problem(w, 503, "cannot create project")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		problem(w, 503, "cannot create project")
		return
	}
	respond(w, 201, map[string]string{"id": id, "name": name, "role": "owner"})
}
func (s *Server) listProjects(w http.ResponseWriter, r *http.Request) {
	if s.HostedAuth == nil {
		problem(w, 404, "projects require hosted authentication")
		return
	}
	p, ok := s.hostedPrincipal(w, r)
	if !ok {
		return
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "database unavailable")
		return
	}
	defer tx.Rollback(r.Context())
	developer, err := s.identity(r.Context(), tx, p)
	if err == nil {
		err = setDeveloper(r.Context(), tx, developer)
	}
	if err != nil {
		problem(w, 503, "cannot read projects")
		return
	}
	rows, err := tx.Query(r.Context(), "SELECT p.id,p.name,m.role FROM projects p JOIN project_memberships m ON m.project_id=p.id ORDER BY p.created_at DESC")
	if err != nil {
		problem(w, 503, "cannot read projects")
		return
	}
	defer rows.Close()
	out := []map[string]string{}
	for rows.Next() {
		var id, name, role string
		if err = rows.Scan(&id, &name, &role); err != nil {
			problem(w, 503, "cannot read projects")
			return
		}
		out = append(out, map[string]string{"id": id, "name": name, "role": role})
	}
	if err = rows.Err(); err != nil {
		problem(w, 503, "cannot read projects")
		return
	}
	respond(w, 200, out)
}

// hostedEvaluationAccess checks membership in a short transaction whose tenant
// setting is local to that transaction. This prevents pooled connections from
// retaining a previous developer's identity.
func (s *Server) hostedEvaluationAccess(w http.ResponseWriter, r *http.Request, evaluationID string, write bool) (string, string, bool) {
	if s.HostedAuth == nil {
		return "", "", true
	}
	p, ok := s.hostedPrincipal(w, r)
	if !ok {
		return "", "", false
	}
	projectID := r.Header.Get("X-AgentProof-Project")
	if _, err := uuid.Parse(projectID); err != nil {
		problem(w, http.StatusBadRequest, "project selector required")
		return "", "", false
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "database unavailable")
		return "", "", false
	}
	defer tx.Rollback(r.Context())
	developer, err := s.identity(r.Context(), tx, p)
	if err == nil {
		err = setDeveloper(r.Context(), tx, developer)
	}
	if err != nil {
		problem(w, 503, "cannot verify project authorization")
		return "", "", false
	}
	var allowed bool
	roleCheck := "TRUE"
	if write {
		roleCheck = "m.role IN ('owner','member')"
	}
	err = tx.QueryRow(r.Context(), "SELECT EXISTS(SELECT 1 FROM benchmark_evaluation_projects e JOIN project_memberships m ON m.project_id=e.project_id WHERE e.evaluation_id=$1 AND e.project_id=$2 AND "+roleCheck+")", evaluationID, projectID).Scan(&allowed)
	if err != nil || !allowed {
		problem(w, http.StatusNotFound, "evaluation not found")
		return "", "", false
	}
	return projectID, developer, true
}
