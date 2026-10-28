CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  kind text NOT NULL DEFAULT 'other',
  description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','blocked','complete','archived')),
  next_action text,
  owner text,
  inactivity_threshold interval NOT NULL DEFAULT interval '14 days',
  last_meaningful_progress_at timestamptz,
  last_milestone_at timestamptz,
  last_user_interaction_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('agent','user','service')),
  permission_scopes text[] NOT NULL DEFAULT '{}',
  token_hash text NOT NULL UNIQUE,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  principal_id uuid NOT NULL UNIQUE REFERENCES principals(id),
  name text NOT NULL UNIQUE,
  agent_type text NOT NULL,
  provider_metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE controllers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  agent_identity_id uuid REFERENCES agent_identities(id),
  policy jsonb NOT NULL DEFAULT '{}',
  checkpoint jsonb NOT NULL DEFAULT '{}',
  max_recovery_level smallint NOT NULL DEFAULT 2 CHECK (max_recovery_level BETWEEN 1 AND 4),
  enabled_recovery_levels smallint[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id),
  CHECK (enabled_recovery_levels <@ ARRAY[1,2,3,4]::smallint[])
);

CREATE TABLE parent_threads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  controller_id uuid NOT NULL REFERENCES controllers(id),
  source text NOT NULL,
  external_thread_id text NOT NULL,
  state text NOT NULL DEFAULT 'unknown' CHECK (state IN ('running','idle','blocked','stopped','unknown')),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_process_heartbeat_at timestamptz,
  last_agent_event_at timestamptz,
  last_meaningful_progress_at timestamptz,
  last_milestone_at timestamptz,
  last_user_interaction_at timestamptz,
  thresholds jsonb NOT NULL DEFAULT '{}',
  metadata jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, external_thread_id)
);

CREATE TABLE project_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  source text NOT NULL,
  match_kind text NOT NULL CHECK (match_kind IN ('working_directory','external_thread_id','external_project_id')),
  match_value text NOT NULL,
  confidence text NOT NULL CHECK (confidence IN ('high','medium','low')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, match_kind, match_value)
);

CREATE TABLE unmapped_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text NOT NULL,
  external_thread_id text,
  working_directory text,
  source_event_id text NOT NULL,
  observed_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, source_event_id)
);

CREATE TABLE tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  parent_thread_id uuid REFERENCES parent_threads(id),
  title text NOT NULL,
  description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'created' CHECK (status IN ('created','running','blocked','needs_review','complete','verified','cancelled')),
  owner text,
  started_at timestamptz,
  due_at timestamptz,
  thresholds jsonb NOT NULL DEFAULT '{}',
  acceptance jsonb NOT NULL DEFAULT '{}',
  verification jsonb NOT NULL DEFAULT '{}',
  last_process_heartbeat_at timestamptz,
  last_agent_event_at timestamptz,
  last_meaningful_progress_at timestamptz,
  last_milestone_at timestamptz,
  last_user_interaction_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE milestones (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  task_id uuid REFERENCES tasks(id),
  title text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','complete','cancelled')),
  due_at timestamptz,
  completed_at timestamptz,
  owner text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subagent_registrations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  parent_thread_id uuid NOT NULL REFERENCES parent_threads(id),
  task_id uuid REFERENCES tasks(id),
  source text NOT NULL,
  external_agent_id text,
  agent_type text,
  status text NOT NULL DEFAULT 'unknown' CHECK (status IN ('running','waiting','blocked','complete','unknown')),
  started_at timestamptz,
  last_agent_event_at timestamptz,
  last_meaningful_progress_at timestamptz,
  last_seen_at timestamptz,
  stall_threshold interval NOT NULL DEFAULT interval '30 minutes',
  confidence text NOT NULL DEFAULT 'unknown' CHECK (confidence IN ('high','medium','low','unknown')),
  details jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  task_id uuid REFERENCES tasks(id),
  parent_thread_id uuid REFERENCES parent_threads(id),
  subagent_registration_id uuid REFERENCES subagent_registrations(id),
  event_type text NOT NULL,
  source text NOT NULL,
  confidence text NOT NULL CHECK (confidence IN ('high','medium','low','unknown')),
  source_event_id text,
  observed_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  actor_principal_id uuid REFERENCES principals(id),
  details jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, source_event_id)
);

CREATE TABLE incidents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  task_id uuid REFERENCES tasks(id),
  parent_thread_id uuid REFERENCES parent_threads(id),
  kind text NOT NULL,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','notifying','awaiting_ack','recovering','resolved','escalated','closed')),
  recovery_level smallint NOT NULL DEFAULT 1 CHECK (recovery_level BETWEEN 1 AND 5),
  reason text NOT NULL,
  retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  retry_budget integer NOT NULL DEFAULT 3 CHECK (retry_budget >= 0),
  cooldown_until timestamptz,
  ack_deadline timestamptz,
  progress_deadline timestamptz,
  opened_at timestamptz NOT NULL DEFAULT now(),
  acknowledged_at timestamptz,
  resumed_progress_at timestamptz,
  resolved_at timestamptz,
  details jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE notification_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES incidents(id),
  channel text NOT NULL,
  recipient text NOT NULL,
  payload jsonb NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','delivered','failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  acknowledged_at timestamptz,
  resumed_progress_at timestamptz,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at timestamptz,
  last_error text
);

CREATE TABLE idempotency_records (
  principal_id uuid NOT NULL REFERENCES principals(id),
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, idempotency_key)
);

CREATE TABLE task_verifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id),
  reviewer_principal_id uuid NOT NULL REFERENCES principals(id),
  decision text NOT NULL CHECK (decision IN ('verified','rejected')),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_approvals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES incidents(id),
  requested_by_principal_id uuid NOT NULL REFERENCES principals(id),
  approved_by_principal_id uuid REFERENCES principals(id),
  operation jsonb NOT NULL,
  operation_hash text NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected','expired','used')),
  expires_at timestamptz NOT NULL,
  approved_at timestamptz,
  used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_project_time_idx ON events(project_id, observed_at DESC);
CREATE INDEX events_task_time_idx ON events(task_id, observed_at DESC);
CREATE INDEX tasks_project_status_idx ON tasks(project_id, status);
CREATE INDEX incidents_open_idx ON incidents(project_id, opened_at DESC) WHERE state NOT IN ('resolved','closed');
CREATE INDEX outbox_pending_idx ON notification_outbox(next_attempt_at) WHERE state = 'pending';
