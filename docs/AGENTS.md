# Working in this repo as a coding agent

This file tells AI coding agents (Claude Code, Cursor, Windsurf, Copilot, Aider,
etc.) how to work here. It is tool-agnostic — `.claude/CLAUDE.md` just points here.

## Before you start

1. Read [`spec/assignment.md`](../spec/assignment.md) — the assignment brief
   this service implements — and [`spec/yad2_search_taxonomy.json`](../spec/yad2_search_taxonomy.json),
   the taxonomy that bounds every field this service is allowed to extract.
2. Read the relevant [`conventions/`](conventions/) files for what you're changing.
3. Read [`services/search-api.md`](services/search-api.md) for the service reference.
4. If the change is non-trivial (more than one file), write a short plan and get
   it approved before editing. See [`conventions/work-protocol.md`](conventions/work-protocol.md).

## House rules

- **Match the existing style.** This codebase has a consistent shape — follow it
  rather than introducing a new pattern. When in doubt, copy the nearest sibling
  service module (e.g. a new extractor field follows the same dict-driven
  pattern as the existing ones in `extractor_service.py`).
- **Keep layers honest.** Routers stay thin and delegate to services; services
  orchestrate; repositories own I/O. Don't put a model call in a router or
  taxonomy-file parsing in a service.
- **The taxonomy is the allowlist, not a suggestion.** Every field this service
  can output comes from `data/taxonomy.json` via `taxonomy_models.py`'s
  dynamically-built, `extra="forbid"` Pydantic models. Never hand-add a field
  name anywhere in the pipeline that isn't sourced from the taxonomy file —
  that's exactly the invariant the security requirements depend on.
- **New external dependency = a decision.** The taxonomy repository and the
  cache repository sit behind interfaces so their backing implementation can
  be swapped without touching the service layer. If it changes a rule, write
  an ADR in [`decisions/`](decisions/).
- **Verify, don't assert.** "It builds" is not "it works." Run it:
  `uv run pytest` for the test suite, and exercise `/parse` against a running
  container for anything touching the pipeline end-to-end.
- **No proprietary or cloud-coupled content beyond OpenAI.** This service is
  deliberately OpenAI-specific (see [ADR 0002](decisions/0002-openai-specific-repository.md))
  — don't add other vendor SDKs without a new ADR justifying the deviation.

## Definition of done

A change is done when **all** hold:

- It follows the conventions (or an ADR justifies the deviation).
- `uv run pytest` passes, including the red-team suite.
- The thing it changes runs end-to-end against a running container and the
  result is inspectable (`curl` against `/parse`, `/health`, or `/metrics`).
- Docs are updated if behavior or a convention changed — especially
  `docs/examples.md` if extraction behavior for a worked example changed.

## What this repo is NOT

- Not a place for half-finished files. If it exists, it should work.
- Not a dumping ground for clever abstractions. Prefer boring and obvious.
- Not a place to invent fields outside the taxonomy, even temporarily for
  testing — write a taxonomy fixture instead.
