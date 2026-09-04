-- Candidate embeddings are an optional, rebuildable assessment projection.
-- The worker may only update candidate metadata and relations, never records.
CREATE TABLE durable_memory.candidate_embedding (
    candidate_id uuid PRIMARY KEY REFERENCES durable_memory.memory_candidate (id)
        ON DELETE CASCADE,
    model_identifier text,
    dimension integer,
    embedding vector,
    lifecycle_status text NOT NULL DEFAULT 'pending' CHECK (
        lifecycle_status IN ('pending', 'indexed', 'failed')
    ),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    indexed_at timestamptz,
    failed_at timestamptz,
    CHECK (
        (lifecycle_status = 'indexed' AND model_identifier IS NOT NULL
         AND dimension IS NOT NULL AND embedding IS NOT NULL AND error_message IS NULL)
        OR lifecycle_status <> 'indexed'
    ),
    CHECK (dimension IS NULL OR dimension > 0),
    CHECK (embedding IS NULL OR dimension = vector_dims(embedding))
);

CREATE TABLE durable_memory.candidate_embedding_job (
    candidate_id uuid PRIMARY KEY REFERENCES durable_memory.candidate_embedding (candidate_id)
        ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'completed', 'failed', 'cancelled')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    failed_at timestamptz
);

CREATE INDEX candidate_embedding_model_status ON durable_memory.candidate_embedding
    (model_identifier, lifecycle_status);
CREATE INDEX candidate_embedding_job_pending ON durable_memory.candidate_embedding_job (created_at)
    WHERE status = 'pending';

ALTER TABLE durable_memory.candidate_embedding ENABLE ROW LEVEL SECURITY;
ALTER TABLE durable_memory.candidate_embedding_job ENABLE ROW LEVEL SECURITY;

CREATE POLICY candidate_embedding_select ON durable_memory.candidate_embedding
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM durable_memory.memory_candidate AS candidate
        WHERE candidate.id = candidate_embedding.candidate_id
    ));
CREATE POLICY candidate_embedding_update ON durable_memory.candidate_embedding
    FOR UPDATE USING (EXISTS (
        SELECT 1 FROM durable_memory.memory_candidate AS candidate
        WHERE candidate.id = candidate_embedding.candidate_id
          AND candidate.submitted_by_profile_id = durable_memory.current_profile_id()
    ));
CREATE POLICY candidate_embedding_job_select ON durable_memory.candidate_embedding_job
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM durable_memory.memory_candidate AS candidate
        WHERE candidate.id = candidate_embedding_job.candidate_id
    ));
CREATE POLICY candidate_embedding_job_update ON durable_memory.candidate_embedding_job
    FOR UPDATE USING (EXISTS (
        SELECT 1 FROM durable_memory.memory_candidate AS candidate
        WHERE candidate.id = candidate_embedding_job.candidate_id
          AND candidate.submitted_by_profile_id = durable_memory.current_profile_id()
    ));

CREATE FUNCTION durable_memory.enqueue_candidate_embedding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
BEGIN
    -- Exact identity assessment is authoritative and needs no semantic retry.
    IF NEW.assessment <> 'new' THEN
        RETURN NEW;
    END IF;
    INSERT INTO durable_memory.candidate_embedding (candidate_id)
    VALUES (NEW.id) ON CONFLICT DO NOTHING;
    INSERT INTO durable_memory.candidate_embedding_job (candidate_id)
    VALUES (NEW.id) ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;

CREATE TRIGGER candidate_embedding_enqueue
AFTER INSERT ON durable_memory.memory_candidate
FOR EACH ROW EXECUTE FUNCTION durable_memory.enqueue_candidate_embedding();

CREATE FUNCTION durable_memory.candidate_semantic_assessment(
    candidate_id uuid,
    duplicate_similarity double precision DEFAULT 0.98,
    conflict_similarity double precision DEFAULT 0.80
)
RETURNS TABLE (record_id uuid, assessment text, reason text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE
    candidate_row durable_memory.memory_candidate%ROWTYPE;
    exact_row durable_memory.record%ROWTYPE;
    match_row record;
    similarity double precision;
BEGIN
    IF duplicate_similarity <= 0 OR duplicate_similarity > 1
       OR conflict_similarity <= 0 OR conflict_similarity >= duplicate_similarity THEN
        RAISE EXCEPTION 'semantic similarity thresholds must satisfy 0 < conflict < duplicate <= 1';
    END IF;
    SELECT * INTO candidate_row FROM durable_memory.memory_candidate
    WHERE id = candidate_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown memory candidate'; END IF;
    IF candidate_row.submitted_by_profile_id <> durable_memory.current_profile_id()
       OR NOT durable_memory.has_capability(candidate_row.namespace_id, 'propose') THEN
        RAISE EXCEPTION 'proposal capability is required';
    END IF;

    -- Recheck identity first for races; semantic similarity can never override it.
    SELECT * INTO exact_row FROM durable_memory.record
    WHERE namespace_id = candidate_row.namespace_id
      AND record_type = candidate_row.record_type
      AND identity_key = candidate_row.identity_key
      AND status = 'active'
    ORDER BY id LIMIT 1;
    IF FOUND THEN
        assessment := CASE WHEN exact_row.payload = candidate_row.canonical_payload
                              AND exact_row.search_text = candidate_row.canonical_search_text
                           THEN 'duplicate' ELSE 'conflict' END;
        reason := CASE WHEN assessment = 'duplicate'
                       THEN 'exact_identity_and_equal_content'
                       ELSE 'exact_identity_with_different_content' END;
        record_id := exact_row.id;
    ELSE
        SELECT record.id, 1 - (projection.embedding <=> candidate_embedding.embedding)
            AS similarity
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
          AND record.status = 'active'
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
            reason := format('semantic_cosine_similarity=%s;duplicate>=%s;conflict>=%s',
                             round(similarity::numeric, 6), duplicate_similarity,
                             conflict_similarity);
        END IF;
    END IF;
    UPDATE durable_memory.memory_candidate SET assessment = candidate_semantic_assessment.assessment
    WHERE id = candidate_row.id;
    IF record_id IS NULL THEN
        DELETE FROM durable_memory.candidate_record_relation WHERE candidate_id = candidate_row.id;
    ELSE
        INSERT INTO durable_memory.candidate_record_relation (candidate_id, record_id, reason)
        VALUES (candidate_row.id, record_id, reason)
        ON CONFLICT ON CONSTRAINT candidate_record_relation_pkey
        DO UPDATE SET record_id = EXCLUDED.record_id,
            reason = EXCLUDED.reason;
    END IF;
    RETURN NEXT;
END $$;

REVOKE ALL ON FUNCTION durable_memory.candidate_semantic_assessment(uuid, double precision, double precision)
    FROM PUBLIC;
