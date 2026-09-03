-- Deployment grants the minimum table privileges to each profile runtime role.
-- RLS below enforces that role's bound profile and namespace capabilities.

CREATE POLICY record_insert ON durable_memory.record
    FOR INSERT WITH CHECK (durable_memory.has_capability(namespace_id, 'approve'));
CREATE POLICY record_update ON durable_memory.record
    FOR UPDATE USING (durable_memory.has_capability(namespace_id, 'approve'))
    WITH CHECK (durable_memory.has_capability(namespace_id, 'approve'));
CREATE POLICY record_revision_insert ON durable_memory.record_revision
    FOR INSERT WITH CHECK (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE id = record_id
                  AND durable_memory.has_capability(namespace_id, 'approve'))
    );
CREATE POLICY change_request_update ON durable_memory.change_request
    FOR UPDATE USING (
        durable_memory.has_capability(namespace_id, 'approve')
    )
    WITH CHECK (
        durable_memory.has_capability(namespace_id, 'approve')
    );

CREATE INDEX record_search_fts ON durable_memory.record
    USING gin (to_tsvector('simple', search_text || ' ' || identity_key));
CREATE INDEX record_namespace_status ON durable_memory.record (namespace_id, status);
CREATE OR REPLACE FUNCTION durable_memory.touch_record_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END
$$;
CREATE TRIGGER record_updated_at BEFORE UPDATE ON durable_memory.record
    FOR EACH ROW EXECUTE FUNCTION durable_memory.touch_record_updated_at();
