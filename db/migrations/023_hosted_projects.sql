-- Release 2A: hosted-mode ownership only. Existing records are explicitly hidden
-- behind an unowned legacy project; they are never assigned to a new developer.
CREATE TABLE developer_identities (
  id uuid PRIMARY KEY,
  issuer text NOT NULL,
  subject text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issuer, subject)
);
CREATE TABLE projects (
  id uuid PRIMARY KEY,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  created_by uuid NOT NULL REFERENCES developer_identities(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE project_memberships (
  project_id uuid NOT NULL REFERENCES projects(id),
  developer_id uuid NOT NULL REFERENCES developer_identities(id),
  role text NOT NULL CHECK (role IN ('owner','member','viewer')),
  PRIMARY KEY (project_id, developer_id)
);
CREATE TABLE project_api_credentials (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id),
  credential_hash text NOT NULL UNIQUE,
  created_by uuid NOT NULL REFERENCES developer_identities(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE TABLE benchmark_evaluation_projects (
  evaluation_id uuid PRIMARY KEY REFERENCES benchmark_evaluations(id),
  project_id uuid NOT NULL REFERENCES projects(id),
  created_by uuid REFERENCES developer_identities(id)
);
-- An ownership mapping is required even for records written before hosted mode.
-- This project deliberately has no membership and therefore is not visible.
INSERT INTO developer_identities(id,issuer,subject)
VALUES ('00000000-0000-0000-0000-000000000001','agentproof://legacy','unowned')
ON CONFLICT DO NOTHING;
INSERT INTO projects(id,name,created_by)
VALUES ('00000000-0000-0000-0000-000000000002','Legacy unowned records','00000000-0000-0000-0000-000000000001')
ON CONFLICT DO NOTHING;
INSERT INTO benchmark_evaluation_projects(evaluation_id,project_id)
SELECT id,'00000000-0000-0000-0000-000000000002' FROM benchmark_evaluations
ON CONFLICT DO NOTHING;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_api_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_evaluation_projects ENABLE ROW LEVEL SECURITY;
-- The runtime role is NOBYPASSRLS. The API sets app.developer_id only after OIDC verification.
CREATE POLICY project_visible_to_member ON projects FOR SELECT
USING (EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=id AND m.developer_id=current_setting('app.developer_id',true)::uuid));
CREATE POLICY project_create_by_self ON projects FOR INSERT
WITH CHECK (created_by=current_setting('app.developer_id',true)::uuid);
CREATE POLICY membership_visible_to_self ON project_memberships FOR SELECT
USING (developer_id=current_setting('app.developer_id',true)::uuid);
CREATE POLICY membership_create_owner ON project_memberships FOR INSERT
WITH CHECK (developer_id=current_setting('app.developer_id',true)::uuid AND role='owner');
CREATE POLICY credential_visible_to_owner ON project_api_credentials FOR ALL
USING (EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=project_id AND m.developer_id=current_setting('app.developer_id',true)::uuid AND m.role='owner'));
CREATE POLICY evaluation_mapping_visible_to_member ON benchmark_evaluation_projects FOR SELECT
USING (EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=project_id AND m.developer_id=current_setting('app.developer_id',true)::uuid));
CREATE POLICY evaluation_mapping_create_by_member ON benchmark_evaluation_projects FOR INSERT
WITH CHECK (created_by=current_setting('app.developer_id',true)::uuid AND EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=project_id AND m.developer_id=current_setting('app.developer_id',true)::uuid AND m.role IN ('owner','member')));
