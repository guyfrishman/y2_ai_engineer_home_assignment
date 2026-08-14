# Working in this repo
Read README.md and docs/DESIGN.md first. `data/taxonomy.json` is the
allowlist for every field this service can output — never hand-add a field
name anywhere in the pipeline that isn't sourced from it. `uv run pytest`
must stay green.
