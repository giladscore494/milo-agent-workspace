> **ARCHIVED (historical).** This document predates Phases 1–11 and may contain stale claims (e.g. in-memory rate limiting, pre-gateway auth, earlier migration coverage). The authoritative, current documentation is [`docs/production-readiness/`](production-readiness/README.md). Where this file contradicts that set, that set wins.

# Planned target architecture

This document describes the planned architecture only. It does not implement frontend, backend, worker, Supabase, deployment, or Cloud Run files.

## Goal

Build a project-based agentic chat workspace around the preserved MILO vehicle-catalog workflow while keeping the existing MILO pipeline as the vehicle-catalog engine.

## Components

- **Next.js frontend**: a browser workspace for projects, conversations, messages, run status, and streamed run events.
- **FastAPI control API**: receives chat/workflow requests, manages project and conversation metadata, creates run records, and enqueues background work.
- **Independent Cloud Run Job worker**: executes long-running MILO/agentic runs outside the browser request lifecycle.
- **Supabase persistence**: stores projects, conversations, messages, runs, and run events.

## Supabase tables

The initial data model should include these tables:

- `projects` — project metadata and configuration. One initial seeded project represents the existing MILO vehicle-catalog workflow.
- `conversations` — conversation threads scoped to projects.
- `messages` — user/assistant/system messages scoped to conversations.
- `runs` — background execution records with status, inputs, outputs, and error metadata.
- `run_events` — append-only progress, log, tool, and state-transition events for each run.

## Execution model

The browser starts or resumes a project conversation through the Next.js UI. The FastAPI control API records the message, creates a run, and dispatches work to the independent Cloud Run Job worker. The worker writes `run_events` and final run output to Supabase so jobs continue after the browser closes or disconnects.

## MILO preservation requirement

The existing MILO pipeline remains the vehicle-catalog engine for the seeded MILO workflow. Future code must wrap or extract the current pipeline rather than silently replacing its behavior. The preserved behavior includes prompts, schemas, model settings, token budgets, chunk sizes, fallback behavior, concurrency limits, validation guards, deterministic merge logic, and partial-failure behavior.

## Non-goals for this baseline PR

This baseline does not create frontend code, backend code, worker code, Supabase schema migrations, Cloud Run configuration, deployment configuration, or secrets. Those implementation details should arrive later as small, reviewable pull requests.
