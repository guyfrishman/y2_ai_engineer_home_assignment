# Code Style

## The rules

1. **Full self-explaining names.** No abbreviations, no single-letter names
   outside a trivial loop index. `canonical_query`, not `cq`; `token_logprobs`,
   not `tlp`. A function that does X is called `do_x()`.
2. **Docstrings only where behavior isn't obvious from name + signature.**
   Don't write a docstring that just restates the function name in prose.
   Do write one when there's a non-obvious invariant, a subtle algorithm
   (e.g. why `_scan_term_occurrences` matches longest-term-first), or a
   trade-off a reader can't infer from the code alone.
3. **Inline comments explain WHY, never WHAT.** `# Mazda's model "3" is a
   bare digit — too ambiguous against room counts to index globally` is a
   why-comment. `# loop over models` is not — delete it.
4. **No section-header comments.** Don't write `# --- Helpers ---` or
   `# === Validation ===` to visually break up a file. If a file needs that
   much internal structure, it's a sign the file should be split, not
   decorated.
5. **One concept per file.** `openai_repository.py` only talks to OpenAI;
   `cache_repository.py` only caches. Don't mix concerns.
6. **Pydantic v2 for data that crosses a boundary** (HTTP, function calls
   between layers). Request/response/taxonomy models live in `y2_ai_search_api/schema/`.
7. **Thin layers.** Routers delegate to services; services orchestrate;
   repositories own I/O. See [routers.md](routers.md) and [repositories.md](repositories.md).
8. **No clever metaprogramming beyond what the taxonomy genuinely needs.**
   `taxonomy_models.py`'s dynamic `create_model(...)` calls are the one
   deliberate exception — the alternative (hand-writing ~70 Hebrew field
   names across three models, kept in sync with the taxonomy file by hand)
   is worse. Don't add a second one without an equally strong reason.
9. **No `print` in app code.** Use the logger — see [logging.md](logging.md).
10. **Match the existing style.** If the codebase does it a certain way, do
    it that way too.

## Python

- Python 3.12, dependencies managed with [uv](https://docs.astral.sh/uv/).
- Modern type hints: `dict`, `list`, `str | None` — not `Dict`, `List`, `Optional`.
- `from typing import Literal` for restricted string sets — and prefer it:
  taxonomy enum fields are built as `Literal[...]`, not bare `str`, so an
  out-of-taxonomy value is a validation error, not silently accepted data.
- `pydantic_settings.BaseSettings` for env config (see [configuration.md](configuration.md)).

## Imports

- Standard library, then third-party, then local — one blank line between groups.
- Absolute imports from the project root (`from services... import ...`) —
  `y2_ai_search_api/` has no `app` package wrapper; modules live directly
  at the project root (`config.py`, `services/`, `repositories/`, ...).
- No wildcard imports.

## Naming

- Files: `snake_case.py`. Classes: `PascalCase`. Functions/vars: `snake_case`.
  Constants: `UPPER_SNAKE_CASE`.
- Confidence-band and threshold constants are always named
  (`LOGPROB_WEIGHT`, `DEGRADED_CONFIDENCE`, `MARGIN_FACTOR_MIN`, ...), never
  inlined as bare numbers — they're the kind of value that gets re-tuned
  against the example set, and a name at the point of use makes that safe.
- Router handler functions are the one place names stay **short** verb-nouns
  (`parse`, `health`) because the path + `summary=` already describe the
  action. See [routers.md](routers.md).
- Hebrew field names (`מס׳_חדרים`, `ק״מ`, ...) are used verbatim as Python
  identifiers and Pydantic field names where they cross the taxonomy
  boundary — Python identifiers are Unicode-aware, this is valid and
  intentional, and translating them to English would break the "the
  taxonomy file is the single source of truth" property the whole schema
  layer depends on.

## When code disagrees with this doc

The code is wrong — fix it. If you think the rule is wrong, write an ADR in
[`../decisions/`](../decisions/) before changing the code.
