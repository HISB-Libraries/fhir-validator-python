# AGENTS

## Goal

FHIR Validation Service API built in Python using FastAPI, fronting the HL7
FHIR Validator CLI (`validator_cli.jar`).

## Architecture

```
+-------------------------------------------------------------------+
|                        Python FastAPI App                         |
|                     Endpoint: POST /fhir/$validate                |
|                     Endpoint: POST /fhir/$convert                 |
|                     Endpoint: GET  /fhir/$packages                |
|                     Endpoint: GET  /healthz                       |
+-------------------------------------------------------------------+
                                  |
   Parses Parameters resource (ig, profile, format, resource)
   -> app/fhir_parameters.py: parse_validate_parameters()
                                  |
                                  v
+-------------------------------------------------------------------+
|         ValidatorEngine (app/validator_engine.py)                 |
|  Owns a persistent subprocess:                                    |
|    java -jar validator_cli.jar server <port> -version <v> ...     |
|  Proxies over HTTP to that subprocess:                            |
|    POST /loadIG           (load new IGs into the running engine)  |
|    POST /validateResource  (validate; returns OperationOutcome)   |
|    POST /convert           (JSON<->XML format conversion)         |
+-------------------------------------------------------------------+
                                  |
     Caches IGs & deps    -> $HOME/.fhir/packages
     Caches terminology   -> $HOME/.fhir (default tx.fhir.org cache)
     Resolves terminology -> tx.fhir.org (unless -tx overridden)
```

Key point: the validator jar's `server` subcommand starts a **long-lived
process that itself serves HTTP** (`/validateResource`, `/loadIG`, etc). The
FastAPI app never shells out per-request — it starts this subprocess once at
app startup (`app/main.py` lifespan) and proxies each `$validate` call to it
over HTTP via `httpx.AsyncClient`. New IGs referenced by a request are loaded
into the *already-running* engine via `POST /loadIG` rather than by
restarting the process, which is what makes IG loading dynamic without
paying the (multi-second-to-minutes) engine construction cost again.

Full reference for the jar's HTTP server mode (verified against the real
`validator_cli.jar` during development):
https://confluence.hl7.org/spaces/FHIR/pages/441520076/Running+the+Validator+as+a+local+HTTP+service

## Request contract (`POST /fhir/$validate`)

Body is a FHIR `Parameters` resource, as **either JSON or XML** — the
envelope format is detected from the request's `Content-Type` header
(anything containing `xml` is parsed as FHIR XML via `app/fhir_xml.py`;
everything else is parsed as JSON via `app/fhir_parameters.py`). Both
parsers produce the same internal `ValidateRequest` shape:

- `ig` (0..*): IG package id/canonical, e.g. `"hl7.fhir.us.core#5.0.1"`
- `profile` (0..*): profile canonical URL to validate against
- `format` (0..1): media type of the resource being validated —
  `application/json`, `application/fhir+json`, `application/xml`, or
  `application/fhir+xml`. Ignored/irrelevant if the resource is embedded
  inline (see below), since an inline resource's representation always
  matches its envelope (JSON body -> JSON resource, XML body -> XML
  resource).
- `resource` (1..1, required): the resource to validate, provided as **one**
  of:
  - `parameter.resource` — an inline FHIR resource (JSON object or XML
    element, matching the envelope)
  - `parameter.valueString` — raw resource text, encoded per `format`
  - `parameter.valueBase64Binary` — raw resource bytes, encoded per `format`

Response representation is chosen via the request's `Accept` header
(`resolve_accept_format` in `app/fhir_parameters.py`); if absent/unrecognized
it defaults to the request envelope's own format (JSON in, JSON out; XML in,
XML out). This applies both to the upstream `OperationOutcome` and to
synthesized error outcomes we build ourselves (`app/main.py::_outcome_response`,
which uses `app/fhir_xml.py::operation_outcome_to_xml` for the XML case).

Note `app/fhir_xml.py` is a narrow, `$validate`-specific XML parser/serializer
(just `ig`/`profile`/`format`/`resource` extraction and a fixed-shape
OperationOutcome writer) — not a general FHIR XML<->JSON converter.

## `POST /fhir/$convert`

Converts a FHIR resource between its JSON and XML representations. Unlike
`$validate`, the body is the **raw resource itself** (no `Parameters`
wrapper) -- input format is detected from `Content-Type` the same way as
`$validate` (anything containing `xml` -> XML, else JSON).

Default behavior (no `Accept` header) is to **flip** the format -- JSON in
gives XML out and vice versa. This is a deliberate divergence from the
underlying validator engine's own `/convert`, which defaults to JSON output
regardless of input when `Accept` is omitted (verified against the real
jar); `app/main.py::convert` always computes and sends an explicit `Accept`
to the engine so the flip actually happens. An explicit `Accept:
application/fhir+json`/`application/fhir+xml` header overrides the flip
(e.g. to request a JSON->JSON round-trip/pretty-print), resolved the same
way as `$validate`'s response format (`resolve_accept_format`).

The upstream response (including its own conversion-error `OperationOutcome`
on bad input, HTTP 500) is passed through as-is; we only synthesize our own
error outcome for an empty request body or an unreachable/down engine.

## `GET /fhir/$packages`

Returns a plain JSON array (not a FHIR resource) describing the packages
listed in the `PACKAGES` env var (see table below), in the order given:

```json
[
  {"name": "hl7.fhir.us.core", "version": "5.0.1", "canonicalUrl": "hl7.fhir.us.core#5.0.1"}
]
```

Pure config reflection (`app/main.py::_package_summary`, splitting each
`<id>#<version>` string on the first `#`) — it doesn't touch the
`ValidatorEngine` at all, so it responds even if the engine isn't running or
the packages aren't actually loaded/cached. It's a static "what does this
deployment claim to support" list, not a live query of the engine's loaded
IGs (that's what `GET /healthz`'s `loaded_igs` field is for) or the package
cache on disk (that's `Settings.packages_dir`, see below) -- the three are
independent and can drift out of sync if you change one without the others.
`PACKAGES` defaults to exactly the IGs shipped in `packages/`, so update
both together when adding/removing one.

## Development commands

```bash
# one-time setup (editable install incl. dev deps)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# run the full test suite (all validator interactions are mocked — no
# Java/jar required)
.venv/bin/pytest -q

# run a single test file / test
.venv/bin/pytest tests/test_fhir_parameters.py -q
.venv/bin/pytest tests/test_validate_endpoint.py::test_validate_happy_path_returns_upstream_operation_outcome -q

# lint + format (must both be clean before committing)
.venv/bin/ruff check .
.venv/bin/ruff format .

# run the API locally against a real validator_cli.jar
curl -sL -o /tmp/validator_cli.jar \
  https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar
VALIDATOR_JAR_PATH=/tmp/validator_cli.jar .venv/bin/uvicorn app.main:app --reload
```

There is no separate typecheck/codegen step. `ruff check` + `pytest` is the
whole gate; run both before considering a change done.

> **Common pitfall:** running `uvicorn app.main:app` directly on the host
> (i.e. not via the Dockerfile) with no `VALIDATOR_JAR_PATH` set will fail
> with `Unable to access jarfile /opt/validator/validator_cli.jar` — that
> default path only exists inside the built Docker image. Always set
> `VALIDATOR_JAR_PATH` to a real downloaded jar (or `AUTO_START_VALIDATOR=false`)
> for local/host runs.

### Docker

```bash
docker build -t fhir-validator-service .
docker run -p 8080:8080 -v fhir-cache:/root/.fhir fhir-validator-service
```

The Dockerfile downloads the **latest** `validator_cli.jar` from GitHub at
build time (HL7 does not guarantee old versions keep working — see the
"Limitations of Use" section of the confluence docs — pin a release tag in
the Dockerfile if you need reproducible builds). Base image is
`python:3.12-slim` (Debian trixie), which requires
`openjdk-21-jre-headless` — `openjdk-17-jre-headless` is **not installable**
on trixie, don't revert to it.

The `/root/.fhir` volume is what makes the package/terminology cache survive
container restarts — always mount it in any real deployment, or every
restart re-downloads every IG and re-warms the terminology cache from
scratch (first boot took ~10s locally just for the base R4 package; adding
IGs adds more).

## Package cache preloading (`app/package_cache.py`)

Per the [FHIR Package Cache spec](https://confluence.hl7.org/display/FHIR/FHIR+Package+Cache),
a package is "installed" purely by the existence of a
`<packageId>#<version>/package/...` folder under the cache root
(`$HOME/.fhir/packages` — no separate manifest/registration step). That
means preloading a package is just: put its already-extracted folder there
*before* the validator subprocess starts, so it never needs a network fetch
for it.

`ValidatorEngine.start()` does this automatically on every startup: it
copies any `<packageId>#<version>` folders found in `Settings.packages_dir`
(default: `packages/`, relative to cwd) into `$HOME/.fhir/packages`,
**skipping any package that's already cached** (never overwrites/refreshes
an existing entry — this is a one-time seed, not a sync). Repo layout
expected:

```
packages/
  hl7.fhir.us.core#5.0.1/
    package/
      package.json
      StructureDefinition-....json
      ...
  hl7.fhir.uv.ips#2.0.0/
    package/
      ...
```

This is exactly the same folder shape as `$HOME/.fhir/packages` itself — you
can drop in a package by copying it straight from an existing cache, or by
extracting a `package.tgz` (`tar xzf package.tgz` into a dir named
`<id>#<version>`, with the `.tgz`'s `package/` folder as-is). Verified
manually: removed a real cached IG from `~/.fhir/packages`, restarted the
engine, confirmed the folder came back byte-for-byte from `packages/`, and
that `/loadIG` then succeeded for it.

The Dockerfile does `COPY packages ./packages` (before `pip install`), so
these are baked into the image and preloaded automatically the moment a
container starts -- no network fetch needed for them even on a totally
fresh deployment/volume. This requires `packages/` to exist in the build
context; if you fork this repo without that directory, remove that `COPY`
line or `docker build` will fail. Verified: a container started from a
fresh image with no pre-existing `/root/.fhir` volume had all packages
copied into `/root/.fhir/packages` (log line `Preloaded N package(s)...`)
*before* the validator subprocess even launched, and `/loadIG` for one of
them completed with `Load ... (00:00.000)` -- i.e. no fetch.

## Environment variables (`app/config.py`)

| Var | Default | Notes |
|---|---|---|
| `VALIDATOR_JAR_PATH` | `/opt/validator/validator_cli.jar` | path to the jar |
| `VALIDATOR_HOST` / `VALIDATOR_PORT` | `127.0.0.1` / `8081` | internal engine HTTP address, not the public API port |
| `AUTO_START_VALIDATOR` | `true` | set `false` for local dev/tests without Java/the jar |
| `FHIR_VERSION` | `4.0` | passed as `-version` when the engine starts |
| `STARTUP_IGS` | `` (empty) | **comma-separated**, e.g. `hl7.fhir.us.core#5.0.1,hl7.fhir.uv.ips` — NOT JSON, despite pydantic-settings' usual list-from-env convention (see comment in config.py for why) |
| `TERMINOLOGY_SERVER` | unset (-> tx.fhir.org) | passed as `-tx <url>` |
| `VALIDATOR_EXTRA_ARGS` | `` (empty) | comma-separated raw CLI args appended to `server ...` |
| `VALIDATOR_STARTUP_TIMEOUT_SECONDS` | `300` | cold start with big IGs can take minutes |
| `VALIDATOR_REQUEST_TIMEOUT_SECONDS` | `120` | per-request httpx timeout to the engine |
| `PACKAGES_DIR` | `packages` | dir of pre-extracted packages copied into `$HOME/.fhir/packages` on startup, see "Package cache preloading" above; missing dir is a no-op |
| `PACKAGES` | see `app/config.py` | comma-separated `<id>#<version>` list returned by `GET /fhir/$packages`; defaults to the IGs shipped in `packages/` -- pure string reflection, not validated against what's actually cached/loaded |

The public FastAPI port is set via the ASGI server invocation (`uvicorn
app.main:app --port ...`), not an env var.

## Known limitations (intentional, not bugs)

- `ig`/`profile` values only accept `valueString`/`valueUri`/`valueCanonical`/
  `valueCode`/`valueUrl` — not FHIR datatypes requiring deeper parsing.
- `resolve_accept_format` only matches exact media-type aliases from the
  `Accept` header (after stripping `;q=...`); it does not implement full
  HTTP content negotiation (multiple weighted candidates, wildcards beyond
  `*/*`).
- The `ValidatorEngine` runs a single validation engine per process (one
  FHIR version, one set of loaded IGs, shared across all requests). There is
  no per-request isolation; concurrent requests share loaded IGs, which is
  the entire point of the persistent-engine design (see Architecture above)
  but means you can't validate against conflicting IG *versions*
  simultaneously without loading both into the same engine.

## Testing notes

- `tests/conftest.py` provides a `FakeValidatorEngine` swapped onto
  `app.state.validator_engine` after FastAPI's lifespan startup runs (with
  `AUTO_START_VALIDATOR=false`, so no real subprocess is spawned in tests).
- `tests/test_validator_engine.py` covers `ValidatorEngine`'s subprocess
  lifecycle (readiness detection, fail-fast on early process exit, timeout)
  using trivial `sh`/`sleep` commands — no Java or the real jar needed for
  these either, just generic subprocess plumbing.
- `tests/test_package_cache.py` covers `preload_packages()` against `tmp_path`
  fixtures — also no Java/jar needed.
- `ValidatorEngine.start()` fails fast (not after waiting out
  `VALIDATOR_STARTUP_TIMEOUT_SECONDS`) if the subprocess exits before
  reporting readiness, and includes the process's own captured output (e.g.
  the "Unable to access jarfile" message above) in the raised
  `ValidatorEngineError`. If you touch the startup-wait logic in
  `app/validator_engine.py`, keep this property — it used to silently hang
  for the full timeout on any startup failure before this was fixed.
- If you need to test against the *real* jar (e.g. verifying a new HTTP
  contract assumption), download it fresh — don't commit it — and point
  `VALIDATOR_JAR_PATH` at it; `ValidatorEngine.start()`/`.stop()` manage the
  subprocess lifecycle directly and were manually verified this way during
  development (real engine start -> `/loadIG` -> `/validateResource` ->
  clean shutdown, and the same flow through the built Docker image).
