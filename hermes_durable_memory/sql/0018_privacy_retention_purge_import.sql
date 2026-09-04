-- Privacy controls are explicitly separate from normal approval-gated deletes.
CREATE TABLE durable_memory.namespace_retention_policy (
    namespace_id uuid PRIMARY KEY REFERENCES durable_memory.namespace (id) ON DELETE CASCADE,
    retention_seconds integer CHECK (retention_seconds BETWEEN 1 AND 315576000),
    updated_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE durable_memory.hard_purge_request (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id uuid NOT NULL REFERENCES durable_memory.namespace (id),
    record_id uuid NOT NULL,
    record_type text NOT NULL,
    identity_key text NOT NULL,
    requested_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    status text NOT NULL CHECK (status IN ('pending', 'purged', 'rejected')) DEFAULT 'pending',
    requested_at timestamptz NOT NULL DEFAULT now(),
    approved_by_profile_id uuid REFERENCES durable_memory.profile (id),
    approved_at timestamptz
);
CREATE TABLE durable_memory.hard_purge_audit (
    request_id uuid PRIMARY KEY REFERENCES durable_memory.hard_purge_request (id),
    namespace_id uuid NOT NULL,
    record_id uuid NOT NULL,
    record_type text NOT NULL,
    identity_key text NOT NULL,
    final_revision integer NOT NULL,
    requested_by_profile_id uuid NOT NULL,
    approved_by_profile_id uuid NOT NULL,
    reason text NOT NULL,
    purged_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE durable_memory.import_checkpoint (
    source_name text NOT NULL,
    scope text NOT NULL,
    checkpoint text,
    report jsonb NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    updated_by_profile_id uuid NOT NULL REFERENCES durable_memory.profile (id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_name, scope)
);
ALTER TABLE durable_memory.namespace_retention_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.hard_purge_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.hard_purge_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.import_checkpoint ENABLE ROW LEVEL SECURITY;
CREATE POLICY retention_policy_select ON durable_memory.namespace_retention_policy FOR SELECT
    USING (durable_memory.has_capability(namespace_id, 'read'));
CREATE POLICY purge_request_select ON durable_memory.hard_purge_request FOR SELECT
    USING (durable_memory.has_capability(namespace_id, 'admin'));
CREATE POLICY purge_audit_select ON durable_memory.hard_purge_audit FOR SELECT
    USING (durable_memory.has_capability(namespace_id, 'admin'));
CREATE POLICY import_checkpoint_owner ON durable_memory.import_checkpoint FOR SELECT
    USING (updated_by_profile_id = durable_memory.current_profile_id());

CREATE OR REPLACE FUNCTION durable_memory.set_namespace_retention(target_namespace_id uuid, seconds integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id();
BEGIN
    IF actor_id IS NULL OR NOT durable_memory.has_capability(target_namespace_id, 'admin') THEN RAISE EXCEPTION 'namespace admin capability is required'; END IF;
    IF seconds IS NOT NULL AND (seconds < 1 OR seconds > 315576000) THEN RAISE EXCEPTION 'retention must be between 1 second and 10 years'; END IF;
    INSERT INTO durable_memory.namespace_retention_policy (namespace_id, retention_seconds, updated_by_profile_id)
    VALUES (target_namespace_id, seconds, actor_id)
    ON CONFLICT (namespace_id) DO UPDATE SET retention_seconds = EXCLUDED.retention_seconds,
        updated_by_profile_id = EXCLUDED.updated_by_profile_id, updated_at = now();
END $$;
CREATE OR REPLACE FUNCTION durable_memory.apply_namespace_retention()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE seconds integer;
BEGIN
    IF NEW.valid_to IS NULL THEN
        SELECT retention_seconds INTO seconds FROM durable_memory.namespace_retention_policy WHERE namespace_id = NEW.namespace_id;
        IF seconds IS NOT NULL THEN NEW.valid_to := NEW.valid_from + make_interval(secs => seconds); END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER record_namespace_retention BEFORE INSERT ON durable_memory.record
    FOR EACH ROW EXECUTE FUNCTION durable_memory.apply_namespace_retention();
CREATE OR REPLACE FUNCTION durable_memory.namespace_retention(target_namespace_id uuid)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
BEGIN
    IF NOT durable_memory.has_capability(target_namespace_id, 'read') THEN RAISE EXCEPTION 'read capability is required'; END IF;
    RETURN (SELECT retention_seconds FROM durable_memory.namespace_retention_policy WHERE namespace_id = target_namespace_id);
END $$;

CREATE OR REPLACE FUNCTION durable_memory.request_hard_purge(target_namespace_id uuid, target_record_id uuid, purge_reason text)
RETURNS durable_memory.hard_purge_request LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id(); target durable_memory.record%ROWTYPE; result durable_memory.hard_purge_request%ROWTYPE;
BEGIN
    IF actor_id IS NULL OR NOT durable_memory.has_capability(target_namespace_id, 'admin') THEN RAISE EXCEPTION 'namespace admin capability is required'; END IF;
    SELECT * INTO target FROM durable_memory.record WHERE id = target_record_id FOR UPDATE;
    IF NOT FOUND OR target.namespace_id <> target_namespace_id THEN RAISE EXCEPTION 'record is not in namespace'; END IF;
    INSERT INTO durable_memory.hard_purge_request (namespace_id, record_id, record_type, identity_key, requested_by_profile_id, reason)
    VALUES (target_namespace_id, target.id, target.record_type, target.identity_key, actor_id, purge_reason)
    RETURNING * INTO result;
    RETURN result;
END $$;
CREATE OR REPLACE FUNCTION durable_memory.approve_hard_purge(target_request_id uuid)
RETURNS durable_memory.hard_purge_request LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id(); request_row durable_memory.hard_purge_request%ROWTYPE; target durable_memory.record%ROWTYPE;
BEGIN
    SELECT * INTO request_row FROM durable_memory.hard_purge_request WHERE id = target_request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown hard purge request'; END IF;
    IF actor_id IS NULL OR NOT durable_memory.has_capability(request_row.namespace_id, 'admin') THEN RAISE EXCEPTION 'namespace admin capability is required'; END IF;
    IF request_row.requested_by_profile_id = actor_id THEN RAISE EXCEPTION 'a different namespace administrator must approve a hard purge'; END IF;
    IF request_row.status <> 'pending' THEN RETURN request_row; END IF;
    SELECT * INTO target FROM durable_memory.record WHERE id = request_row.record_id FOR UPDATE;
    IF NOT FOUND OR target.namespace_id <> request_row.namespace_id THEN RAISE EXCEPTION 'purge target no longer exists'; END IF;
    INSERT INTO durable_memory.hard_purge_audit (request_id, namespace_id, record_id, record_type, identity_key, final_revision, requested_by_profile_id, approved_by_profile_id, reason)
    VALUES (request_row.id, request_row.namespace_id, target.id, target.record_type, target.identity_key, target.revision, request_row.requested_by_profile_id, actor_id, request_row.reason);
    DELETE FROM durable_memory.candidate_record_relation WHERE record_id = target.id;
    DELETE FROM durable_memory.record_relation WHERE source_record_id = target.id OR target_record_id = target.id;
    DELETE FROM durable_memory.change_request WHERE record_id = target.id;
    DELETE FROM durable_memory.record_revision WHERE record_id = target.id;
    DELETE FROM durable_memory.record WHERE id = target.id;
    UPDATE durable_memory.hard_purge_request SET status = 'purged', approved_by_profile_id = actor_id, approved_at = now() WHERE id = request_row.id RETURNING * INTO request_row;
    RETURN request_row;
END $$;
CREATE OR REPLACE FUNCTION durable_memory.save_import_checkpoint(source text, target_scope text, next_checkpoint text, next_report jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
DECLARE actor_id uuid := durable_memory.current_profile_id();
BEGIN
    IF actor_id IS NULL OR source = '' OR target_scope = '' THEN RAISE EXCEPTION 'valid importer identity is required'; END IF;
    INSERT INTO durable_memory.import_checkpoint (source_name, scope, checkpoint, report, updated_by_profile_id)
    VALUES (source, target_scope, next_checkpoint, next_report, actor_id)
    ON CONFLICT (source_name, scope) DO UPDATE SET checkpoint = EXCLUDED.checkpoint, report = EXCLUDED.report, updated_by_profile_id = EXCLUDED.updated_by_profile_id, updated_at = now();
END $$;
CREATE OR REPLACE FUNCTION durable_memory.load_import_checkpoint(source text, target_scope text)
RETURNS TABLE (checkpoint text, report jsonb) LANGUAGE plpgsql SECURITY DEFINER SET search_path = durable_memory, pg_temp AS $$
BEGIN
    RETURN QUERY SELECT item.checkpoint, item.report FROM durable_memory.import_checkpoint AS item
    WHERE item.source_name = source AND item.scope = target_scope
      AND item.updated_by_profile_id = durable_memory.current_profile_id();
END $$;
REVOKE ALL ON TABLE durable_memory.namespace_retention_policy, durable_memory.hard_purge_request, durable_memory.hard_purge_audit, durable_memory.import_checkpoint FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.set_namespace_retention(uuid, integer), durable_memory.namespace_retention(uuid), durable_memory.request_hard_purge(uuid, uuid, text), durable_memory.approve_hard_purge(uuid), durable_memory.save_import_checkpoint(text, text, text, jsonb), durable_memory.load_import_checkpoint(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.set_namespace_retention(uuid, integer), durable_memory.namespace_retention(uuid), durable_memory.request_hard_purge(uuid, uuid, text), durable_memory.approve_hard_purge(uuid), durable_memory.save_import_checkpoint(text, text, text, jsonb), durable_memory.load_import_checkpoint(text, text) TO PUBLIC;
