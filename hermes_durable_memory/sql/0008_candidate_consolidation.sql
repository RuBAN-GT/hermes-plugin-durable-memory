ALTER TABLE durable_memory.change_request
    ADD COLUMN consolidated_candidate_id uuid
        REFERENCES durable_memory.memory_candidate (id);

CREATE UNIQUE INDEX change_request_consolidated_candidate
    ON durable_memory.change_request (consolidated_candidate_id)
    WHERE consolidated_candidate_id IS NOT NULL;

CREATE FUNCTION durable_memory.jsonb_merge_patch(target jsonb, patch jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE
    result jsonb := CASE WHEN jsonb_typeof(target) = 'object' THEN target ELSE '{}'::jsonb END;
    key text;
    value jsonb;
BEGIN
    IF jsonb_typeof(patch) <> 'object' THEN
        RETURN patch;
    END IF;
    FOR key, value IN SELECT * FROM jsonb_each(patch) LOOP
        IF value = 'null'::jsonb THEN
            result := result - key;
        ELSIF jsonb_typeof(value) = 'object' THEN
            result := result || jsonb_build_object(
                key, durable_memory.jsonb_merge_patch(result -> key, value)
            );
        ELSE
            result := result || jsonb_build_object(key, value);
        END IF;
    END LOOP;
    RETURN result;
END
$$;

CREATE FUNCTION durable_memory.consolidate_candidate(
    candidate_id uuid,
    request_id uuid,
    requested_policy_action text,
    ttl_seconds integer
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE
    candidate_row durable_memory.memory_candidate%ROWTYPE;
    record_row durable_memory.record%ROWTYPE;
    relation_record_id uuid;
    actor_id uuid := durable_memory.current_profile_id();
    merged_payload jsonb;
    request_key text;
    existing_request_id uuid;
BEGIN
    IF requested_policy_action NOT IN ('require', 'auto') THEN
        RAISE EXCEPTION 'candidate consolidation policy must require approval or auto-apply';
    END IF;
    IF ttl_seconds <= 0 THEN
        RAISE EXCEPTION 'candidate consolidation expiry must be positive';
    END IF;
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate
    WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'approve') THEN
        RAISE EXCEPTION 'approval capability is required';
    END IF;
    IF candidate_row.assessment NOT IN ('duplicate', 'conflict') THEN
        RAISE EXCEPTION 'memory candidate has no matching record to consolidate';
    END IF;
    IF jsonb_typeof(candidate_row.canonical_payload) <> 'object' THEN
        RAISE EXCEPTION 'candidate canonical payload must be an object';
    END IF;
    SELECT relation.record_id INTO relation_record_id
    FROM durable_memory.candidate_record_relation AS relation
    WHERE relation.candidate_id = candidate_row.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'memory candidate has no matching record to consolidate';
    END IF;
    SELECT * INTO record_row FROM durable_memory.record
    WHERE id = relation_record_id FOR UPDATE;
    IF NOT FOUND
       OR record_row.namespace_id <> candidate_row.namespace_id
       OR record_row.status <> 'active'
       OR record_row.record_type <> candidate_row.record_type
       OR record_row.identity_key <> candidate_row.identity_key THEN
        RAISE EXCEPTION 'candidate relation must target the active matching record';
    END IF;
    SELECT id INTO existing_request_id FROM durable_memory.change_request
    WHERE consolidated_candidate_id = candidate_row.id;
    IF FOUND THEN RETURN existing_request_id; END IF;

    merged_payload := durable_memory.jsonb_merge_patch(
        record_row.payload, candidate_row.canonical_payload
    ) || jsonb_build_object('identity', to_jsonb(record_row.identity_key));
    request_key := md5(
        actor_id::text || '|update|' || candidate_row.namespace_id::text || '|'
        || record_row.identity_key || '|' || merged_payload::text || '|'
        || record_row.revision::text || '|' || candidate_row.id::text
    );
    INSERT INTO durable_memory.change_request (
        id, namespace_id, record_id, operation, record_type, identity_key,
        expected_revision, payload, search_text, idempotency_key, status,
        policy_action, requested_by_profile_id, expires_at,
        consolidated_candidate_id
    ) VALUES (
        request_id, candidate_row.namespace_id, record_row.id, 'update',
        record_row.record_type, record_row.identity_key, record_row.revision,
        merged_payload, candidate_row.canonical_search_text, request_key,
        'pending', requested_policy_action, actor_id,
        now() + (ttl_seconds * interval '1 second'), candidate_row.id
    ) RETURNING id INTO existing_request_id;
    RETURN existing_request_id;
END
$$;

REVOKE ALL ON FUNCTION durable_memory.jsonb_merge_patch(jsonb, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.consolidate_candidate(uuid, uuid, text, integer)
    FROM PUBLIC;
