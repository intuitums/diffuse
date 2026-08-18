ALTER TABLE api_tokens
DROP CONSTRAINT api_tokens_scopes_check;

ALTER TABLE api_tokens
ADD CONSTRAINT api_tokens_scopes_check
CHECK (
    cardinality(scopes) BETWEEN 1 AND 8
    AND scopes <@ ARRAY[
        'diffuse:mcp:read',
        'diffuse:mcp:write',
        'diffuse:mcp:generate',
        'diffuse:api:read',
        'diffuse:api:write',
        'diffuse:api:generate',
        'diffuse:admin'
    ]::TEXT[]
);
