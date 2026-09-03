CREATE SCHEMA IF NOT EXISTS durable_memory;

CREATE TABLE durable_memory.schema_migration (
    version integer PRIMARY KEY,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE durable_memory.profile (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    runtime_role name NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE durable_memory.namespace (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    kind text NOT NULL CHECK (kind IN ('private', 'shared')),
    owner_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE durable_memory.namespace_grant (
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    grantee_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    capability text NOT NULL CHECK (
        capability IN ('read', 'propose', 'approve', 'admin')
    ),
    granted_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, grantee_profile_id, capability)
);

CREATE TABLE durable_memory.record (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    record_type text NOT NULL,
    identity_key text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('active', 'tombstoned')
    ),
    revision integer NOT NULL,
    search_text text NOT NULL,
    payload jsonb NOT NULL,
    origin text NOT NULL DEFAULT 'tool',
    created_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    updated_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX record_active_identity
    ON durable_memory.record (namespace_id, record_type, identity_key)
    WHERE status = 'active';

CREATE TABLE durable_memory.record_revision (
    record_id uuid NOT NULL REFERENCES durable_memory.record (id),
    revision integer NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'delete')),
    payload jsonb NOT NULL,
    actor_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, revision)
);

CREATE TABLE durable_memory.change_request (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    record_id uuid REFERENCES durable_memory.record (id),
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'delete')),
    record_type text NOT NULL,
    identity_key text NOT NULL,
    expected_revision integer,
    payload jsonb NOT NULL,
    search_text text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired', 'superseded')
    ),
    policy_action text NOT NULL,
    requested_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    decided_by_profile_id uuid REFERENCES durable_memory.profile (id),
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    expires_at timestamptz NOT NULL
);

CREATE INDEX change_request_pending_namespace
    ON durable_memory.change_request (namespace_id, status);

CREATE FUNCTION durable_memory.current_profile_id()
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
    SELECT id FROM durable_memory.profile WHERE runtime_role = session_user
$$;

CREATE FUNCTION durable_memory.has_capability(
    target_namespace_id uuid,
    required_capability text
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM durable_memory.namespace AS namespace
        WHERE namespace.id = target_namespace_id
          AND namespace.owner_profile_id = durable_memory.current_profile_id()
    )
    OR EXISTS (
        SELECT 1
        FROM durable_memory.namespace_grant AS namespace_grant
        WHERE namespace_grant.namespace_id = target_namespace_id
          AND namespace_grant.grantee_profile_id = durable_memory.current_profile_id()
          AND namespace_grant.capability = required_capability
    )
$$;

ALTER TABLE durable_memory.namespace ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.namespace_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.record ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.record_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.change_request ENABLE ROW LEVEL SECURITY;

CREATE POLICY namespace_select ON durable_memory.namespace
    FOR SELECT
    USING (
        durable_memory.has_capability(id, 'read')
        OR durable_memory.has_capability(id, 'propose')
        OR durable_memory.has_capability(id, 'approve')
        OR durable_memory.has_capability(id, 'admin')
    );

CREATE POLICY namespace_insert ON durable_memory.namespace
    FOR INSERT
    WITH CHECK (owner_profile_id = durable_memory.current_profile_id());

CREATE POLICY namespace_update ON durable_memory.namespace
    FOR UPDATE
    USING (durable_memory.has_capability(id, 'admin'))
    WITH CHECK (durable_memory.has_capability(id, 'admin'));

CREATE POLICY namespace_grant_select ON durable_memory.namespace_grant
    FOR SELECT
    USING (
        grantee_profile_id = durable_memory.current_profile_id()
        OR durable_memory.has_capability(namespace_id, 'admin')
    );

CREATE POLICY namespace_grant_insert ON durable_memory.namespace_grant
    FOR INSERT
    WITH CHECK (durable_memory.has_capability(namespace_id, 'admin'));

CREATE POLICY namespace_grant_delete ON durable_memory.namespace_grant
    FOR DELETE
    USING (durable_memory.has_capability(namespace_id, 'admin'));

CREATE POLICY record_select ON durable_memory.record
    FOR SELECT
    USING (
        durable_memory.has_capability(namespace_id, 'read')
        OR durable_memory.has_capability(namespace_id, 'propose')
        OR durable_memory.has_capability(namespace_id, 'approve')
        OR durable_memory.has_capability(namespace_id, 'admin')
    );

CREATE POLICY record_revision_select ON durable_memory.record_revision
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM durable_memory.record
            WHERE record.id = record_revision.record_id
              AND (
                  durable_memory.has_capability(record.namespace_id, 'read')
                  OR durable_memory.has_capability(record.namespace_id, 'approve')
              )
        )
    );

CREATE POLICY change_request_select ON durable_memory.change_request
    FOR SELECT
    USING (
        requested_by_profile_id = durable_memory.current_profile_id()
        OR durable_memory.has_capability(namespace_id, 'approve')
        OR durable_memory.has_capability(namespace_id, 'admin')
    );

CREATE POLICY change_request_insert ON durable_memory.change_request
    FOR INSERT
    WITH CHECK (
        requested_by_profile_id = durable_memory.current_profile_id()
        AND durable_memory.has_capability(namespace_id, 'propose')
    );
