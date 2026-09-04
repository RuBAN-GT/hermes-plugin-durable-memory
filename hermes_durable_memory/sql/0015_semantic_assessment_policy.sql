ALTER TABLE durable_memory.memory_type
    ADD COLUMN semantic_assessment_required boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN durable_memory.memory_type.semantic_assessment_required IS
    'When true, candidates must complete semantic assessment before a create request is published.';

CREATE OR REPLACE VIEW durable_memory.inventory_definition WITH (security_invoker = true) AS
SELECT type.namespace_id, type.record_type, version.version, version.fields,
       version.schema, type.lifecycle_status, type.semantic_assessment_required
FROM durable_memory.memory_type AS type
JOIN durable_memory.memory_schema_version AS version ON version.memory_type_id = type.id
WHERE type.lifecycle_status = 'active' AND version.lifecycle_status = 'active';
