# Diffuse feature map

Keep this at the operator / user point of view. Discover internals at runtime.

**Status:** Diffuse's web application is still `planned`. There is no stable
product UI for Benny to drive through a control adapter today. Map only
surfaces that an operator can exercise; leave `control.skill_name` empty and
keep `benny-reproduce` disabled until a control adapter passes the nine-step
setup check in `control-adapter.md`.

## Operator surfaces (not yet adapter-backed)

### Repository onboarding (CLI)

Lets an operator register a GitHub repository for indexing.

#### How a user gets there

- From a machine with Compose up: `docker compose run --rm worker repository add ...`

#### How the control adapter drives it

- Not configured. Block repro until an adapter can start Compose, run the CLI path, and reset.

#### Stable selectors

- CLI command names and stdout status fields only for now.

#### States to exercise

- Success, auth failure, origin allowlist rejection, mirror already exists

#### Preconditions and setup

- Local Compose stack, GitHub token, configured origin allowlist

#### Evidence and cross-check

- CLI output plus repository list / mirror state via documented status commands

#### Gotchas

- Cloud VM git `insteadOf` rewrites break mirror remote equality checks.

### Pull request review publication (GitHub)

Lets an operator receive Diffuse findings as native GitHub review comments and checks.

#### How a user gets there

- Open or update a pull request on an onboarded repository with webhooks configured.

#### How the control adapter drives it

- Not configured. Exact UI repro would need a disposable GitHub fixture repo and webhook path, not a Diffuse web UI.

#### Stable selectors

- GitHub PR conversation, Checks tab, finding comment threads

#### States to exercise

- First review, re-trigger, addressed/resolved thread, reaction feedback

#### Preconditions and setup

- Onboarded fixture repo, worker healthy, model credentials, webhook secret

#### Evidence and cross-check

- Published review comments pinned to the exact head commit; check conclusion

#### Gotchas

- Self-webhook loop suppression; do not treat utility bots as fix owners.

### MCP / REST review tools

Lets an authorized client search code, ask grounded questions, and re-run reviews.

#### How a user gets there

- Call documented MCP tools or `/api/v1` endpoints with a repository-scoped token.

#### How the control adapter drives it

- Not configured.

#### Stable selectors

- OpenAPI operationIds / MCP tool names from docs

#### States to exercise

- Authorized success, insufficient evidence fail-closed, unauthorized

#### Preconditions and setup

- API up, valid service token, indexed repository

#### Evidence and cross-check

- Response body plus audit_events / job state

#### Gotchas

- Generation scope vs write scope authorization differs by endpoint.

## Completeness checklist

- [ ] Control adapter skill named and installed
- [ ] Nine-step adapter setup check passes
- [ ] Every reproducible user-facing feature has adapter actions and reset
- [ ] Web UI sections added when the dashboard ships
