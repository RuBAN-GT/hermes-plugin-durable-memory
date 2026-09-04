-- Preserve the approval boundary when migrations 4--11 are already deployed.
ALTER TABLE durable_memory.memory_schema_version
    DROP CONSTRAINT memory_schema_version_memory_type_id_lifecycle_status_key;
CREATE UNIQUE INDEX memory_schema_version_one_active
    ON durable_memory.memory_schema_version (memory_type_id)
    WHERE lifecycle_status = 'active';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM durable_memory.namespace AS namespace
        JOIN durable_memory.profile AS profile ON profile.id = namespace.owner_profile_id
        WHERE namespace.kind = 'private'
          AND namespace.slug <> 'profile:' || profile.slug
    ) THEN
        RAISE EXCEPTION 'private namespace owner and slug do not match';
    END IF;
END $$;

ALTER TABLE durable_memory.embedding_job
    DROP CONSTRAINT embedding_job_status_check,
    ADD COLUMN claim_token uuid,
    ADD COLUMN claimed_at timestamptz,
    ADD CONSTRAINT embedding_job_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled'));
ALTER TABLE durable_memory.candidate_embedding_job
    DROP CONSTRAINT candidate_embedding_job_status_check,
    ADD COLUMN claim_token uuid,
    ADD COLUMN claimed_at timestamptz,
    ADD CONSTRAINT candidate_embedding_job_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled'));

-- Registry metadata supersedes legacy canonical records, retaining audit history.
UPDATE durable_memory.record
SET status = 'superseded'
WHERE record_type = '__inventory_definition__' AND status = 'active';

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
        INSERT INTO durable_memory.memory_type (namespace_id, record_type, created_by_profile_id) VALUES (request_row.namespace_id, request_row.identity_key, request_row.requested_by_profile_id) ON CONFLICT (namespace_id, record_type) DO NOTHING RETURNING id INTO type_id;
        IF type_id IS NULL THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.memory_schema_version (memory_type_id, version, fields, schema, created_by_profile_id) VALUES (type_id, 1, request_row.payload -> 'fields', '{}'::jsonb, actor_id);
        UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    IF request_row.operation = 'create' THEN
        IF request_row.valid_to IS NOT NULL AND request_row.valid_to <= COALESCE(request_row.valid_from, now()) THEN
            UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
        END IF;
        new_record_id := request_row.id;
        UPDATE durable_memory.record SET status = 'expired'
        WHERE namespace_id = request_row.namespace_id AND record_type = request_row.record_type
          AND identity_key = request_row.identity_key AND status = 'active'
          AND valid_to IS NOT NULL AND valid_to <= now();
        INSERT INTO durable_memory.record (id, namespace_id, record_type, identity_key, status, revision, search_text, payload, valid_from, valid_to, created_by_profile_id, updated_by_profile_id)
        VALUES (new_record_id, request_row.namespace_id, request_row.record_type, request_row.identity_key, 'active', 1, request_row.search_text, request_row.payload, COALESCE(request_row.valid_from, now()), request_row.valid_to, request_row.requested_by_profile_id, actor_id)
        ON CONFLICT (namespace_id, record_type, identity_key) WHERE status = 'active' DO NOTHING;
        IF NOT FOUND THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (new_record_id, 1, 'create', request_row.payload, actor_id);
        UPDATE durable_memory.change_request SET record_id = new_record_id, status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    SELECT * INTO record_row FROM durable_memory.record WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR record_row.namespace_id <> request_row.namespace_id OR record_row.record_type <> request_row.record_type OR record_row.identity_key <> request_row.identity_key OR record_row.status <> 'active' OR (request_row.expected_revision IS NOT NULL AND record_row.revision <> request_row.expected_revision) THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN
        UPDATE durable_memory.record SET revision = next_revision, search_text = request_row.search_text, payload = request_row.payload, valid_from = COALESCE(request_row.valid_from, valid_from), valid_to = COALESCE(request_row.valid_to, valid_to), updated_by_profile_id = actor_id WHERE id = record_row.id;
    ELSE UPDATE durable_memory.record SET status = 'tombstoned', revision = next_revision, updated_by_profile_id = actor_id WHERE id = record_row.id; END IF;
    INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (record_row.id, next_revision, request_row.operation, CASE WHEN request_row.operation = 'update' THEN request_row.payload ELSE record_row.payload END, actor_id);
    UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.enqueue_record_embedding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE content_digest char(64);
BEGIN
    IF NEW.record_type = '__inventory_definition__' THEN DELETE FROM durable_memory.record_embedding WHERE record_id = NEW.id; RETURN NEW; END IF;
    IF NEW.status <> 'active' OR (NEW.valid_to IS NOT NULL AND NEW.valid_to <= now()) THEN
        UPDATE durable_memory.record_embedding SET lifecycle_status = 'deleted', embedding = NULL, dimension = NULL, error_message = NULL WHERE record_id = NEW.id;
        UPDATE durable_memory.embedding_job SET status = 'cancelled', claim_token = NULL, claimed_at = NULL WHERE record_id = NEW.id AND status IN ('pending', 'processing'); RETURN NEW;
    END IF;
    content_digest := encode(digest(NEW.search_text, 'sha256'), 'hex');
    INSERT INTO durable_memory.record_embedding (record_id, revision, content_hash, lifecycle_status) VALUES (NEW.id, NEW.revision, content_digest, 'pending') ON CONFLICT (record_id, revision) DO NOTHING;
    INSERT INTO durable_memory.embedding_job (record_id, revision) VALUES (NEW.id, NEW.revision) ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
