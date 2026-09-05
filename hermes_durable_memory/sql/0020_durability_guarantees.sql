-- INSERT ... RETURNING is subject to the SELECT policy. Check ownership from
-- the row itself so a profile can observe its first private namespace safely.
DROP POLICY namespace_select ON durable_memory.namespace;
CREATE POLICY namespace_select ON durable_memory.namespace
    FOR SELECT
    USING (
        owner_profile_id = durable_memory.current_profile_id()
        OR durable_memory.has_capability(id, 'read')
        OR durable_memory.has_capability(id, 'propose')
        OR durable_memory.has_capability(id, 'approve')
        OR durable_memory.has_capability(id, 'admin')
    );

-- Migration 0017 could leave recent legacy processing rows without a lease.
WITH recovered AS (
    UPDATE durable_memory.embedding_job
    SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
        last_error = CASE WHEN attempts >= max_attempts
            THEN 'embedding lease expired' ELSE NULL END,
        failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
        claim_token = NULL,
        claimed_at = NULL,
        lease_expires_at = NULL
    WHERE status = 'processing' AND lease_expires_at IS NULL
    RETURNING record_id, revision, status, last_error
)
UPDATE durable_memory.record_embedding AS projection
SET lifecycle_status = recovered.status,
    error_message = recovered.last_error,
    failed_at = CASE WHEN recovered.status = 'failed' THEN now() ELSE NULL END
FROM recovered
WHERE (projection.record_id, projection.revision) =
    (recovered.record_id, recovered.revision);

WITH recovered AS (
    UPDATE durable_memory.candidate_embedding_job
    SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
        last_error = CASE WHEN attempts >= max_attempts
            THEN 'candidate embedding lease expired' ELSE NULL END,
        failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
        claim_token = NULL,
        claimed_at = NULL,
        lease_expires_at = NULL
    WHERE status = 'processing' AND lease_expires_at IS NULL
    RETURNING candidate_id, status, last_error
)
UPDATE durable_memory.candidate_embedding AS projection
SET lifecycle_status = recovered.status,
    error_message = recovered.last_error,
    failed_at = CASE WHEN recovered.status = 'failed' THEN now() ELSE NULL END
FROM recovered
WHERE projection.candidate_id = recovered.candidate_id;

-- Import progress belongs to a profile even when source and scope collide.
ALTER TABLE durable_memory.import_checkpoint
    ADD COLUMN profile_id uuid REFERENCES durable_memory.profile (id);
UPDATE durable_memory.import_checkpoint
SET profile_id = updated_by_profile_id;
ALTER TABLE durable_memory.import_checkpoint
    ALTER COLUMN profile_id SET NOT NULL,
    DROP CONSTRAINT import_checkpoint_pkey,
    ADD PRIMARY KEY (profile_id, source_name, scope);
DROP POLICY import_checkpoint_owner ON durable_memory.import_checkpoint;
CREATE POLICY import_checkpoint_owner ON durable_memory.import_checkpoint FOR SELECT
    USING (profile_id = durable_memory.current_profile_id());

CREATE OR REPLACE FUNCTION durable_memory.save_import_checkpoint(
    source text, target_scope text, next_checkpoint text, next_report jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id();
BEGIN
    IF actor_id IS NULL OR source = '' OR target_scope = '' THEN
        RAISE EXCEPTION 'valid importer identity is required';
    END IF;
    INSERT INTO durable_memory.import_checkpoint (
        profile_id, source_name, scope, checkpoint, report, updated_by_profile_id
    ) VALUES (actor_id, source, target_scope, next_checkpoint, next_report, actor_id)
    ON CONFLICT (profile_id, source_name, scope) DO UPDATE
    SET checkpoint = EXCLUDED.checkpoint,
        report = EXCLUDED.report,
        updated_by_profile_id = EXCLUDED.updated_by_profile_id,
        updated_at = now();
END $$;

CREATE OR REPLACE FUNCTION durable_memory.load_import_checkpoint(
    source text, target_scope text
) RETURNS TABLE (checkpoint text, report jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
BEGIN
    RETURN QUERY
    SELECT item.checkpoint, item.report
    FROM durable_memory.import_checkpoint AS item
    WHERE item.profile_id = durable_memory.current_profile_id()
      AND item.source_name = source
      AND item.scope = target_scope;
END $$;

-- The update mode is part of the trusted boundary. Remove the unreleased old
-- signature rather than retaining a compatibility path with implicit merge.
ALTER TABLE durable_memory.change_request
    ADD COLUMN update_mode text NOT NULL DEFAULT 'patch'
    CHECK (update_mode IN ('patch', 'replace'));

CREATE FUNCTION durable_memory.lock_memory_record(target_record_id uuid)
RETURNS void LANGUAGE sql
SET search_path = durable_memory, pg_temp AS $$
    SELECT pg_advisory_xact_lock(hashtextextended(target_record_id::text, 0))
$$;
REVOKE ALL ON FUNCTION durable_memory.lock_memory_record(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION durable_memory.candidate_semantic_assessment(
    candidate_id uuid, duplicate_similarity double precision DEFAULT 0.98,
    conflict_similarity double precision DEFAULT 0.80
) RETURNS TABLE (record_id uuid, assessment text, reason text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE candidate_snapshot durable_memory.memory_candidate%ROWTYPE;
    candidate_row durable_memory.memory_candidate%ROWTYPE;
    exact_row durable_memory.record%ROWTYPE;
    match_row record; similarity double precision;
    relation_record_id uuid; current_relation_record_id uuid;
    prospective_record_id uuid; locked_record_id uuid;
BEGIN
    IF duplicate_similarity <= 0 OR duplicate_similarity > 1
       OR conflict_similarity <= 0
       OR conflict_similarity >= duplicate_similarity THEN
        RAISE EXCEPTION 'semantic similarity thresholds must satisfy 0 < conflict < duplicate <= 1';
    END IF;
    SELECT * INTO candidate_snapshot FROM durable_memory.memory_candidate
    WHERE id = candidate_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF candidate_snapshot.submitted_by_profile_id <> durable_memory.current_profile_id()
       OR NOT durable_memory.has_capability(candidate_snapshot.namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    SELECT relation.record_id INTO relation_record_id
    FROM durable_memory.candidate_record_relation AS relation
    WHERE relation.candidate_id = $1;
    SELECT * INTO exact_row FROM durable_memory.record
    WHERE namespace_id = candidate_snapshot.namespace_id
      AND record_type = candidate_snapshot.record_type
      AND identity_key = candidate_snapshot.identity_key
      AND status = 'active' AND valid_from <= now()
      AND (valid_to IS NULL OR valid_to > now())
      AND record_type <> '__inventory_definition__'
    ORDER BY id LIMIT 1;
    IF FOUND THEN
        prospective_record_id := exact_row.id;
    ELSE
        SELECT record.id,
               1 - (projection.embedding <=> candidate_embedding.embedding) AS similarity
        INTO match_row
        FROM durable_memory.candidate_embedding AS candidate_embedding
        JOIN durable_memory.record_embedding AS projection
          ON projection.model_identifier = candidate_embedding.model_identifier
         AND projection.dimension = candidate_embedding.dimension
         AND projection.lifecycle_status = 'indexed'
        JOIN durable_memory.record AS record ON record.id = projection.record_id
        WHERE candidate_embedding.candidate_id = candidate_snapshot.id
          AND candidate_embedding.lifecycle_status = 'indexed'
          AND record.namespace_id = candidate_snapshot.namespace_id
          AND record.record_type = candidate_snapshot.record_type
          AND record.record_type <> '__inventory_definition__'
          AND record.status = 'active' AND record.valid_from <= now()
          AND (record.valid_to IS NULL OR record.valid_to > now())
          AND projection.revision = record.revision
          AND durable_memory.has_capability(record.namespace_id, 'propose')
        ORDER BY projection.embedding <=> candidate_embedding.embedding, record.id
        LIMIT 1;
        IF FOUND AND match_row.similarity >= conflict_similarity THEN
            prospective_record_id := match_row.id;
        END IF;
    END IF;
    FOR locked_record_id IN
        SELECT target.record_id
        FROM (VALUES (relation_record_id), (prospective_record_id)) AS target(record_id)
        WHERE target.record_id IS NOT NULL
        GROUP BY target.record_id
        ORDER BY hashtextextended(target.record_id::text, 0), target.record_id
    LOOP
        PERFORM durable_memory.lock_memory_record(locked_record_id);
    END LOOP;
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate
    WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF candidate_row.submitted_by_profile_id <> durable_memory.current_profile_id()
       OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    SELECT relation.record_id INTO current_relation_record_id
    FROM durable_memory.candidate_record_relation AS relation
    WHERE relation.candidate_id = $1;
    IF current_relation_record_id IS NOT NULL
       AND current_relation_record_id IS DISTINCT FROM relation_record_id
       AND current_relation_record_id IS DISTINCT FROM prospective_record_id THEN
        RAISE EXCEPTION 'semantic assessment target changed during evaluation';
    END IF;
    SELECT * INTO exact_row FROM durable_memory.record
    WHERE namespace_id = candidate_row.namespace_id
      AND record_type = candidate_row.record_type
      AND identity_key = candidate_row.identity_key
      AND status = 'active' AND valid_from <= now()
      AND (valid_to IS NULL OR valid_to > now())
      AND record_type <> '__inventory_definition__'
    ORDER BY id LIMIT 1;
    IF FOUND THEN
        assessment := CASE
            WHEN exact_row.payload = candidate_row.canonical_payload
             AND exact_row.search_text = candidate_row.canonical_search_text
            THEN 'duplicate' ELSE 'conflict' END;
        reason := CASE WHEN assessment = 'duplicate'
            THEN 'exact_identity_and_equal_content'
            ELSE 'exact_identity_with_different_content' END;
        record_id := exact_row.id;
    ELSE
        SELECT record.id,
               1 - (projection.embedding <=> candidate_embedding.embedding) AS similarity
        INTO match_row
        FROM durable_memory.candidate_embedding AS candidate_embedding
        JOIN durable_memory.record_embedding AS projection
          ON projection.model_identifier = candidate_embedding.model_identifier
         AND projection.dimension = candidate_embedding.dimension
         AND projection.lifecycle_status = 'indexed'
        JOIN durable_memory.record AS record ON record.id = projection.record_id
        WHERE candidate_embedding.candidate_id = candidate_row.id
          AND candidate_embedding.lifecycle_status = 'indexed'
          AND record.namespace_id = candidate_row.namespace_id
          AND record.record_type = candidate_row.record_type
          AND record.record_type <> '__inventory_definition__'
          AND record.status = 'active' AND record.valid_from <= now()
          AND (record.valid_to IS NULL OR record.valid_to > now())
          AND projection.revision = record.revision
          AND durable_memory.has_capability(record.namespace_id, 'propose')
        ORDER BY projection.embedding <=> candidate_embedding.embedding, record.id
        LIMIT 1;
        IF NOT FOUND OR match_row.similarity < conflict_similarity THEN
            record_id := NULL; assessment := 'new'; reason := NULL;
        ELSE
            record_id := match_row.id;
            similarity := match_row.similarity;
            assessment := CASE WHEN similarity >= duplicate_similarity
                THEN 'duplicate' ELSE 'conflict' END;
            reason := format(
                'semantic_cosine_similarity=%s;duplicate>=%s;conflict>=%s',
                round(similarity::numeric, 6), duplicate_similarity,
                conflict_similarity
            );
        END IF;
    END IF;
    IF record_id IS NOT NULL
       AND record_id IS DISTINCT FROM relation_record_id
       AND record_id IS DISTINCT FROM prospective_record_id THEN
        RAISE EXCEPTION 'semantic assessment target changed during evaluation';
    END IF;
    UPDATE durable_memory.memory_candidate
    SET assessment = candidate_semantic_assessment.assessment
    WHERE id = candidate_row.id;
    IF record_id IS NULL THEN
        DELETE FROM durable_memory.candidate_record_relation
        WHERE candidate_id = candidate_row.id;
    ELSE
        INSERT INTO durable_memory.candidate_record_relation (
            candidate_id, record_id, reason
        ) VALUES (candidate_id, record_id, reason)
        ON CONFLICT ON CONSTRAINT candidate_record_relation_pkey DO UPDATE
        SET record_id = EXCLUDED.record_id, reason = EXCLUDED.reason;
    END IF;
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.apply_change_request(
    request_id uuid, decision text, allow_auto boolean
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE;
    record_row durable_memory.record%ROWTYPE;
    actor_id uuid := durable_memory.current_profile_id();
    canonical_payload jsonb; canonical_search_text text;
    locked_record_id uuid; new_record_id uuid; next_revision integer; type_id uuid;
BEGIN
    SELECT record_id INTO locked_record_id
    FROM durable_memory.change_request WHERE id = request_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown change request'; END IF;
    IF locked_record_id IS NOT NULL THEN
        PERFORM durable_memory.lock_memory_record(locked_record_id);
    END IF;
    SELECT * INTO request_row FROM durable_memory.change_request
    WHERE id = request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown change request'; END IF;
    SELECT payload, search_text INTO canonical_payload, canonical_search_text
    FROM durable_memory.change_request_private AS private_request
    WHERE private_request.request_id = request_row.id;
    IF NOT FOUND THEN RAISE EXCEPTION 'missing trusted change request payload'; END IF;
    IF decision NOT IN ('approve', 'reject') THEN RAISE EXCEPTION 'invalid decision'; END IF;
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(request_row.namespace_id, 'approve') THEN
        IF NOT (allow_auto AND decision = 'approve'
                AND request_row.policy_action = 'auto'
                AND request_row.requested_by_profile_id = actor_id) THEN
            RAISE EXCEPTION 'approval capability is required';
        END IF;
    END IF;
    IF request_row.status <> 'pending' THEN RETURN; END IF;
    IF request_row.expires_at <= now() THEN
        UPDATE durable_memory.change_request SET status = 'expired', decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    IF decision = 'reject' THEN
        UPDATE durable_memory.change_request
        SET status = 'rejected', decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    IF request_row.record_type = '__inventory_definition__' THEN
        IF request_row.operation <> 'create'
           OR jsonb_typeof(canonical_payload -> 'fields') <> 'object' THEN
            RAISE EXCEPTION 'invalid inventory definition';
        END IF;
        INSERT INTO durable_memory.memory_type (
            namespace_id, record_type, created_by_profile_id
        ) VALUES (
            request_row.namespace_id, request_row.identity_key,
            request_row.requested_by_profile_id
        ) ON CONFLICT (namespace_id, record_type) DO NOTHING
        RETURNING id INTO type_id;
        IF type_id IS NULL THEN
            UPDATE durable_memory.change_request
            SET status = 'superseded', decided_by_profile_id = actor_id,
                decided_at = now()
            WHERE id = request_row.id;
            RETURN;
        END IF;
        INSERT INTO durable_memory.memory_schema_version (
            memory_type_id, version, fields, schema, created_by_profile_id
        ) VALUES (type_id, 1, canonical_payload -> 'fields', '{}'::jsonb, actor_id);
        UPDATE durable_memory.change_request
        SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    PERFORM durable_memory.validate_submission_payload(
        request_row.namespace_id, request_row.record_type, canonical_payload
    );
    IF request_row.operation = 'create' THEN
        IF request_row.valid_to IS NOT NULL
           AND request_row.valid_to <= COALESCE(request_row.valid_from, now()) THEN
            UPDATE durable_memory.change_request
            SET status = 'superseded', decided_by_profile_id = actor_id,
                decided_at = now()
            WHERE id = request_row.id;
            RETURN;
        END IF;
        new_record_id := request_row.id;
        INSERT INTO durable_memory.record (
            id, namespace_id, record_type, identity_key, status, revision,
            search_text, payload, valid_from, valid_to, created_by_profile_id,
            updated_by_profile_id
        ) VALUES (
            new_record_id, request_row.namespace_id, request_row.record_type,
            request_row.identity_key, 'active', 1, canonical_search_text,
            canonical_payload, COALESCE(request_row.valid_from, now()),
            request_row.valid_to, request_row.requested_by_profile_id, actor_id
        ) ON CONFLICT (namespace_id, record_type, identity_key)
            WHERE status = 'active' DO NOTHING;
        IF NOT FOUND THEN
            UPDATE durable_memory.change_request
            SET status = 'superseded', decided_by_profile_id = actor_id,
                decided_at = now()
            WHERE id = request_row.id;
            RETURN;
        END IF;
        INSERT INTO durable_memory.record_revision (
            record_id, revision, operation, payload, actor_profile_id
        ) VALUES (new_record_id, 1, 'create', canonical_payload, actor_id);
        UPDATE durable_memory.change_request
        SET record_id = new_record_id, status = 'approved',
            decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    SELECT * INTO record_row FROM durable_memory.record
    WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR record_row.namespace_id <> request_row.namespace_id
       OR record_row.record_type <> request_row.record_type
       OR record_row.identity_key <> request_row.identity_key
       OR record_row.status <> 'active'
       OR (request_row.expected_revision IS NOT NULL
           AND record_row.revision <> request_row.expected_revision) THEN
        UPDATE durable_memory.change_request
        SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN
        UPDATE durable_memory.record
        SET revision = next_revision, search_text = canonical_search_text,
            payload = canonical_payload,
            valid_from = COALESCE(request_row.valid_from, valid_from),
            valid_to = COALESCE(request_row.valid_to, valid_to),
            updated_by_profile_id = actor_id
        WHERE id = record_row.id;
    ELSE
        UPDATE durable_memory.record
        SET status = 'tombstoned', revision = next_revision,
            updated_by_profile_id = actor_id
        WHERE id = record_row.id;
    END IF;
    INSERT INTO durable_memory.record_revision (
        record_id, revision, operation, payload, actor_profile_id
    ) VALUES (
        record_row.id, next_revision, request_row.operation,
        CASE WHEN request_row.operation = 'update'
            THEN canonical_payload ELSE record_row.payload END,
        actor_id
    );
    UPDATE durable_memory.change_request
    SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now()
    WHERE id = request_row.id;
END $$;

INSERT INTO durable_memory.operation_policy (
    profile_id, operation, action, ttl_seconds
)
SELECT profile.id, operation.name, 'require', 86400
FROM durable_memory.profile AS profile
CROSS JOIN (VALUES ('create'), ('update'), ('delete')) AS operation(name)
ON CONFLICT (profile_id, operation) DO NOTHING;

CREATE FUNCTION durable_memory.current_operation_policy()
RETURNS TABLE (operation text, action text, ttl_seconds integer)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE requester_id uuid := durable_memory.current_profile_id();
BEGIN
    IF requester_id IS NULL THEN RAISE EXCEPTION 'profile policy is unavailable'; END IF;
    RETURN QUERY
    SELECT policy.operation, policy.action, policy.ttl_seconds
    FROM durable_memory.operation_policy AS policy
    WHERE policy.profile_id = requester_id
    ORDER BY policy.operation;
END $$;

DROP FUNCTION durable_memory.submit_change_request(
    uuid, uuid, text, text, text, jsonb, text, integer,
    timestamptz, timestamptz, text
);

CREATE FUNCTION durable_memory.submit_change_request(
    target_namespace_id uuid, target_record_id uuid, requested_operation text,
    requested_record_type text, requested_identity_key text, submitted_payload jsonb,
    submitted_search_text text, submitted_expected_revision integer,
    submitted_valid_from timestamptz, submitted_valid_to timestamptz,
    requested_update_mode text, submission_key text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE requester_id uuid := durable_memory.current_profile_id();
    policy_action text; policy_ttl integer; target durable_memory.record%ROWTYPE;
    final_payload jsonb; final_search_text text; request_id uuid;
    final_type text; final_identity text; review_payload jsonb; definition_fields jsonb;
    captured_expected_revision integer;
BEGIN
    IF requester_id IS NULL
       OR NOT durable_memory.has_capability(target_namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    IF requested_operation NOT IN ('create', 'update', 'delete') THEN
        RAISE EXCEPTION 'invalid operation';
    END IF;
    IF requested_update_mode NOT IN ('patch', 'replace') THEN
        RAISE EXCEPTION 'invalid update mode';
    END IF;
    SELECT action, ttl_seconds INTO policy_action, policy_ttl
    FROM durable_memory.operation_policy
    WHERE profile_id = requester_id AND operation = requested_operation;
    IF NOT FOUND THEN RAISE EXCEPTION 'operation policy is not configured'; END IF;
    IF policy_action = 'deny' THEN RAISE EXCEPTION 'operation denied by policy'; END IF;
    IF requested_operation = 'create' THEN
        IF target_record_id IS NOT NULL OR requested_record_type = ''
           OR requested_identity_key = '' THEN
            RAISE EXCEPTION 'create request is invalid';
        END IF;
        final_type := requested_record_type;
        final_identity := requested_identity_key;
        final_payload := submitted_payload;
        IF final_payload ->> 'identity' IS DISTINCT FROM final_identity THEN
            RAISE EXCEPTION 'payload identity does not match request identity';
        END IF;
        final_search_text := submitted_search_text;
        review_payload := submitted_payload;
    ELSE
        IF target_record_id IS NULL THEN RAISE EXCEPTION 'mutation requires a record id'; END IF;
        PERFORM durable_memory.lock_memory_record(target_record_id);
        SELECT * INTO target FROM durable_memory.record
        WHERE id = target_record_id FOR UPDATE;
        IF NOT FOUND OR target.namespace_id <> target_namespace_id
           OR target.status <> 'active' THEN
            RAISE EXCEPTION 'record is not an active member of the requested namespace';
        END IF;
        IF (requested_record_type <> '' AND requested_record_type <> target.record_type)
           OR (requested_identity_key <> '' AND requested_identity_key <> target.identity_key) THEN
            RAISE EXCEPTION 'record type or identity does not match target';
        END IF;
        final_type := target.record_type;
        final_identity := target.identity_key;
        captured_expected_revision := COALESCE(
            submitted_expected_revision, target.revision
        );
        IF requested_operation = 'update' THEN
            IF submitted_payload ? 'identity'
               AND submitted_payload ->> 'identity' <> final_identity THEN
                RAISE EXCEPTION 'payload identity does not match target';
            END IF;
            review_payload := submitted_payload - 'identity';
            IF requested_update_mode = 'replace' THEN
                final_payload := review_payload || jsonb_build_object('identity', final_identity);
            ELSE
                final_payload := durable_memory.jsonb_merge_patch(target.payload, review_payload)
                    || jsonb_build_object('identity', final_identity);
            END IF;
            final_search_text := CASE WHEN submitted_search_text = ''
                THEN target.search_text ELSE submitted_search_text END;
        ELSE
            review_payload := '{}'::jsonb;
            final_payload := target.payload;
            final_search_text := target.search_text;
        END IF;
    END IF;
    PERFORM durable_memory.validate_submission_payload(
        target_namespace_id, final_type, final_payload
    );
    IF requested_operation = 'update' AND submitted_search_text = '' THEN
        SELECT definition.fields INTO definition_fields
        FROM durable_memory.inventory_definition AS definition
        WHERE definition.namespace_id = target_namespace_id
          AND definition.record_type = final_type;
        IF definition_fields IS NOT NULL THEN
            SELECT COALESCE(string_agg(
                CASE WHEN jsonb_typeof(final_payload -> field.name) = 'string'
                    THEN final_payload ->> field.name
                    ELSE (final_payload -> field.name)::text END,
                ' ' ORDER BY field.name
            ), '') INTO final_search_text
            FROM jsonb_each(definition_fields) AS field(name, specification)
            WHERE final_payload ? field.name
              AND (COALESCE((field.specification ->> 'searchable')::boolean, false)
                   OR COALESCE((field.specification ->> 'semantic')::boolean, false));
        END IF;
    END IF;
    submission_key := encode(digest(convert_to(jsonb_build_object(
        'requester_id', requester_id,
        'namespace_id', target_namespace_id,
        'operation', requested_operation,
        'target_record_id', target_record_id,
        'record_type', final_type,
        'identity_key', final_identity,
        'submitted_payload', submitted_payload,
        'submitted_search_text', submitted_search_text,
        'submitted_expected_revision', submitted_expected_revision,
        'submitted_valid_from_epoch', extract(epoch FROM submitted_valid_from),
        'submitted_valid_to_epoch', extract(epoch FROM submitted_valid_to),
        'update_mode', requested_update_mode,
        'submission_key', submission_key
    )::text, 'UTF8'), 'sha256'), 'hex');
    INSERT INTO durable_memory.change_request (
        id, namespace_id, record_id, operation, record_type, identity_key,
        expected_revision, update_mode, payload, search_text, valid_from, valid_to,
        idempotency_key, status, policy_action, requested_by_profile_id, expires_at
    ) VALUES (
        gen_random_uuid(), target_namespace_id, target_record_id, requested_operation,
        final_type, final_identity, captured_expected_revision, requested_update_mode,
        review_payload,
        submitted_search_text, submitted_valid_from, submitted_valid_to, submission_key,
        'pending', policy_action, requester_id, now() + (policy_ttl * interval '1 second')
    ) ON CONFLICT (idempotency_key) DO UPDATE
        SET id = durable_memory.change_request.id
    RETURNING id INTO request_id;
    INSERT INTO durable_memory.change_request_private (request_id, payload, search_text)
    VALUES (request_id, final_payload, final_search_text) ON CONFLICT DO NOTHING;
    IF policy_action = 'auto' THEN
        PERFORM durable_memory.apply_change_request(request_id, 'approve', true);
    END IF;
    RETURN request_id;
END $$;

-- Candidate consolidation is always an RFC 7396 patch.
CREATE OR REPLACE FUNCTION durable_memory.consolidate_candidate(
    candidate_id uuid, request_id uuid, requested_policy_action text, ttl_seconds integer
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE candidate_row durable_memory.memory_candidate%ROWTYPE;
    record_row durable_memory.record%ROWTYPE; actor_id uuid := durable_memory.current_profile_id();
    existing_request_id uuid; request_key text; locked_record_id uuid;
BEGIN
    SELECT relation.record_id INTO locked_record_id
    FROM durable_memory.candidate_record_relation AS relation
    WHERE relation.candidate_id = $1;
    IF FOUND THEN PERFORM durable_memory.lock_memory_record(locked_record_id); END IF;
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate
    WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'approve') THEN
        RAISE EXCEPTION 'approval capability is required';
    END IF;
    IF candidate_row.assessment NOT IN ('duplicate', 'conflict')
       OR jsonb_typeof(candidate_row.canonical_payload) <> 'object' THEN
        RAISE EXCEPTION 'memory candidate cannot be consolidated';
    END IF;
    SELECT record.* INTO record_row
    FROM durable_memory.candidate_record_relation AS relation
    JOIN durable_memory.record AS record ON record.id = relation.record_id
    WHERE relation.candidate_id = candidate_row.id
      AND relation.record_id = locked_record_id FOR UPDATE OF record;
    IF NOT FOUND OR record_row.namespace_id <> candidate_row.namespace_id
       OR record_row.status <> 'active' OR record_row.record_type <> candidate_row.record_type
       OR record_row.identity_key <> candidate_row.identity_key THEN
        RAISE EXCEPTION 'candidate relation must target the active matching record';
    END IF;
    SELECT id INTO existing_request_id FROM durable_memory.change_request
    WHERE consolidated_candidate_id = candidate_row.id;
    IF FOUND THEN RETURN existing_request_id; END IF;
    request_key := md5(actor_id::text || '|update|' || candidate_row.namespace_id::text
        || '|' || record_row.id::text || '|' || record_row.record_type || '|'
        || record_row.identity_key || '|' || candidate_row.canonical_payload::text
        || '|' || record_row.revision::text || '|patch|' || candidate_row.id::text);
    existing_request_id := durable_memory.submit_change_request(
        candidate_row.namespace_id, record_row.id, 'update', '', '',
        candidate_row.canonical_payload, candidate_row.canonical_search_text,
        record_row.revision, NULL, NULL, 'patch', request_key
    );
    UPDATE durable_memory.change_request
    SET consolidated_candidate_id = candidate_row.id
    WHERE id = existing_request_id;
    RETURN existing_request_id;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.request_hard_purge(
    target_namespace_id uuid, target_record_id uuid, purge_reason text
) RETURNS durable_memory.hard_purge_request LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id();
    target durable_memory.record%ROWTYPE;
    result durable_memory.hard_purge_request%ROWTYPE;
BEGIN
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(target_namespace_id, 'admin') THEN
        RAISE EXCEPTION 'namespace admin capability is required';
    END IF;
    PERFORM durable_memory.lock_memory_record(target_record_id);
    SELECT * INTO target FROM durable_memory.record
    WHERE id = target_record_id FOR UPDATE;
    IF NOT FOUND OR target.namespace_id <> target_namespace_id THEN
        RAISE EXCEPTION 'record is not in namespace';
    END IF;
    INSERT INTO durable_memory.hard_purge_request (
        namespace_id, record_id, record_type, identity_key,
        requested_by_profile_id, reason
    ) VALUES (
        target_namespace_id, target.id, target.record_type, target.identity_key,
        actor_id, purge_reason
    ) RETURNING * INTO result;
    RETURN result;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.approve_hard_purge(target_request_id uuid)
RETURNS durable_memory.hard_purge_request LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id();
    request_row durable_memory.hard_purge_request%ROWTYPE;
    target durable_memory.record%ROWTYPE; candidate_ids uuid[]; stale_request_ids uuid[];
    locked_record_id uuid;
BEGIN
    SELECT record_id INTO locked_record_id
    FROM durable_memory.hard_purge_request WHERE id = target_request_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown hard purge request'; END IF;
    PERFORM durable_memory.lock_memory_record(locked_record_id);
    SELECT * INTO request_row FROM durable_memory.hard_purge_request
    WHERE id = target_request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown hard purge request'; END IF;
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(request_row.namespace_id, 'admin') THEN
        RAISE EXCEPTION 'namespace admin capability is required';
    END IF;
    IF request_row.requested_by_profile_id = actor_id THEN
        RAISE EXCEPTION 'a different namespace administrator must approve a hard purge';
    END IF;
    IF request_row.status <> 'pending' THEN RETURN request_row; END IF;
    SELECT * INTO target FROM durable_memory.record
    WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR target.namespace_id <> request_row.namespace_id THEN
        RAISE EXCEPTION 'purge target no longer exists';
    END IF;
    INSERT INTO durable_memory.hard_purge_audit (
        request_id, namespace_id, record_id, record_type, identity_key, final_revision,
        requested_by_profile_id, approved_by_profile_id, reason
    ) VALUES (
        request_row.id, request_row.namespace_id, target.id, target.record_type,
        target.identity_key, target.revision, request_row.requested_by_profile_id,
        actor_id, request_row.reason
    );
    SELECT array_agg(request.id) INTO stale_request_ids
    FROM durable_memory.change_request AS request
    WHERE request.namespace_id = target.namespace_id
      AND request.operation = 'create'
      AND request.status = 'pending'
      AND request.record_id IS NULL
      AND request.record_type = target.record_type
      AND request.identity_key = target.identity_key;
    SELECT array_agg(DISTINCT candidate_id) INTO candidate_ids FROM (
        SELECT relation.candidate_id
        FROM durable_memory.candidate_record_relation AS relation
        WHERE relation.record_id = target.id
        UNION
        SELECT candidate.id
        FROM durable_memory.memory_candidate AS candidate
        JOIN durable_memory.change_request AS request
          ON request.id = candidate.change_request_id
        WHERE request.record_id = target.id
        UNION
        SELECT candidate.id
        FROM durable_memory.memory_candidate AS candidate
        WHERE stale_request_ids IS NOT NULL
          AND candidate.change_request_id = ANY(stale_request_ids)
        UNION
        SELECT request.consolidated_candidate_id
        FROM durable_memory.change_request AS request
        WHERE request.record_id = target.id
          AND request.consolidated_candidate_id IS NOT NULL
    ) AS purge_candidates;
    DELETE FROM durable_memory.candidate_record_relation WHERE record_id = target.id;
    IF candidate_ids IS NOT NULL THEN
        UPDATE durable_memory.memory_candidate SET change_request_id = NULL
        WHERE id = ANY(candidate_ids);
        UPDATE durable_memory.change_request SET consolidated_candidate_id = NULL
        WHERE consolidated_candidate_id = ANY(candidate_ids);
    END IF;
    DELETE FROM durable_memory.record_relation
    WHERE source_record_id = target.id OR target_record_id = target.id;
    DELETE FROM durable_memory.change_request
    WHERE record_id = target.id
       OR (stale_request_ids IS NOT NULL AND id = ANY(stale_request_ids));
    IF candidate_ids IS NOT NULL THEN
        DELETE FROM durable_memory.memory_candidate WHERE id = ANY(candidate_ids);
    END IF;
    DELETE FROM durable_memory.record_revision WHERE record_id = target.id;
    DELETE FROM durable_memory.record WHERE id = target.id;
    UPDATE durable_memory.hard_purge_request
    SET status = 'purged', approved_by_profile_id = actor_id, approved_at = now()
    WHERE id = request_row.id RETURNING * INTO request_row;
    RETURN request_row;
END $$;

REVOKE ALL ON FUNCTION durable_memory.submit_change_request(
    uuid, uuid, text, text, text, jsonb, text, integer,
    timestamptz, timestamptz, text, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.current_operation_policy() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.submit_change_request(
    uuid, uuid, text, text, text, jsonb, text, integer,
    timestamptz, timestamptz, text, text
) TO PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.current_operation_policy() TO PUBLIC;
