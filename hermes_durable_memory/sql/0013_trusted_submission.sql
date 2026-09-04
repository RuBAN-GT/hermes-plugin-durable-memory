-- Runtime roles submit review documents only. Trusted functions derive all
-- authority-bearing fields and retain canonical merged payloads privately.
CREATE TABLE durable_memory.operation_policy (
    profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'delete')),
    action text NOT NULL CHECK (action IN ('auto', 'require', 'deny')),
    ttl_seconds integer NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 604800),
    PRIMARY KEY (profile_id, operation)
);
ALTER TABLE durable_memory.operation_policy ENABLE ROW LEVEL SECURITY;

CREATE TABLE durable_memory.change_request_private (
    request_id uuid PRIMARY KEY REFERENCES durable_memory.change_request (id) ON DELETE CASCADE,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    search_text text NOT NULL
);
ALTER TABLE durable_memory.change_request_private ENABLE ROW LEVEL SECURITY;

INSERT INTO durable_memory.operation_policy (profile_id, operation, action, ttl_seconds)
SELECT profile.id, operation.operation, 'require', 86400
FROM durable_memory.profile AS profile
CROSS JOIN (VALUES ('create'), ('update'), ('delete')) AS operation(operation)
ON CONFLICT DO NOTHING;

INSERT INTO durable_memory.change_request_private (request_id, payload, search_text)
SELECT id, payload, search_text FROM durable_memory.change_request
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION durable_memory.jsonb_merge_patch(target jsonb, patch jsonb)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE key text; value jsonb; result jsonb := target;
BEGIN
    FOR key, value IN SELECT * FROM jsonb_each(patch) LOOP
        IF value = 'null'::jsonb THEN result := result - key;
        ELSIF jsonb_typeof(value) = 'object' AND jsonb_typeof(result -> key) = 'object' THEN
            result := jsonb_set(result, ARRAY[key], durable_memory.jsonb_merge_patch(result -> key, value));
        ELSE result := jsonb_set(result, ARRAY[key], value, true);
        END IF;
    END LOOP;
    RETURN result;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.validate_submission_payload(
    target_namespace_id uuid, target_record_type text, candidate_payload jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE fields jsonb; field_name text; field_spec jsonb; value jsonb;
BEGIN
    IF jsonb_typeof(candidate_payload) <> 'object' THEN RAISE EXCEPTION 'record payload must be an object'; END IF;
    SELECT definition.fields INTO fields FROM durable_memory.inventory_definition AS definition
    WHERE definition.namespace_id = target_namespace_id AND definition.record_type = target_record_type;
    IF fields IS NULL THEN RETURN; END IF;
    FOR field_name, value IN SELECT * FROM jsonb_each(candidate_payload) LOOP
        IF field_name NOT IN ('identity', 'text') AND NOT fields ? field_name THEN
            RAISE EXCEPTION 'unknown inventory field: %', field_name;
        END IF;
    END LOOP;
    FOR field_name, field_spec IN SELECT * FROM jsonb_each(fields) LOOP
        value := candidate_payload -> field_name;
        IF COALESCE((field_spec ->> 'required')::boolean, false) AND value IS NULL THEN
            RAISE EXCEPTION 'required inventory field missing: %', field_name;
        END IF;
        IF value IS NOT NULL AND NOT (
            (field_spec ->> 'kind' IN ('string', 'text') AND jsonb_typeof(value) = 'string') OR
            (field_spec ->> 'kind' = 'integer' AND jsonb_typeof(value) = 'number' AND (value #>> '{}') ~ '^-?[0-9]+$') OR
            (field_spec ->> 'kind' = 'number' AND jsonb_typeof(value) = 'number') OR
            (field_spec ->> 'kind' = 'boolean' AND jsonb_typeof(value) = 'boolean') OR
            (field_spec ->> 'kind' = 'object' AND jsonb_typeof(value) = 'object') OR
            (field_spec ->> 'kind' = 'array' AND jsonb_typeof(value) = 'array')
        ) THEN RAISE EXCEPTION 'invalid inventory field type: %', field_name; END IF;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.apply_change_request(request_id uuid, decision text, allow_auto boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE; record_row durable_memory.record%ROWTYPE;
    actor_id uuid := durable_memory.current_profile_id(); canonical_payload jsonb; canonical_search_text text;
    new_record_id uuid; next_revision integer; type_id uuid;
BEGIN
    SELECT * INTO request_row FROM durable_memory.change_request WHERE id = request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown change request'; END IF;
    SELECT payload, search_text INTO canonical_payload, canonical_search_text
    FROM durable_memory.change_request_private AS private_request
    WHERE private_request.request_id = request_row.id;
    IF NOT FOUND THEN RAISE EXCEPTION 'missing trusted change request payload'; END IF;
    IF decision NOT IN ('approve', 'reject') THEN RAISE EXCEPTION 'invalid decision'; END IF;
    IF actor_id IS NULL OR NOT durable_memory.has_capability(request_row.namespace_id, 'approve') THEN
        IF NOT (allow_auto AND decision = 'approve' AND request_row.policy_action = 'auto' AND request_row.requested_by_profile_id = actor_id) THEN RAISE EXCEPTION 'approval capability is required'; END IF;
    END IF;
    IF request_row.status <> 'pending' THEN RETURN; END IF;
    IF request_row.expires_at <= now() THEN UPDATE durable_memory.change_request SET status = 'expired', decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    IF decision = 'reject' THEN UPDATE durable_memory.change_request SET status = 'rejected', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    IF request_row.record_type = '__inventory_definition__' THEN
        IF request_row.operation <> 'create' OR jsonb_typeof(canonical_payload -> 'fields') <> 'object' THEN RAISE EXCEPTION 'invalid inventory definition'; END IF;
        INSERT INTO durable_memory.memory_type (namespace_id, record_type, created_by_profile_id) VALUES (request_row.namespace_id, request_row.identity_key, request_row.requested_by_profile_id) ON CONFLICT (namespace_id, record_type) DO NOTHING RETURNING id INTO type_id;
        IF type_id IS NULL THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.memory_schema_version (memory_type_id, version, fields, schema, created_by_profile_id) VALUES (type_id, 1, canonical_payload -> 'fields', '{}'::jsonb, actor_id);
        UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    PERFORM durable_memory.validate_submission_payload(request_row.namespace_id, request_row.record_type, canonical_payload);
    IF request_row.operation = 'create' THEN
        IF request_row.valid_to IS NOT NULL AND request_row.valid_to <= COALESCE(request_row.valid_from, now()) THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        new_record_id := request_row.id;
        INSERT INTO durable_memory.record (id, namespace_id, record_type, identity_key, status, revision, search_text, payload, valid_from, valid_to, created_by_profile_id, updated_by_profile_id)
        VALUES (new_record_id, request_row.namespace_id, request_row.record_type, request_row.identity_key, 'active', 1, canonical_search_text, canonical_payload, COALESCE(request_row.valid_from, now()), request_row.valid_to, request_row.requested_by_profile_id, actor_id)
        ON CONFLICT (namespace_id, record_type, identity_key) WHERE status = 'active' DO NOTHING;
        IF NOT FOUND THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
        INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (new_record_id, 1, 'create', canonical_payload, actor_id);
        UPDATE durable_memory.change_request SET record_id = new_record_id, status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    SELECT * INTO record_row FROM durable_memory.record WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR record_row.namespace_id <> request_row.namespace_id OR record_row.record_type <> request_row.record_type OR record_row.identity_key <> request_row.identity_key OR record_row.status <> 'active' OR (request_row.expected_revision IS NOT NULL AND record_row.revision <> request_row.expected_revision) THEN UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN; END IF;
    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN UPDATE durable_memory.record SET revision = next_revision, search_text = canonical_search_text, payload = canonical_payload, valid_from = COALESCE(request_row.valid_from, valid_from), valid_to = COALESCE(request_row.valid_to, valid_to), updated_by_profile_id = actor_id WHERE id = record_row.id; ELSE UPDATE durable_memory.record SET status = 'tombstoned', revision = next_revision, updated_by_profile_id = actor_id WHERE id = record_row.id; END IF;
    INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id) VALUES (record_row.id, next_revision, request_row.operation, CASE WHEN request_row.operation = 'update' THEN canonical_payload ELSE record_row.payload END, actor_id);
    UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.submit_change_request(
    target_namespace_id uuid, target_record_id uuid, requested_operation text,
    requested_record_type text, requested_identity_key text, submitted_payload jsonb,
    submitted_search_text text, submitted_expected_revision integer,
    submitted_valid_from timestamptz, submitted_valid_to timestamptz, submission_key text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE requester_id uuid := durable_memory.current_profile_id(); policy_action text; policy_ttl integer;
    target durable_memory.record%ROWTYPE; final_payload jsonb; final_search_text text; request_id uuid;
    final_type text; final_identity text; review_payload jsonb;
BEGIN
    IF requester_id IS NULL OR NOT durable_memory.has_capability(target_namespace_id, 'propose') THEN RAISE EXCEPTION 'proposal capability is required'; END IF;
    IF requested_operation NOT IN ('create', 'update', 'delete') THEN RAISE EXCEPTION 'invalid operation'; END IF;
    SELECT action, ttl_seconds INTO policy_action, policy_ttl FROM durable_memory.operation_policy WHERE profile_id = requester_id AND operation = requested_operation;
    policy_action := COALESCE(policy_action, 'require'); policy_ttl := COALESCE(policy_ttl, 86400);
    IF policy_action = 'deny' THEN RAISE EXCEPTION 'operation denied by policy'; END IF;
    IF requested_operation = 'create' THEN
        IF target_record_id IS NOT NULL OR requested_record_type = '' OR requested_identity_key = '' THEN RAISE EXCEPTION 'create request is invalid'; END IF;
        final_type := requested_record_type; final_identity := requested_identity_key; final_payload := submitted_payload;
        IF final_payload ->> 'identity' IS DISTINCT FROM final_identity THEN RAISE EXCEPTION 'payload identity does not match request identity'; END IF;
        final_search_text := submitted_search_text; review_payload := submitted_payload;
    ELSE
        IF target_record_id IS NULL THEN RAISE EXCEPTION 'mutation requires a record id'; END IF;
        SELECT * INTO target FROM durable_memory.record WHERE id = target_record_id;
        IF NOT FOUND OR target.namespace_id <> target_namespace_id OR target.status <> 'active' THEN RAISE EXCEPTION 'record is not an active member of the requested namespace'; END IF;
        IF (requested_record_type <> '' AND requested_record_type <> target.record_type) OR (requested_identity_key <> '' AND requested_identity_key <> target.identity_key) THEN RAISE EXCEPTION 'record type or identity does not match target'; END IF;
        final_type := target.record_type; final_identity := target.identity_key;
        IF requested_operation = 'update' THEN
            IF submitted_payload ? 'identity' AND submitted_payload ->> 'identity' <> final_identity THEN RAISE EXCEPTION 'payload identity does not match target'; END IF;
            review_payload := submitted_payload - 'identity'; final_payload := durable_memory.jsonb_merge_patch(target.payload, review_payload) || jsonb_build_object('identity', final_identity);
            final_search_text := CASE WHEN submitted_search_text = '' THEN target.search_text ELSE submitted_search_text END;
        ELSE review_payload := '{}'::jsonb; final_payload := target.payload; final_search_text := target.search_text; END IF;
    END IF;
    PERFORM durable_memory.validate_submission_payload(target_namespace_id, final_type, final_payload);
    INSERT INTO durable_memory.change_request (id, namespace_id, record_id, operation, record_type, identity_key, expected_revision, payload, search_text, valid_from, valid_to, idempotency_key, status, policy_action, requested_by_profile_id, expires_at)
    VALUES (gen_random_uuid(), target_namespace_id, target_record_id, requested_operation, final_type, final_identity, submitted_expected_revision, review_payload, submitted_search_text, submitted_valid_from, submitted_valid_to, submission_key, 'pending', policy_action, requester_id, now() + (policy_ttl * interval '1 second'))
    ON CONFLICT (idempotency_key) DO UPDATE SET id = durable_memory.change_request.id RETURNING id INTO request_id;
    INSERT INTO durable_memory.change_request_private (request_id, payload, search_text) VALUES (request_id, final_payload, final_search_text) ON CONFLICT DO NOTHING;
    IF policy_action = 'auto' THEN PERFORM durable_memory.apply_change_request(request_id, 'approve', true); END IF;
    RETURN request_id;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.consolidate_candidate(
    candidate_id uuid, request_id uuid, requested_policy_action text, ttl_seconds integer
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE candidate_row durable_memory.memory_candidate%ROWTYPE;
    record_row durable_memory.record%ROWTYPE; actor_id uuid := durable_memory.current_profile_id();
    existing_request_id uuid; request_key text;
BEGIN
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate
    WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF actor_id IS NULL OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'approve') THEN
        RAISE EXCEPTION 'approval capability is required';
    END IF;
    IF candidate_row.assessment NOT IN ('duplicate', 'conflict')
       OR jsonb_typeof(candidate_row.canonical_payload) <> 'object' THEN
        RAISE EXCEPTION 'memory candidate cannot be consolidated';
    END IF;
    SELECT record.* INTO record_row FROM durable_memory.candidate_record_relation AS relation
    JOIN durable_memory.record AS record ON record.id = relation.record_id
    WHERE relation.candidate_id = candidate_row.id FOR UPDATE OF record;
    IF NOT FOUND OR record_row.namespace_id <> candidate_row.namespace_id
       OR record_row.status <> 'active' OR record_row.record_type <> candidate_row.record_type
       OR record_row.identity_key <> candidate_row.identity_key THEN
        RAISE EXCEPTION 'candidate relation must target the active matching record';
    END IF;
    SELECT id INTO existing_request_id FROM durable_memory.change_request
    WHERE consolidated_candidate_id = candidate_row.id;
    IF FOUND THEN RETURN existing_request_id; END IF;
    request_key := md5(actor_id::text || '|update|' || candidate_row.namespace_id::text
        || '|' || record_row.identity_key || '|' || candidate_row.canonical_payload::text
        || '|' || record_row.revision::text || '|' || candidate_row.id::text);
    existing_request_id := durable_memory.submit_change_request(
        candidate_row.namespace_id, record_row.id, 'update', '', '',
        candidate_row.canonical_payload, candidate_row.canonical_search_text,
        record_row.revision, NULL, NULL, request_key
    );
    UPDATE durable_memory.change_request SET consolidated_candidate_id = candidate_row.id
    WHERE id = existing_request_id;
    RETURN existing_request_id;
END $$;

-- Proposal inspection is metadata-only; update construction occurs above.
CREATE OR REPLACE FUNCTION durable_memory.proposal_record(target_record_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE record_row durable_memory.record%ROWTYPE;
BEGIN
    SELECT * INTO record_row FROM durable_memory.record WHERE id = target_record_id;
    IF NOT FOUND OR NOT durable_memory.has_capability(record_row.namespace_id, 'propose') THEN RAISE EXCEPTION 'proposal capability is required'; END IF;
    RETURN jsonb_build_object('id', record_row.id, 'namespace_id', record_row.namespace_id, 'record_type', record_row.record_type, 'identity_key', record_row.identity_key, 'status', record_row.status, 'revision', record_row.revision);
END $$;

REVOKE ALL ON TABLE durable_memory.operation_policy, durable_memory.change_request_private FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON TABLE durable_memory.record, durable_memory.record_revision, durable_memory.change_request, durable_memory.memory_type, durable_memory.memory_schema_version FROM PUBLIC;
DO $$
DECLARE runtime_role name;
BEGIN
    FOR runtime_role IN SELECT profile.runtime_role FROM durable_memory.profile AS profile LOOP
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON TABLE durable_memory.record, durable_memory.record_revision, durable_memory.change_request, durable_memory.memory_type, durable_memory.memory_schema_version FROM %I', runtime_role);
        EXECUTE format('REVOKE EXECUTE ON FUNCTION durable_memory.apply_change_request(uuid, text, boolean) FROM %I', runtime_role);
        EXECUTE format('REVOKE EXECUTE ON FUNCTION durable_memory.auto_apply_change_request(uuid) FROM %I', runtime_role);
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION durable_memory.apply_change_request(uuid, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.auto_apply_change_request(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.submit_change_request(uuid, uuid, text, text, text, jsonb, text, integer, timestamptz, timestamptz, text) TO PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.decide_change_request(uuid, text) TO PUBLIC;
