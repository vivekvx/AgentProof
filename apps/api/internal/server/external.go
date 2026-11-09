package server

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.temporal.io/sdk/client"
)

type externalWorkflow interface {
	ExternalSession(context.Context, string, string) (externalTask, error)
	ExternalUpdate(context.Context, string, string, string, any) (map[string]any, error)
}

type externalToolDescriptor struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema"`
}

type externalTask struct {
	SchemaVersion  string                   `json:"schema_version"`
	Attempt        int                      `json:"attempt"`
	RunID          string                   `json:"run_id"`
	TrialID        string                   `json:"trial_id"`
	ScenarioID     string                   `json:"scenario_id"`
	Request        string                   `json:"request"`
	Tools          []externalToolDescriptor `json:"tools"`
	Limits         map[string]int           `json:"limits"`
	DeadlineUnixMS int64                    `json:"deadline_unix_ms"`
	ExpiresAt      string                   `json:"expires_at"`
	ResumeToken    *string                  `json:"resume_token"`
}

func (t TemporalStarter) ExternalSession(ctx context.Context, evaluationID, trialID string) (externalTask, error) {
	var out externalTask
	value, err := t.Client.QueryWorkflow(ctx, "benchmark-"+evaluationID, "", "external_trial_session", trialID)
	if err != nil {
		return out, err
	}
	err = value.Get(&out)
	return out, err
}

func (t TemporalStarter) ExternalUpdate(ctx context.Context, evaluationID, updateName, updateID string, payload any) (map[string]any, error) {
	var out map[string]any
	handle, err := t.Client.UpdateWorkflow(ctx, client.UpdateWorkflowOptions{
		WorkflowID:   "benchmark-" + evaluationID,
		UpdateID:     updateID,
		UpdateName:   updateName,
		Args:         []any{payload},
		WaitForStage: client.WorkflowUpdateStageCompleted,
	})
	if err != nil {
		return nil, err
	}
	err = handle.Get(ctx, &out)
	return out, err
}

type externalToolRequest struct {
	SchemaVersion string                     `json:"schema_version"`
	RunID         string                     `json:"run_id"`
	TrialID       string                     `json:"trial_id"`
	Sequence      int                        `json:"sequence"`
	OperationID   string                     `json:"operation_id"`
	Tool          string                     `json:"tool"`
	Arguments     map[string]json.RawMessage `json:"arguments"`
}

type externalOutcomeRequest struct {
	SchemaVersion string  `json:"schema_version"`
	RunID         string  `json:"run_id"`
	TrialID       string  `json:"trial_id"`
	LastSequence  int     `json:"last_sequence"`
	Resolution    string  `json:"resolution"`
	Message       *string `json:"message,omitempty"`
	ErrorCode     *string `json:"error_code,omitempty"`
}

type externalReplayReader interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}

func (s *Server) externalWorkflow(w http.ResponseWriter) (externalWorkflow, bool) {
	value, ok := s.Starter.(externalWorkflow)
	if !ok {
		problem(w, http.StatusServiceUnavailable, "external workflow service unavailable")
		return nil, false
	}
	return value, true
}

func (s *Server) externalAgentRoutes(r chi.Router) {
	r.Post("/agent/v1/sessions/{id}/tools", s.externalTool)
	r.Post("/agent/v1/sessions/{id}/complete", s.externalComplete)
	r.Delete("/agent/v1/sessions/{id}", s.externalCancel)
	r.Get("/agent/v1/sessions/{id}", s.externalResult)
}

func (s *Server) createExternalSession(w http.ResponseWriter, r *http.Request) {
	if len(s.BearerToken) < 32 {
		problem(w, http.StatusServiceUnavailable, "external session issuance requires a configured control credential")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 1024)
	body, err := io.ReadAll(r.Body)
	if err != nil || len(body) != 0 {
		problem(w, http.StatusBadRequest, "session request body must be empty")
		return
	}
	trialID, ok := validUUID(w, r)
	if !ok {
		return
	}
	var evaluationID string
	err = s.Pool.QueryRow(r.Context(), "SELECT evaluation_id FROM benchmark_trials WHERE id=$1", trialID).Scan(&evaluationID)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, http.StatusNotFound, "trial not found")
		return
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external trial")
		return
	}
	workflowClient, ok := s.externalWorkflow(w)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
	defer cancel()
	task, err := workflowClient.ExternalSession(ctx, evaluationID, trialID)
	if err != nil || task.TrialID != trialID || task.RunID != evaluationID || task.Attempt < 1 {
		problem(w, http.StatusConflict, "external session is not ready")
		return
	}
	deadline := task.DeadlineUnixMS
	mac := hmac.New(sha256.New, []byte(s.BearerToken))
	_, _ = mac.Write([]byte(fmt.Sprintf("agentproof-session-v1:%s:%s:%d:%d", evaluationID, trialID, task.Attempt, deadline)))
	credential := hex.EncodeToString(mac.Sum(nil))
	digest := sha256.Sum256([]byte(credential))
	result, err := s.Pool.Exec(ctx, "UPDATE benchmark_external_sessions SET credential_hash=$1 WHERE trial_id=$2 AND evaluation_id=$3 AND attempt=$4 AND state='waiting' AND expires_at>now()", hex.EncodeToString(digest[:]), trialID, evaluationID, task.Attempt)
	if err != nil || result.RowsAffected() != 1 {
		problem(w, http.StatusGone, "external session expired or closed")
		return
	}
	task.ResumeToken = &credential
	w.Header().Set("Cache-Control", "no-store")
	respond(w, http.StatusCreated, map[string]any{"task": task, "credential": credential})
}

func sessionCredential(r *http.Request) (string, bool) {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		return "", false
	}
	token := strings.TrimPrefix(header, "Bearer ")
	if len(token) != 64 {
		return "", false
	}
	if _, err := hex.DecodeString(token); err != nil {
		return "", false
	}
	return token, true
}

func decodeExternalJSON(w http.ResponseWriter, r *http.Request, limit int64, target any) error {
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return errors.New("one JSON object required")
	}
	return nil
}

func (s *Server) externalIdentity(w http.ResponseWriter, ctx context.Context, trialID string) (string, int, bool) {
	var evaluationID string
	var attempt int
	err := s.Pool.QueryRow(ctx, "SELECT evaluation_id,attempt FROM benchmark_external_sessions WHERE trial_id=$1", trialID).Scan(&evaluationID, &attempt)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, http.StatusNotFound, "external session not found")
		return "", 0, false
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external session")
		return "", 0, false
	}
	return evaluationID, attempt, true
}

func (s *Server) authenticateExternalSession(w http.ResponseWriter, r *http.Request, trialID string) (string, int, bool) {
	token, ok := sessionCredential(r)
	if !ok {
		problem(w, http.StatusUnauthorized, "session credential required")
		return "", 0, false
	}
	digest := sha256.Sum256([]byte(token))
	provided := hex.EncodeToString(digest[:])
	var evaluationID, expectedHash, state string
	var attempt int
	var expires time.Time
	err := s.Pool.QueryRow(r.Context(), "SELECT evaluation_id,attempt,credential_hash,state,expires_at FROM benchmark_external_sessions WHERE trial_id=$1", trialID).Scan(&evaluationID, &attempt, &expectedHash, &state, &expires)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, http.StatusNotFound, "external session not found")
		return "", 0, false
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external session")
		return "", 0, false
	}
	if len(provided) != len(expectedHash) || subtle.ConstantTimeCompare([]byte(provided), []byte(expectedHash)) != 1 {
		problem(w, http.StatusUnauthorized, "invalid session credential")
		return "", 0, false
	}
	if state != "waiting" || !expires.After(time.Now()) {
		problem(w, http.StatusGone, "external session is closed")
		return "", 0, false
	}
	return evaluationID, attempt, true
}

func parseExternalArguments(raw map[string]json.RawMessage) (map[string]any, error) {
	if len(raw) > 32 {
		return nil, errors.New("too many tool arguments")
	}
	arguments := make(map[string]any, len(raw))
	for key, value := range raw {
		if len(key) > 64 || len(value) > 2048 {
			return nil, errors.New("tool argument is too large")
		}
		var item any
		if err := json.Unmarshal(value, &item); err != nil {
			return nil, err
		}
		arguments[key] = item
	}
	return arguments, nil
}

func externalReplayMismatch(ctx context.Context, db externalReplayReader, trialID string, request externalToolRequest, arguments map[string]any) (bool, error) {
	var raw json.RawMessage
	err := db.QueryRow(ctx, "SELECT payload FROM benchmark_events WHERE trial_id=$1 AND payload->>'operation_id'=$2", trialID, request.OperationID).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var event struct {
		Sequence      int            `json:"sequence"`
		Tool          string         `json:"tool"`
		RequestedTool string         `json:"requested_tool"`
		Arguments     map[string]any `json:"arguments"`
	}
	if json.Unmarshal(raw, &event) != nil {
		return true, nil
	}
	tool := event.Tool
	if event.RequestedTool != "" {
		tool = event.RequestedTool
	}
	if event.Sequence != request.Sequence || tool != request.Tool {
		return true, nil
	}
	delete(event.Arguments, "schema_version")
	delete(arguments, "schema_version")
	if rejected, ok := event.Arguments["_rejected_arguments"].(map[string]any); ok {
		encoded, marshalErr := json.Marshal(arguments)
		if marshalErr != nil {
			return true, nil
		}
		digest := sha256.Sum256(encoded)
		return rejected["sha256"] != hex.EncodeToString(digest[:]), nil
	}
	expected, err := json.Marshal(event.Arguments)
	if err != nil {
		return true, nil
	}
	actual, err := json.Marshal(arguments)
	if err != nil {
		return true, nil
	}
	return !hmac.Equal(expected, actual), nil
}

func (s *Server) externalTool(w http.ResponseWriter, r *http.Request) {
	trialID, ok := validUUID(w, r)
	if !ok {
		return
	}
	var request externalToolRequest
	if err := decodeExternalJSON(w, r, 4096, &request); err != nil {
		problem(w, http.StatusBadRequest, "invalid tool request")
		return
	}
	if request.SchemaVersion != "1.0" || request.TrialID != trialID || request.Sequence < 1 || request.Sequence > 32 {
		problem(w, http.StatusBadRequest, "invalid tool request identity")
		return
	}
	if _, err := uuid.Parse(request.RunID); err != nil {
		problem(w, http.StatusBadRequest, "invalid run identity")
		return
	}
	operationID, err := uuid.Parse(request.OperationID)
	if err != nil {
		problem(w, http.StatusBadRequest, "invalid operation identity")
		return
	}
	arguments, err := parseExternalArguments(request.Arguments)
	if err != nil {
		problem(w, http.StatusBadRequest, "invalid tool arguments")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	evaluationID, attempt, ok := s.authenticateExternalSession(w, r, trialID)
	if !ok {
		return
	}
	if request.RunID != evaluationID {
		problem(w, http.StatusForbidden, "run identity does not match session")
		return
	}
	mismatch, err := externalReplayMismatch(r.Context(), s.Pool, trialID, request, arguments)
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot verify operation replay")
		return
	}
	if mismatch {
		respond(w, http.StatusConflict, map[string]any{
			"schema_version": "1.0", "run_id": evaluationID, "trial_id": trialID,
			"sequence": request.Sequence, "operation_id": operationID.String(),
			"ok": false, "error_code": "replay_mismatch",
		})
		return
	}
	workflowClient, ok := s.externalWorkflow(w)
	if !ok {
		return
	}
	payload := map[string]any{
		"schema_version": request.SchemaVersion,
		"run_id":         request.RunID,
		"trial_id":       request.TrialID,
		"attempt":        attempt,
		"sequence":       request.Sequence,
		"operation_id":   operationID.String(),
		"tool":           request.Tool,
		"arguments":      arguments,
	}
	updateID := "external-tool-" + trialID + "-" + operationID.String()
	result, err := workflowClient.ExternalUpdate(ctx, evaluationID, "external_tool_call", updateID, payload)
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "external tool execution unavailable")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	if code, _ := result["error_code"].(string); code != "" {
		status := map[string]int{"unauthorized": http.StatusForbidden, "deadline_exceeded": http.StatusGone, "invalid_arguments": http.StatusBadRequest, "unknown_tool": http.StatusBadRequest, "budget_exhausted": http.StatusTooManyRequests, "replay_mismatch": http.StatusConflict, "unavailable": http.StatusServiceUnavailable}[code]
		if status == 0 {
			status = http.StatusBadGateway
		}
		respond(w, status, result)
		return
	}
	respond(w, http.StatusOK, result)
}

func (s *Server) externalComplete(w http.ResponseWriter, r *http.Request) {
	trialID, ok := validUUID(w, r)
	if !ok {
		return
	}
	var request externalOutcomeRequest
	if err := decodeExternalJSON(w, r, 4096, &request); err != nil {
		problem(w, http.StatusBadRequest, "invalid completion request")
		return
	}
	if request.SchemaVersion != "1.0" || request.TrialID != trialID || request.LastSequence < 0 || request.LastSequence > 32 {
		problem(w, http.StatusBadRequest, "invalid completion identity")
		return
	}
	if _, err := uuid.Parse(request.RunID); err != nil {
		problem(w, http.StatusBadRequest, "invalid run identity")
		return
	}
	if request.Resolution != "completed" && request.Resolution != "refused" && request.Resolution != "blocked" && request.Resolution != "unknown" {
		problem(w, http.StatusBadRequest, "invalid completion resolution")
		return
	}
	if request.ErrorCode != nil && request.Message != nil {
		problem(w, http.StatusBadRequest, "error completion cannot include a message")
		return
	}
	if request.Message != nil && len(*request.Message) > 2000 {
		problem(w, http.StatusBadRequest, "completion message exceeds limit")
		return
	}
	if request.ErrorCode != nil {
		allowed := map[string]bool{
			"deadline_exceeded": true,
			"cancelled":         true,
			"unavailable":       true,
			"malformed_request": true,
			"internal_error":    true,
		}
		if !allowed[*request.ErrorCode] {
			problem(w, http.StatusBadRequest, "invalid completion error code")
			return
		}
	}
	evaluationID, attempt, ok := s.authenticateExternalSession(w, r, trialID)
	if !ok {
		return
	}
	if request.RunID != evaluationID {
		problem(w, http.StatusForbidden, "run identity does not match session")
		return
	}
	workflowClient, ok := s.externalWorkflow(w)
	if !ok {
		return
	}
	outcome := map[string]any{"resolution": request.Resolution}
	if request.Message != nil {
		outcome["message"] = *request.Message
	}
	if request.ErrorCode != nil {
		outcome["error_code"] = *request.ErrorCode
	}
	payload := map[string]any{"run_id": evaluationID, "trial_id": trialID, "attempt": attempt, "last_sequence": request.LastSequence, "outcome": outcome}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	result, err := workflowClient.ExternalUpdate(ctx, evaluationID, "complete_external_trial", "external-complete-"+trialID, payload)
	if err != nil {
		problem(w, http.StatusConflict, "external completion was rejected")
		return
	}
	if accepted, _ := result["accepted"].(bool); !accepted {
		code, _ := result["error_code"].(string)
		status := http.StatusConflict
		if code == "unauthorized" {
			status = http.StatusUnauthorized
		} else if code == "deadline_exceeded" {
			status = http.StatusGone
		}
		respond(w, status, result)
		return
	}
	respond(w, http.StatusAccepted, result)
}

func (s *Server) externalCancel(w http.ResponseWriter, r *http.Request) {
	trialID, ok := validUUID(w, r)
	if !ok {
		return
	}
	evaluationID, attempt, ok := s.authenticateExternalSession(w, r, trialID)
	if !ok {
		return
	}
	workflowClient, ok := s.externalWorkflow(w)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	result, err := workflowClient.ExternalUpdate(ctx, evaluationID, "cancel_external_trial", "external-cancel-"+trialID, map[string]any{"run_id": evaluationID, "trial_id": trialID, "attempt": attempt})
	if err != nil {
		problem(w, http.StatusConflict, "external cancellation was rejected")
		return
	}
	respond(w, http.StatusAccepted, result)
}

func (s *Server) externalResult(w http.ResponseWriter, r *http.Request) {
	trialID, ok := validUUID(w, r)
	if !ok {
		return
	}
	token, ok := sessionCredential(r)
	if !ok {
		problem(w, http.StatusUnauthorized, "session credential required")
		return
	}
	digest := sha256.Sum256([]byte(token))
	var evaluationID, storedHash, state string
	var expires time.Time
	err := s.Pool.QueryRow(r.Context(), "SELECT evaluation_id,credential_hash,state,expires_at FROM benchmark_external_sessions WHERE trial_id=$1", trialID).Scan(&evaluationID, &storedHash, &state, &expires)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, http.StatusNotFound, "external session not found")
		return
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external result")
		return
	}
	actual := hex.EncodeToString(digest[:])
	if len(actual) != len(storedHash) || subtle.ConstantTimeCompare([]byte(actual), []byte(storedHash)) != 1 {
		problem(w, http.StatusUnauthorized, "invalid session credential")
		return
	}
	var result json.RawMessage
	var trialStatus string
	err = s.Pool.QueryRow(r.Context(), "SELECT status,result FROM benchmark_trials WHERE id=$1 AND evaluation_id=$2", trialID, evaluationID).Scan(&trialStatus, &result)
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external result")
		return
	}
	rows, err := s.Pool.Query(r.Context(), "SELECT payload FROM benchmark_events WHERE trial_id=$1 ORDER BY sequence", trialID)
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external evidence")
		return
	}
	defer rows.Close()
	events := []json.RawMessage{}
	for rows.Next() {
		var event json.RawMessage
		if err = rows.Scan(&event); err != nil {
			problem(w, http.StatusServiceUnavailable, "cannot read external evidence")
			return
		}
		events = append(events, event)
	}
	if rows.Err() != nil {
		problem(w, http.StatusServiceUnavailable, "cannot read external evidence")
		return
	}
	status := http.StatusOK
	if trialStatus != "completed" {
		status = http.StatusAccepted
	}
	w.Header().Set("Cache-Control", "no-store")
	respond(w, status, map[string]any{"schema_version": "1.0", "run_id": evaluationID, "trial_id": trialID, "session_state": state, "expires_at": expires, "trial_status": trialStatus, "result": result, "events": events})
}
