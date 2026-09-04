-- Typed metadata and references remain projections of generic records; no
-- domain-specific record tables are introduced.
ALTER TABLE durable_memory.memory_type
    ADD COLUMN archetype text NOT NULL DEFAULT 'entity'
        CHECK (archetype IN ('entity', 'event', 'observation', 'relation',
                            'recommendation', 'collection_entry')),
    ADD COLUMN sensitivity text NOT NULL DEFAULT 'normal'
        CHECK (sensitivity IN ('normal', 'financial', 'health')),
    ADD COLUMN mutable boolean NOT NULL DEFAULT true;

CREATE TABLE durable_memory.record_external_identity (
    record_id uuid NOT NULL REFERENCES durable_memory.record (id) ON DELETE CASCADE,
    scheme text NOT NULL CHECK (scheme <> ''),
    value text NOT NULL CHECK (value <> ''),
    PRIMARY KEY (record_id, scheme),
    UNIQUE (scheme, value)
);

CREATE TABLE durable_memory.record_relation (
    source_record_id uuid NOT NULL REFERENCES durable_memory.record (id) ON DELETE CASCADE,
    relation_type text NOT NULL CHECK (relation_type <> ''),
    target_record_id uuid NOT NULL REFERENCES durable_memory.record (id) ON DELETE RESTRICT,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_record_id, relation_type, target_record_id),
    CHECK (source_record_id <> target_record_id)
);
CREATE INDEX record_relation_target ON durable_memory.record_relation (target_record_id);

ALTER TABLE durable_memory.record_external_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.record_relation ENABLE ROW LEVEL SECURITY;
CREATE POLICY record_external_identity_select ON durable_memory.record_external_identity
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM durable_memory.record AS record
        WHERE record.id = record_external_identity.record_id
          AND durable_memory.has_capability(record.namespace_id, 'read')
    ));
CREATE POLICY record_relation_select ON durable_memory.record_relation
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM durable_memory.record AS record
        WHERE record.id = record_relation.source_record_id
          AND durable_memory.has_capability(record.namespace_id, 'read')
    ));

CREATE OR REPLACE VIEW durable_memory.inventory_definition WITH (security_invoker = true) AS
SELECT type.namespace_id, type.record_type, version.version, version.fields,
       version.schema, type.lifecycle_status, type.semantic_assessment_required,
       type.archetype, type.sensitivity, type.mutable
FROM durable_memory.memory_type AS type
JOIN durable_memory.memory_schema_version AS version ON version.memory_type_id = type.id
WHERE type.lifecycle_status = 'active' AND version.lifecycle_status = 'active';

REVOKE ALL ON durable_memory.record_external_identity, durable_memory.record_relation FROM PUBLIC;
