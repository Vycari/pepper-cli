# API contract snapshot

`api-v1.openapi.json` is a **copy** of the OpenAPI schema that the Pepper server publishes for
`/api/v1/*` and `/auth/device/*`. The server repo (`Vycari/pepper`) is the source of truth.
**Do not hand-edit this file**: changes made here are overwritten by the next sync and mean
nothing to the server.

## What uses it

`tests/test_api_contract.py` reads every `# MIRROR: <server path>::<Model>` comment in
`src/pepper_cli/`, finds the schema `<Model>` in this file, and checks that every field of the
annotated dataclass is a property of that schema. The CLI may ignore server fields it does not
need; it may not read a field the server does not send. A mirror the test cannot check must be
listed, with a reason, in the test's allowlists.

## How it is refreshed

The server pushes it. After each green CI run on its `main`, `Vycari/pepper`'s contract-push
workflow (`planning/contract-push.md` there) copies this file in and, if it differs, opens or
updates a single `contract-sync` pull request, authored by the `vycari-contract-sync` App.
`SOURCE.json` records the server commit and CI run it came from. Nothing in this repo fetches it.

When a server change adds a field the CLI wants, the order is: the server change merges, the
snapshot is refreshed here, then the CLI change lands.

## Support

The `/api/v1` API is supported for Vycari's own clients only. This snapshot is published as part
of the CLI source for transparency, not as a stable public API for third parties.
