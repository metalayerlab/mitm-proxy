# HTTP API Reference

The server listens on port 8080 by default. The live OpenAPI schema is served at `/openapi.json`, and interactive docs at `/docs`.

## Endpoints

Generated from the code: don't edit the table, run `uv run scripts/gen-wiki.py`.

<!-- generated:start http-api -->
| Method | Path | Summary |
|---|---|---|
| `GET` | `/healthz` | Liveness: the process is up |
| `GET` | `/readyz` | Readiness: able to serve requests |
| `GET` | `/version` | The running version |
<!-- generated:end http-api -->

`/healthz` (liveness) answers as long as the process runs. `/readyz` (readiness) answers 503 while the service can't serve, for example while a dependency is down. Orchestrators should restart on the first and route traffic on the second.

<!-- guidance: for every non-health endpoint, add a subsection with the request (parameters, body), the response (with an example) and the errors it can return. -->

## Authentication

<!-- guidance: how clients authenticate (token header, forward-auth at the proxy, none on a private network), and what an unauthenticated request gets. -->

## Errors

Errors are JSON: `{"detail": "<message>"}`, with a 4xx status for a client mistake and 5xx for a server-side failure.

<!-- guidance: list the error statuses this API uses, and what a client should do about each. -->
