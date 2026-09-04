-- Embeddings are a rebuildable search projection. Canonical record payloads
-- remain exclusively in durable_memory.record and record_revision.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE durable_memory.record_embedding (
    record_id uuid NOT NULL REFERENCES durable_memory.record (id) ON DELETE CASCADE,
    revision integer NOT NULL,
    model_identifier text,
    content_hash char(64) NOT NULL,
    dimension integer,
    embedding vector,
    lifecycle_status text NOT NULL DEFAULT 'pending' CHECK (
        lifecycle_status IN ('pending', 'indexed', 'failed', 'deleted')
    ),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    indexed_at timestamptz,
    failed_at timestamptz,
    PRIMARY KEY (record_id, revision),
    CHECK (
        (lifecycle_status = 'indexed' AND model_identifier IS NOT NULL
         AND dimension IS NOT NULL AND embedding IS NOT NULL AND error_message IS NULL)
        OR lifecycle_status <> 'indexed'
    ),
    CHECK (dimension IS NULL OR dimension > 0),
    CHECK (embedding IS NULL OR dimension = vector_dims(embedding))
);

CREATE TABLE durable_memory.embedding_job (
    record_id uuid NOT NULL,
    revision integer NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'completed', 'failed', 'cancelled')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    failed_at timestamptz,
    PRIMARY KEY (record_id, revision),
    FOREIGN KEY (record_id, revision)
        REFERENCES durable_memory.record_embedding (record_id, revision)
        ON DELETE CASCADE
);

-- A dimensionless vector column permits model changes. pgvector cannot build a
-- single HNSW index over mixed dimensions, so deployments add a partial,
-- model-and-dimension-specific HNSW index after selecting an embedding model.
CREATE INDEX record_embedding_model_status ON durable_memory.record_embedding
    (model_identifier, lifecycle_status);
CREATE INDEX embedding_job_pending ON durable_memory.embedding_job (created_at)
    WHERE status = 'pending';

ALTER TABLE durable_memory.record_embedding ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.embedding_job ENABLE ROW LEVEL SECURITY;

CREATE POLICY record_embedding_select ON durable_memory.record_embedding
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = record_embedding.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'read'))
    );
CREATE POLICY record_embedding_update ON durable_memory.record_embedding
    FOR UPDATE USING (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = record_embedding.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'approve'))
    ) WITH CHECK (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = record_embedding.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'approve'))
    );
CREATE POLICY embedding_job_select ON durable_memory.embedding_job
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = embedding_job.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'read'))
    );
CREATE POLICY embedding_job_update ON durable_memory.embedding_job
    FOR UPDATE USING (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = embedding_job.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'approve'))
    ) WITH CHECK (
        EXISTS (SELECT 1 FROM durable_memory.record
                WHERE record.id = embedding_job.record_id
                  AND durable_memory.has_capability(record.namespace_id, 'approve'))
    );

CREATE FUNCTION durable_memory.enqueue_record_embedding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE content_digest char(64);
BEGIN
    IF NEW.record_type = '__inventory_definition__' THEN
        RETURN NEW;
    END IF;
    IF NEW.status <> 'active' THEN
        UPDATE durable_memory.record_embedding
        SET lifecycle_status = 'deleted', embedding = NULL, dimension = NULL,
            error_message = NULL
        WHERE record_id = NEW.id;
        UPDATE durable_memory.embedding_job SET status = 'cancelled'
        WHERE record_id = NEW.id AND status = 'pending';
        RETURN NEW;
    END IF;
    content_digest := encode(digest(NEW.search_text, 'sha256'), 'hex');
    INSERT INTO durable_memory.record_embedding (
        record_id, revision, content_hash, lifecycle_status
    ) VALUES (NEW.id, NEW.revision, content_digest, 'pending')
    ON CONFLICT (record_id, revision) DO NOTHING;
    INSERT INTO durable_memory.embedding_job (record_id, revision)
    VALUES (NEW.id, NEW.revision) ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;

CREATE TRIGGER record_embedding_enqueue
AFTER INSERT OR UPDATE OF revision, status, search_text ON durable_memory.record
FOR EACH ROW EXECUTE FUNCTION durable_memory.enqueue_record_embedding();

-- Seed jobs for records created before this migration so the projection can be rebuilt.
INSERT INTO durable_memory.record_embedding (
    record_id, revision, content_hash, lifecycle_status
)
SELECT id, revision, encode(digest(search_text, 'sha256'), 'hex'), 'pending'
FROM durable_memory.record
WHERE status = 'active' AND record_type <> '__inventory_definition__'
ON CONFLICT DO NOTHING;
INSERT INTO durable_memory.embedding_job (record_id, revision)
SELECT record_id, revision FROM durable_memory.record_embedding
WHERE lifecycle_status = 'pending'
ON CONFLICT DO NOTHING;
