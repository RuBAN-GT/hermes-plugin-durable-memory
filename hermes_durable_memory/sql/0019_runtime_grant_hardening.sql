-- Runtime roles may submit or decide requests, but never mutate canonical data
-- or invoke the internal auto-apply path directly.
DO $$
DECLARE runtime_role name;
BEGIN
    FOR runtime_role IN SELECT profile.runtime_role FROM durable_memory.profile AS profile LOOP
        EXECUTE format(
            'REVOKE INSERT, UPDATE, DELETE ON TABLE durable_memory.record, durable_memory.record_revision, durable_memory.change_request, durable_memory.memory_type, durable_memory.memory_schema_version FROM %I',
            runtime_role
        );
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION durable_memory.apply_change_request(uuid, text, boolean), durable_memory.auto_apply_change_request(uuid) FROM %I',
            runtime_role
        );
    END LOOP;
END $$;

REVOKE INSERT, UPDATE, DELETE ON TABLE durable_memory.record, durable_memory.record_revision,
    durable_memory.change_request, durable_memory.memory_type,
    durable_memory.memory_schema_version FROM PUBLIC;
REVOKE ALL ON FUNCTION durable_memory.apply_change_request(uuid, text, boolean),
    durable_memory.auto_apply_change_request(uuid) FROM PUBLIC;

-- The two public entry points are authority-constrained internally.
GRANT EXECUTE ON FUNCTION durable_memory.submit_change_request(
    uuid, uuid, text, text, text, jsonb, text, integer, timestamptz, timestamptz, text
) TO PUBLIC;
GRANT EXECUTE ON FUNCTION durable_memory.decide_change_request(uuid, text) TO PUBLIC;
