# Durable Memory

Use the installed Hermes interface for durable memory. Prefer the
`hermes durable-memory` CLI (or the installed Hermes durable-memory tool) for
`search`, `namespaces`, `list-inventories`, and `pending`.

All `propose` operations are mutations. Respect the configured approval policy:
when the result is `pending`, do not claim that the record was saved. Ask for
approval or use `hermes durable-memory approve --request-id <id>` only when the
user has explicitly authorized that approval. Never bypass approval with direct
database writes, and never print database URLs or credentials.

The active profile comes from the Hermes process environment. Do not infer a
profile, read `.env` files, or access another profile's private namespace.
