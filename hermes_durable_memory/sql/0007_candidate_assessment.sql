ALTER TABLE durable_memory.memory_candidate
    ADD COLUMN canonical_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN canonical_search_text text NOT NULL DEFAULT '',
    ADD COLUMN assessment text NOT NULL DEFAULT 'new' CHECK (
        assessment IN ('new', 'duplicate', 'conflict')
    );

CREATE TABLE durable_memory.candidate_record_relation (
    candidate_id uuid PRIMARY KEY REFERENCES durable_memory.memory_candidate (id)
        ON DELETE CASCADE,
    record_id uuid NOT NULL REFERENCES durable_memory.record (id),
    reason text NOT NULL CHECK (reason <> '')
);

CREATE FUNCTION durable_memory.validate_candidate_record_relation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM durable_memory.memory_candidate AS candidate
        JOIN durable_memory.record AS record ON record.id = NEW.record_id
        WHERE candidate.id = NEW.candidate_id
          AND candidate.namespace_id = record.namespace_id
          AND record.status = 'active'
    ) THEN
        RAISE EXCEPTION 'candidate relation must target an active record in its namespace';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER candidate_record_relation_active_record_check
BEFORE INSERT OR UPDATE ON durable_memory.candidate_record_relation
FOR EACH ROW EXECUTE FUNCTION durable_memory.validate_candidate_record_relation();

ALTER TABLE durable_memory.candidate_record_relation ENABLE ROW LEVEL SECURITY;

CREATE POLICY candidate_record_relation_select
    ON durable_memory.candidate_record_relation FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM durable_memory.memory_candidate AS candidate
            WHERE candidate.id = candidate_record_relation.candidate_id
        )
    );

CREATE POLICY candidate_record_relation_insert
    ON durable_memory.candidate_record_relation FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM durable_memory.memory_candidate AS candidate
            WHERE candidate.id = candidate_record_relation.candidate_id
              AND submitted_by_profile_id = durable_memory.current_profile_id()
              AND durable_memory.has_capability(namespace_id, 'propose')
        )
    );

CREATE FUNCTION durable_memory.candidate_identity_assessment(
    candidate_namespace_id uuid,
    candidate_record_type text,
    candidate_identity_key text,
    candidate_payload jsonb,
    candidate_search_text text
)
RETURNS TABLE (record_id uuid, assessment text, reason text)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = durable_memory, pg_temp
AS $$
DECLARE matched durable_memory.record%ROWTYPE;
BEGIN
    IF NOT durable_memory.has_capability(candidate_namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    SELECT * INTO matched FROM durable_memory.record
    WHERE namespace_id = candidate_namespace_id
      AND record_type = candidate_record_type
      AND identity_key = candidate_identity_key
      AND status = 'active'
    ORDER BY id
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::uuid, 'new'::text, NULL::text;
    ELSIF matched.payload = candidate_payload
          AND matched.search_text = candidate_search_text THEN
        RETURN QUERY SELECT matched.id, 'duplicate'::text,
            'exact_identity_and_equal_content'::text;
    ELSE
        RETURN QUERY SELECT matched.id, 'conflict'::text,
            'exact_identity_with_different_content'::text;
    END IF;
END
$$;

REVOKE ALL ON FUNCTION durable_memory.candidate_identity_assessment(
    uuid, text, text, jsonb, text
) FROM PUBLIC;
