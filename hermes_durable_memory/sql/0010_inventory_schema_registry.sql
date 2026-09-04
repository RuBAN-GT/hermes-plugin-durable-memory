-- Inventory schemas are registry metadata, never canonical memory records.
CREATE TABLE durable_memory.memory_type (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    record_type text NOT NULL CHECK (record_type <> '' AND record_type <> '__inventory_definition__'),
    lifecycle_status text NOT NULL DEFAULT 'active' CHECK (lifecycle_status IN ('active', 'retired')),
    created_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    UNIQUE (namespace_id, record_type)
);
CREATE TABLE durable_memory.memory_schema_version (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_type_id uuid NOT NULL REFERENCES durable_memory.memory_type (id),
    version integer NOT NULL CHECK (version > 0),
    fields jsonb NOT NULL CHECK (jsonb_typeof(fields) = 'object'),
    schema jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(schema) = 'object'),
    lifecycle_status text NOT NULL DEFAULT 'active' CHECK (lifecycle_status IN ('active', 'superseded')),
    created_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (memory_type_id, version),
    UNIQUE NULLS NOT DISTINCT (memory_type_id, lifecycle_status) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE VIEW durable_memory.inventory_definition WITH (security_invoker = true) AS
SELECT type.namespace_id, type.record_type, version.version, version.fields,
       version.schema, type.lifecycle_status
FROM durable_memory.memory_type AS type
JOIN durable_memory.memory_schema_version AS version ON version.memory_type_id = type.id
WHERE type.lifecycle_status = 'active' AND version.lifecycle_status = 'active';

ALTER TABLE durable_memory.memory_type ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.memory_schema_version ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_type_select ON durable_memory.memory_type FOR SELECT
    USING (durable_memory.has_capability(namespace_id, 'read'));
CREATE POLICY memory_schema_version_select ON durable_memory.memory_schema_version FOR SELECT
    USING (EXISTS (SELECT 1 FROM durable_memory.memory_type AS type
                  WHERE type.id = memory_schema_version.memory_type_id
                    AND durable_memory.has_capability(type.namespace_id, 'read')));

-- Preserve audit history while making old definition records invisible everywhere.
INSERT INTO durable_memory.memory_type (namespace_id, record_type, created_by_profile_id)
SELECT record.namespace_id, record.identity_key, record.created_by_profile_id
FROM durable_memory.record AS record
WHERE record.record_type = '__inventory_definition__' AND record.status = 'active'
ON CONFLICT (namespace_id, record_type) DO NOTHING;
INSERT INTO durable_memory.memory_schema_version (memory_type_id, version, fields, schema, created_by_profile_id)
SELECT type.id, 1, record.payload -> 'fields', jsonb_build_object('legacy_record_id', record.id), record.created_by_profile_id
FROM durable_memory.record AS record
JOIN durable_memory.memory_type AS type
  ON type.namespace_id = record.namespace_id AND type.record_type = record.identity_key
WHERE record.record_type = '__inventory_definition__' AND record.status = 'active'
  AND jsonb_typeof(record.payload -> 'fields') = 'object'
ON CONFLICT (memory_type_id, version) DO NOTHING;
DELETE FROM durable_memory.record_embedding
WHERE record_id IN (SELECT id FROM durable_memory.record WHERE record_type = '__inventory_definition__');

CREATE OR REPLACE FUNCTION durable_memory.proposal_inventory_definition(target_namespace_id uuid, target_record_type text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE definition jsonb;
BEGIN
    IF NOT durable_memory.has_capability(target_namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    SELECT jsonb_build_object('namespace_id', namespace_id, 'record_type', record_type,
        'version', version, 'fields', fields, 'lifecycle_status', lifecycle_status)
    INTO definition FROM durable_memory.inventory_definition
    WHERE namespace_id = target_namespace_id AND record_type = target_record_type;
    RETURN definition;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.candidate_semantic_assessment(
    candidate_id uuid, duplicate_similarity double precision DEFAULT 0.98,
    conflict_similarity double precision DEFAULT 0.80
) RETURNS TABLE (record_id uuid, assessment text, reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE candidate_row durable_memory.memory_candidate%ROWTYPE; exact_row durable_memory.record%ROWTYPE;
    match_row record; similarity double precision;
BEGIN
    IF duplicate_similarity <= 0 OR duplicate_similarity > 1 OR conflict_similarity <= 0 OR conflict_similarity >= duplicate_similarity THEN RAISE EXCEPTION 'semantic similarity thresholds must satisfy 0 < conflict < duplicate <= 1'; END IF;
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF candidate_row.submitted_by_profile_id <> durable_memory.current_profile_id() OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'propose') THEN RAISE EXCEPTION 'proposal capability is required'; END IF;
    SELECT * INTO exact_row FROM durable_memory.record WHERE namespace_id = candidate_row.namespace_id AND record_type = candidate_row.record_type AND identity_key = candidate_row.identity_key AND status = 'active' AND record_type <> '__inventory_definition__' ORDER BY id LIMIT 1;
    IF FOUND THEN
        assessment := CASE WHEN exact_row.payload = candidate_row.canonical_payload AND exact_row.search_text = candidate_row.canonical_search_text THEN 'duplicate' ELSE 'conflict' END;
        reason := CASE WHEN assessment = 'duplicate' THEN 'exact_identity_and_equal_content' ELSE 'exact_identity_with_different_content' END; record_id := exact_row.id;
    ELSE
        SELECT record.id, 1 - (projection.embedding <=> candidate_embedding.embedding) AS similarity INTO match_row
        FROM durable_memory.candidate_embedding AS candidate_embedding JOIN durable_memory.record_embedding AS projection ON projection.model_identifier = candidate_embedding.model_identifier AND projection.dimension = candidate_embedding.dimension AND projection.lifecycle_status = 'indexed' JOIN durable_memory.record AS record ON record.id = projection.record_id
        WHERE candidate_embedding.candidate_id = candidate_row.id AND candidate_embedding.lifecycle_status = 'indexed' AND record.namespace_id = candidate_row.namespace_id AND record.record_type = candidate_row.record_type AND record.record_type <> '__inventory_definition__' AND record.status = 'active' AND projection.revision = record.revision AND durable_memory.has_capability(record.namespace_id, 'propose') ORDER BY projection.embedding <=> candidate_embedding.embedding, record.id LIMIT 1;
        IF NOT FOUND OR match_row.similarity < conflict_similarity THEN record_id := NULL; assessment := 'new'; reason := NULL; ELSE record_id := match_row.id; similarity := match_row.similarity; assessment := CASE WHEN similarity >= duplicate_similarity THEN 'duplicate' ELSE 'conflict' END; reason := format('semantic_cosine_similarity=%s;duplicate>=%s;conflict>=%s', round(similarity::numeric, 6), duplicate_similarity, conflict_similarity); END IF;
    END IF;
    UPDATE durable_memory.memory_candidate SET assessment = candidate_semantic_assessment.assessment WHERE id = candidate_row.id;
    IF record_id IS NULL THEN DELETE FROM durable_memory.candidate_record_relation WHERE candidate_id = candidate_row.id; ELSE INSERT INTO durable_memory.candidate_record_relation (candidate_id, record_id, reason) VALUES (candidate_id, record_id, reason) ON CONFLICT ON CONSTRAINT candidate_record_relation_pkey DO UPDATE SET record_id = EXCLUDED.record_id, reason = EXCLUDED.reason; END IF;
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.apply_change_request(request_id uuid, decision text, allow_auto boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE; record_row durable_memory.record%ROWTYPE;
    actor_id uuid := durable_memory.current_profile_id(); new_record_id uuid; next_revision integer; type_id uuid;
BEGIN
    SELECT * INTO request_row FROM durable_memory.change_request WHERE id = request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown change request'; END IF;
    IF decision NOT IN ('approve', 'reject') THEN RAISE EXCEPTION 'invalid decision'; END IF;
    IF actor_id IS NULL OR NOT durable_memory.has_capability(request_row.namespace_id, 'approve') THEN
        IF NOT (allow_auto AND decision = 'approve' AND request_row.policy_action = 'auto' AND request_row.requested_by_profile_id = actor_id) THEN RAISE EXCEPTION 'approval capability is required'; END IF;
    END IF;
    IF request_row.record_type = '__inventory_definition__' AND request_row.operation <> 'create' THEN RAISE EXCEPTION 'inventory definitions are immutable'; END IF;
    IF request_row.status <> 'pending' THEN RETURN; END IF;
    IF request_row.expires_at <= now() THEN UPDATE durable_memory.change_request SET status = 'expired', decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    IF decision = 'reject' THEN UPDATE durable_memory.change_request SET status = 'rejected', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    IF request_row.record_type = '__inventory_definition__' THEN
        INSERT INTO durable_memory.memory_type (namespace_id, record_type, created_by_profile_id)
        VALUES (request_row.namespace_id, request_row.identity_key, request_row.requested_by_profile_id)
        ON CONFLICT (namespace_id, record_type) DO NOTHING RETURNING id INTO type_id;
        IF type_id IS NULL THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.memory_schema_version (memory_type_id, version, fields, schema, created_by_profile_id)
        VALUES (type_id, 1, request_row.payload -> 'fields', '{}'::jsonb, actor_id);
        UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    IF request_row.operation = 'create' THEN
        new_record_id := request_row.id;
        INSERT INTO durable_memory.record (id, namespace_id, record_type, identity_key, status, revision, search_text, payload, created_by_profile_id, updated_by_profile_id)
        VALUES (new_record_id, request_row.namespace_id, request_row.record_type, request_row.identity_key, 'active', 1, request_row.search_text, request_row.payload, request_row.requested_by_profile_id, actor_id)
        ON CONFLICT (namespace_id, record_type, identity_key) WHERE status = 'active' DO NOTHING;
        IF NOT FOUND THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (new_record_id, 1, 'create', request_row.payload, actor_id);
        UPDATE durable_memory.change_request SET record_id = new_record_id, status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    SELECT * INTO record_row FROM durable_memory.record WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR record_row.status <> 'active' OR (request_row.expected_revision IS NOT NULL AND record_row.revision <> request_row.expected_revision) THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    IF record_row.record_type = '__inventory_definition__' THEN RAISE EXCEPTION 'inventory definitions are immutable'; END IF;
    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN UPDATE durable_memory.record SET revision = next_revision, search_text = request_row.search_text, payload = request_row.payload, updated_by_profile_id = actor_id WHERE id = record_row.id; ELSE UPDATE durable_memory.record SET status = 'tombstoned', revision = next_revision, updated_by_profile_id = actor_id WHERE id = record_row.id; END IF;
    INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (record_row.id, next_revision, request_row.operation, CASE WHEN request_row.operation = 'update' THEN request_row.payload ELSE record_row.payload END, actor_id);
    UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id;
END $$;

-- Search and worker queries must stay defensive for legacy records.
CREATE OR REPLACE FUNCTION durable_memory.enqueue_record_embedding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE content_digest char(64);
BEGIN
    IF NEW.record_type = '__inventory_definition__' THEN
        DELETE FROM durable_memory.record_embedding WHERE record_id = NEW.id;
        RETURN NEW;
    END IF;
    IF NEW.status <> 'active' THEN UPDATE durable_memory.record_embedding SET lifecycle_status = 'deleted', embedding = NULL, dimension = NULL, error_message = NULL WHERE record_id = NEW.id; UPDATE durable_memory.embedding_job SET status = 'cancelled' WHERE record_id = NEW.id AND status = 'pending'; RETURN NEW; END IF;
    content_digest := encode(digest(NEW.search_text, 'sha256'), 'hex');
    INSERT INTO durable_memory.record_embedding (record_id, revision, content_hash, lifecycle_status) VALUES (NEW.id, NEW.revision, content_digest, 'pending') ON CONFLICT (record_id, revision) DO NOTHING;
    INSERT INTO durable_memory.embedding_job (record_id, revision) VALUES (NEW.id, NEW.revision) ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;

REVOKE ALL ON durable_memory.memory_type, durable_memory.memory_schema_version FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.proposal_inventory_definition(uuid, text) FROM PUBLIC;
