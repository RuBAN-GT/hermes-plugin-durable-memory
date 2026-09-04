CREATE TABLE durable_memory.memory_candidate (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    change_request_id uuid REFERENCES durable_memory.change_request (id),
    record_type text NOT NULL,
    identity_key text NOT NULL,
    payload jsonb NOT NULL,
    text text NOT NULL DEFAULT '',
    submitted_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE durable_memory.memory_evidence (
    id uuid PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES durable_memory.memory_candidate (id)
        ON DELETE CASCADE,
    source_kind text NOT NULL CHECK (source_kind <> ''),
    source_ref text NOT NULL CHECK (source_ref <> ''),
    observed_at timestamptz NOT NULL,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    extractor_identity text,
    extractor_version text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX memory_candidate_namespace_created
    ON durable_memory.memory_candidate (namespace_id, created_at);
CREATE INDEX memory_evidence_candidate ON durable_memory.memory_evidence (candidate_id);

CREATE FUNCTION durable_memory.validate_candidate_change_request()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE;
BEGIN
    IF NEW.change_request_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO request_row FROM durable_memory.change_request
    WHERE id = NEW.change_request_id;
    IF NOT FOUND
       OR request_row.namespace_id <> NEW.namespace_id
       OR request_row.requested_by_profile_id <> NEW.submitted_by_profile_id THEN
        RAISE EXCEPTION 'candidate change request must share namespace and submitter';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER memory_candidate_change_request_check
BEFORE INSERT OR UPDATE ON durable_memory.memory_candidate
FOR EACH ROW EXECUTE FUNCTION durable_memory.validate_candidate_change_request();

ALTER TABLE durable_memory.memory_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.memory_evidence ENABLE ROW LEVEL SECURITY;

CREATE POLICY memory_candidate_select ON durable_memory.memory_candidate
    FOR SELECT USING (
        submitted_by_profile_id = durable_memory.current_profile_id()
        OR durable_memory.has_capability(namespace_id, 'approve')
        OR durable_memory.has_capability(namespace_id, 'admin')
    );

CREATE POLICY memory_candidate_insert ON durable_memory.memory_candidate
    FOR INSERT WITH CHECK (
        submitted_by_profile_id = durable_memory.current_profile_id()
        AND durable_memory.has_capability(namespace_id, 'propose')
        AND (
            change_request_id IS NULL
            OR EXISTS (
                SELECT 1 FROM durable_memory.change_request
                WHERE id = change_request_id
                  AND namespace_id = memory_candidate.namespace_id
                  AND requested_by_profile_id = durable_memory.current_profile_id()
            )
        )
    );

CREATE POLICY memory_evidence_select ON durable_memory.memory_evidence
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM durable_memory.memory_candidate
            WHERE id = candidate_id
        )
    );

CREATE POLICY memory_evidence_insert ON durable_memory.memory_evidence
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM durable_memory.memory_candidate
            WHERE id = candidate_id
              AND submitted_by_profile_id = durable_memory.current_profile_id()
              AND durable_memory.has_capability(namespace_id, 'propose')
        )
    );
