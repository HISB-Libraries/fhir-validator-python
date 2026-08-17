"""Application configuration.

All settings are overridable via environment variables (see field names,
upper-cased) or a local `.env` file. Defaults assume a container layout where
the validator jar lives at /opt/validator/validator_cli.jar and the FHIR
package/terminology cache lives under the process's home directory
(~/.fhir), which should be mounted as a persistent volume in production so
that downloaded IGs and cached ValueSets survive restarts.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # --- Validator engine process ---
    validator_jar_path: str = "/opt/validator/validator_cli.jar"
    validator_host: str = "127.0.0.1"
    validator_port: int = 8081
    auto_start_validator: bool = True
    """Set to False in tests/dev environments where java/the jar are unavailable."""

    # --- Validation engine options (only settable at engine startup, per
    # the validator's `server` subcommand -- see AGENTS.md) ---
    fhir_version: str = "4.0"
    startup_igs: str = ""
    """Comma-separated IG packages (e.g. "hl7.fhir.us.core#5.0.1,hl7.fhir.uv.ips")
    preloaded when the engine starts. Note: pydantic-settings would otherwise expect
    list-typed env vars to be JSON-encoded, so this is kept as a plain string and
    split via `startup_igs_list` for a simpler operator experience."""
    terminology_server: str | None = None
    """Passed as `-tx <url>`. Leave unset to use the validator's default (tx.fhir.org)."""
    validator_extra_args: str = ""
    """Comma-separated additional raw CLI args appended verbatim to the `server` subcommand."""
    packages_dir: str = "packages"
    """Directory of pre-extracted `<packageId>#<version>/package/...` folders (see
    app/package_cache.py) copied into the shared FHIR package cache (`~/.fhir/packages`)
    before the engine starts, so those IGs never need a network fetch. Relative paths are
    resolved against the process's current working directory. A missing directory is a
    no-op, not an error -- this is optional."""

    @property
    def startup_igs_list(self) -> list[str]:
        return [ig.strip() for ig in self.startup_igs.split(",") if ig.strip()]

    @property
    def validator_extra_args_list(self) -> list[str]:
        return [arg.strip() for arg in self.validator_extra_args.split(",") if arg.strip()]

    # --- Process lifecycle ---
    validator_startup_timeout_seconds: float = 300.0
    """Cold start (downloading/parsing IGs) can take minutes on first boot."""
    validator_request_timeout_seconds: float = 120.0

    # --- HTTP API ---
    default_response_format: str = "application/fhir+json"

    log_level: str = "INFO"
