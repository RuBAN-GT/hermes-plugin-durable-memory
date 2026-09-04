-- Projection jobs are rebuildable, but a crashed worker must not strand one.
ALTER TABLE durable_memory.embedding_job
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
    ADD COLUMN lease_expires_at timestamptz;

ALTER TABLE durable_memory.candidate_embedding_job
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
    ADD COLUMN lease_expires_at timestamptz;

CREATE INDEX embedding_job_lease_expiry ON durable_memory.embedding_job (lease_expires_at)
    WHERE status = 'processing';
CREATE INDEX candidate_embedding_job_lease_expiry
    ON durable_memory.candidate_embedding_job (lease_expires_at)
    WHERE status = 'processing';

-- Recover abandoned claims without erasing attempt history. Exhausted jobs are
-- terminal failures and require an explicit operator requeue after remediation.
UPDATE durable_memory.embedding_job
SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
    failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE failed_at END,
    last_error = CASE WHEN attempts >= max_attempts THEN 'embedding lease expired' ELSE last_error END,
    claim_token = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL
WHERE status = 'processing' AND claimed_at < now() - interval '15 minutes';

UPDATE durable_memory.candidate_embedding_job
SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
    failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE failed_at END,
    last_error = CASE WHEN attempts >= max_attempts THEN 'candidate embedding lease expired' ELSE last_error END,
    claim_token = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL
WHERE status = 'processing' AND claimed_at < now() - interval '15 minutes';
