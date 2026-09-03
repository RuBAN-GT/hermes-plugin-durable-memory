DROP POLICY record_insert ON durable_memory.record;
DROP POLICY record_update ON durable_memory.record;
DROP POLICY record_revision_insert ON durable_memory.record_revision;
DROP POLICY change_request_update ON durable_memory.change_request;

DROP POLICY change_request_insert ON durable_memory.change_request;
CREATE POLICY change_request_insert ON durable_memory.change_request
    FOR INSERT
    WITH CHECK (
        requested_by_profile_id = durable_memory.current_profile_id()
        AND status = 'pending'
        AND decided_by_profile_id IS NULL
        AND decided_at IS NULL
        AND (
            (operation = 'create' AND record_id IS NULL)
            OR (operation IN ('update', 'delete') AND record_id IS NOT NULL)
        )
        AND durable_memory.has_capability(namespace_id, 'propose')
    );

CREATE FUNCTION durable_memory.decide_change_request(
    request_id uuid,
    decision text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE
    request_row durable_memory.change_request%ROWTYPE;
    record_row durable_memory.record%ROWTYPE;
    actor_id uuid := durable_memory.current_profile_id();
    new_record_id uuid;
    next_revision integer;
BEGIN
    SELECT * INTO request_row
    FROM durable_memory.change_request
    WHERE id = request_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown change request';
    END IF;
    IF decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'invalid decision';
    END IF;
    IF actor_id IS NULL
       OR NOT durable_memory.has_capability(request_row.namespace_id, 'approve') THEN
        RAISE EXCEPTION 'approval capability is required';
    END IF;
    IF request_row.status <> 'pending' THEN
        RETURN;
    END IF;
    IF request_row.expires_at <= now() THEN
        UPDATE durable_memory.change_request
        SET status = 'expired', decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;
    IF decision = 'reject' THEN
        UPDATE durable_memory.change_request
        SET status = 'rejected', decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;

    IF request_row.operation = 'create' THEN
        new_record_id := request_row.id;
        INSERT INTO durable_memory.record (
            id, namespace_id, record_type, identity_key, status, revision,
            search_text, payload, created_by_profile_id, updated_by_profile_id
        ) VALUES (
            new_record_id, request_row.namespace_id, request_row.record_type,
            request_row.identity_key, 'active', 1, request_row.search_text,
            request_row.payload, request_row.requested_by_profile_id, actor_id
        ) ON CONFLICT (namespace_id, record_type, identity_key) WHERE status = 'active'
        DO NOTHING;

        IF NOT FOUND THEN
            UPDATE durable_memory.change_request
            SET status = 'superseded', decided_by_profile_id = actor_id,
                decided_at = now()
            WHERE id = request_row.id;
            RETURN;
        END IF;

        INSERT INTO durable_memory.record_revision (
            record_id, revision, operation, payload, actor_profile_id
        ) VALUES (
            new_record_id, 1, 'create', request_row.payload, actor_id
        );
        UPDATE durable_memory.change_request
        SET record_id = new_record_id, status = 'approved',
            decided_by_profile_id = actor_id, decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;

    SELECT * INTO record_row
    FROM durable_memory.record
    WHERE id = request_row.record_id
    FOR UPDATE;
    IF NOT FOUND
       OR record_row.status <> 'active'
       OR (request_row.expected_revision IS NOT NULL
           AND record_row.revision <> request_row.expected_revision) THEN
        UPDATE durable_memory.change_request
        SET status = 'superseded', decided_by_profile_id = actor_id,
            decided_at = now()
        WHERE id = request_row.id;
        RETURN;
    END IF;

    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN
        UPDATE durable_memory.record
        SET revision = next_revision, search_text = request_row.search_text,
            payload = request_row.payload, updated_by_profile_id = actor_id
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
            THEN request_row.payload ELSE record_row.payload END,
        actor_id
    );
    UPDATE durable_memory.change_request
    SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now()
    WHERE id = request_row.id;
END
$$;

REVOKE ALL ON FUNCTION durable_memory.decide_change_request(uuid, text) FROM PUBLIC;
