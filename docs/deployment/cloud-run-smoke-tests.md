> **ARCHIVED (historical).** This document predates Phases 1–11 and may contain stale claims (e.g. in-memory rate limiting, pre-gateway auth, earlier migration coverage). The authoritative, current documentation is [`docs/production-readiness/`](../production-readiness/README.md). Where this file contradicts that set, that set wins.

# Private Cloud Run smoke tests

These smoke tests are manual and read-only. They assume the Cloud Run API remains private and that the operator has an identity authorized to invoke the service.

Set the private service URL without a trailing slash:

```bash
API_URL="https://milo-agent-api-REGION-PROJECT.run.app"
```

Fetch an identity token for the private service audience:

```bash
TOKEN="$(gcloud auth print-identity-token --audiences="$API_URL")"
```

## Free read-only smoke tests

Health check:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" "$API_URL/health"
```

Project listing:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" "$API_URL/projects"
```

Do not include `POST /runs` in free smoke tests. That endpoint immediately invokes the worker and may make paid Kimi/Moonshot calls.

## Optional write-only conversation check

Conversation creation can be tested separately because it writes to Supabase but does not execute the worker. Run it only when an operator explicitly accepts a production database write.
