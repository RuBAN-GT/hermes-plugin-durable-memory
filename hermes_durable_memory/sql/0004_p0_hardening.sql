-- Reserve private namespace slugs and validate ownership at the database boundary.
ALTER TABLE durable_memory.namespace
    ADD CONSTRAINT namespace_shared_slug_not_private_prefix
    CHECK (kind <> 'shared' OR slug !~ '^profile:');

CREATE FUNCTION durable_memory.validate_namespace()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
BEGIN
    IF NEW.kind = 'shared' AND NEW.slug ~ '^profile:' THEN
        RAISE EXCEPTION 'shared namespace slug uses reserved profile: prefix';
    END IF;
    IF NEW.kind = 'private' AND NEW.slug <> ('profile:' || (
        SELECT slug FROM durable_memory.profile WHERE id = NEW.owner_profile_id
    )) THEN
        RAISE EXCEPTION 'private namespace owner and slug do not match';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER namespace_private_owner_check
BEFORE INSERT OR UPDATE ON durable_memory.namespace
FOR EACH ROW EXECUTE FUNCTION durable_memory.validate_namespace();

-- Read and propose are independent capabilities.  A proposer receives only the
-- record required to construct a guarded update, not general search access.
DROP POLICY record_select ON durable_memory.record;
CREATE POLICY record_select ON durable_memory.record
    FOR SELECT USING (durable_memory.has_capability(namespace_id, 'read'));

CREATE FUNCTION durable_memory.proposal_record(target_record_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE
    record_row durable_memory.record%ROWTYPE;
BEGIN
    SELECT * INTO record_row FROM durable_memory.record WHERE id = target_record_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown record'; END IF;
    IF NOT durable_memory.has_capability(record_row.namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    RETURN to_jsonb(record_row);
END
$$;

CREATE FUNCTION durable_memory.proposal_inventory_definition(
    target_namespace_id uuid, target_record_type text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE record_row durable_memory.record%ROWTYPE;
BEGIN
    IF NOT durable_memory.has_capability(target_namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;
    SELECT * INTO record_row FROM durable_memory.record
    WHERE namespace_id = target_namespace_id
      AND record_type = '__inventory_definition__'
      AND identity_key = target_record_type
      AND status = 'active';
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN to_jsonb(record_row);
END
$$;

-- Keep the auto flag inside a function that runtime roles cannot execute. A
-- session GUC would be attacker-controlled and could bypass approval.
CREATE FUNCTION durable_memory.apply_change_request(
    request_id uuid, decision text, allow_auto boolean
)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp
AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE; record_row durable_memory.record%ROWTYPE;
    actor_id uuid := durable_memory.current_profile_id(); new_record_id uuid; next_revision integer;
BEGIN
    SELECT * INTO request_row FROM durable_memory.change_request WHERE id = request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown change request'; END IF;
    IF decision NOT IN ('approve', 'reject') THEN RAISE EXCEPTION 'invalid decision'; END IF;
    IF actor_id IS NULL OR NOT durable_memory.has_capability(request_row.namespace_id, 'approve') THEN
        IF NOT (allow_auto AND decision = 'approve' AND request_row.policy_action = 'auto'
                AND request_row.requested_by_profile_id = actor_id) THEN
            RAISE EXCEPTION 'approval capability is required';
        END IF;
    END IF;
    IF request_row.record_type = '__inventory_definition__' AND request_row.operation <> 'create' THEN
        RAISE EXCEPTION 'inventory definitions are immutable';
    END IF;
    IF request_row.status <> 'pending' THEN RETURN; END IF;
    IF request_row.expires_at <= now() THEN
        UPDATE durable_memory.change_request SET status = 'expired', decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    IF decision = 'reject' THEN
        UPDATE durable_memory.change_request SET status = 'rejected', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
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
    IF NOT FOUND OR record_row.status <> 'active' OR (request_row.expected_revision IS NOT NULL AND record_row.revision <> request_row.expected_revision) THEN
        UPDATE durable_memory.change_request SET status = 'superseded', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id; RETURN;
    END IF;
    IF record_row.record_type = '__inventory_definition__' THEN RAISE EXCEPTION 'inventory definitions are immutable'; END IF;
    next_revision := record_row.revision + 1;
    IF request_row.operation = 'update' THEN
        UPDATE durable_memory.record SET revision = next_revision, search_text = request_row.search_text, payload = request_row.payload, updated_by_profile_id = actor_id WHERE id = record_row.id;
    ELSE
        UPDATE durable_memory.record SET status = 'tombstoned', revision = next_revision, updated_by_profile_id = actor_id WHERE id = record_row.id;
    END IF;
    INSERT INTO durable_memory.record_revision (record_id, revision, operation, payload, actor_profile_id)
    VALUES (record_row.id, next_revision, request_row.operation, CASE WHEN request_row.operation = 'update' THEN request_row.payload ELSE record_row.payload END, actor_id);
    UPDATE durable_memory.change_request SET status = 'approved', decided_by_profile_id = actor_id, decided_at = now() WHERE id = request_row.id;
END $$;

CREATE OR REPLACE FUNCTION durable_memory.decide_change_request(request_id uuid, decision text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
    SELECT durable_memory.apply_change_request(request_id, decision, false)
$$;

CREATE FUNCTION durable_memory.auto_apply_change_request(request_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE request_row durable_memory.change_request%ROWTYPE;
BEGIN
    SELECT * INTO request_row FROM durable_memory.change_request WHERE id = request_id;
    IF NOT FOUND OR request_row.policy_action <> 'auto' OR request_row.requested_by_profile_id <> durable_memory.current_profile_id() THEN
        RAISE EXCEPTION 'only the auto request initiator may auto-apply';
    END IF;
    PERFORM durable_memory.apply_change_request(request_id, 'approve', true);
END $$;

REVOKE ALL ON FUNCTION durable_memory.apply_change_request(uuid, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.proposal_record(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.proposal_inventory_definition(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.auto_apply_change_request(uuid) FROM PUBLIC;
