GRANT USAGE ON SCHEMA durable_memory TO {runtime_role};
GRANT SELECT ON durable_memory.profile TO {runtime_role};
GRANT SELECT, INSERT, UPDATE ON durable_memory.namespace TO {runtime_role};
GRANT SELECT, INSERT, DELETE ON durable_memory.namespace_grant TO {runtime_role};
GRANT SELECT ON durable_memory.memory_type, durable_memory.memory_schema_version,
  durable_memory.inventory_definition TO {runtime_role};
GRANT SELECT, INSERT ON durable_memory.memory_candidate TO {runtime_role};
GRANT SELECT, INSERT ON durable_memory.memory_evidence TO {runtime_role};
GRANT SELECT, INSERT ON durable_memory.candidate_record_relation TO {runtime_role};
GRANT SELECT ON durable_memory.record, durable_memory.record_revision,
  durable_memory.change_request TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.submit_change_request(
  uuid, uuid, text, text, text, jsonb, text, integer, timestamptz, timestamptz,
  text, text)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.decide_change_request(uuid, text)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.proposal_record(uuid)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.current_operation_policy()
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.save_import_checkpoint(
  text, text, text, jsonb) TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.load_import_checkpoint(text, text)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.proposal_inventory_definition(uuid, text)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.candidate_identity_assessment(
  uuid, text, text, jsonb, text) TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.consolidate_candidate(
  uuid, uuid, text, integer) TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.candidate_semantic_assessment(
  uuid, double precision, double precision) TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.set_namespace_retention(uuid, integer)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.request_hard_purge(uuid, uuid, text)
  TO {runtime_role};
GRANT EXECUTE ON FUNCTION durable_memory.approve_hard_purge(uuid)
  TO {runtime_role};
