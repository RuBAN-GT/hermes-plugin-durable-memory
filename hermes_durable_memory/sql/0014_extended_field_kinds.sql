CREATE OR REPLACE FUNCTION durable_memory.validate_submission_payload(
    target_namespace_id uuid, target_record_type text, candidate_payload jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = durable_memory, pg_temp AS $$
DECLARE fields jsonb; field_name text; field_spec jsonb; value jsonb;
BEGIN
    IF jsonb_typeof(candidate_payload) <> 'object' THEN
        RAISE EXCEPTION 'record payload must be an object';
    END IF;
    SELECT definition.fields INTO fields FROM durable_memory.inventory_definition AS definition
    WHERE definition.namespace_id = target_namespace_id AND definition.record_type = target_record_type;
    IF fields IS NULL THEN RETURN; END IF;
    FOR field_name, value IN SELECT * FROM jsonb_each(candidate_payload) LOOP
        IF field_name NOT IN ('identity', 'text') AND NOT fields ? field_name THEN
            RAISE EXCEPTION 'unknown inventory field: %', field_name;
        END IF;
    END LOOP;
    FOR field_name, field_spec IN SELECT * FROM jsonb_each(fields) LOOP
        value := candidate_payload -> field_name;
        IF COALESCE((field_spec ->> 'required')::boolean, false) AND value IS NULL THEN
            RAISE EXCEPTION 'required inventory field missing: %', field_name;
        END IF;
        IF value IS NOT NULL AND NOT (
            (field_spec ->> 'kind' IN ('string', 'text', 'reference') AND jsonb_typeof(value) = 'string') OR
            (field_spec ->> 'kind' = 'enum' AND jsonb_typeof(value) = 'string'
                AND jsonb_typeof(field_spec -> 'values') = 'array'
                AND field_spec -> 'values' @> jsonb_build_array(value)) OR
            (field_spec ->> 'kind' = 'integer' AND jsonb_typeof(value) = 'number' AND (value #>> '{}') ~ '^-?[0-9]+$') OR
            (field_spec ->> 'kind' = 'number' AND jsonb_typeof(value) = 'number') OR
            (field_spec ->> 'kind' = 'boolean' AND jsonb_typeof(value) = 'boolean') OR
            (field_spec ->> 'kind' = 'object' AND jsonb_typeof(value) = 'object') OR
            (field_spec ->> 'kind' = 'array' AND jsonb_typeof(value) = 'array') OR
            (field_spec ->> 'kind' = 'decimal' AND jsonb_typeof(value) = 'string' AND (value #>> '{}') ~ '^-?[0-9]+(\.[0-9]+)?$') OR
            (field_spec ->> 'kind' = 'date' AND jsonb_typeof(value) = 'string' AND (value #>> '{}') ~ '^\d{4}-\d{2}-\d{2}$') OR
            (field_spec ->> 'kind' = 'datetime' AND jsonb_typeof(value) = 'string' AND (value #>> '{}') ~ 'T.*(Z|[+-]\d{2}:\d{2})$') OR
            (field_spec ->> 'kind' = 'money' AND jsonb_typeof(value) = 'object'
                AND jsonb_typeof(value -> 'amount_minor') = 'number'
                AND (value ->> 'amount_minor') ~ '^-?[0-9]+$'
                AND value ->> 'currency' ~ '^[A-Z]{3}$') OR
            (field_spec ->> 'kind' = 'measurement' AND jsonb_typeof(value) = 'object'
                AND jsonb_typeof(value -> 'value') = 'string'
                AND (value ->> 'value') ~ '^-?[0-9]+(\.[0-9]+)?$'
                AND jsonb_typeof(value -> 'unit') = 'string'
                AND value ->> 'unit' <> '')
        ) THEN
            RAISE EXCEPTION 'invalid inventory field type: %', field_name;
        END IF;
    END LOOP;
END $$;
