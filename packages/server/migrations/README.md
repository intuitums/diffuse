# Diffuse SQL migrations

`packages/server/migrations/schema.sql` is the immutable `0001_initial_schema` baseline. Do not edit it
after release.

Add future transactional migrations here as consecutive files named
`0002_short_name.sql`, `0003_short_name.sql`, and so on. Migration files are
checksum-verified after application and must not contain psql backslash
commands.
