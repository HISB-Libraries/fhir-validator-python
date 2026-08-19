# syntax=docker/dockerfile:1
FROM python:3.12-slim

# --- Java runtime for validator_cli.jar ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless curl \
    && rm -rf /var/lib/apt/lists/*

# --- HL7 FHIR Validator CLI ---
# Always pulls the latest release per HL7 guidance (older pinned versions may
# stop working -- see https://confluence.hl7.org/spaces/FHIR/pages/35718580).
# Pin to a specific release tag here if you need reproducible builds instead.
RUN mkdir -p /opt/validator \
    && curl -sL -o /opt/validator/validator_cli.jar \
       https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar

ENV VALIDATOR_JAR_PATH=/opt/validator/validator_cli.jar

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
# Sample resource used for the startup warm-up validation (see
# ValidatorEngine.validate_initial_load_resource()); resolved relative to
# this WORKDIR by the default INITIAL_LOAD_RESOURCE_PATH.
COPY initial_load_resource.json ./
# Pre-extracted IG packages (see app/package_cache.py) -- the last-resort
# fallback tier for PACKAGES/DEFAULT_IG entries (after the FHIR package
# registry and build.fhir.org), and preloaded proactively for any
# STARTUP_IGS entry found here. Requires packages/ to exist in the build
# context (it's checked into this repo); if you fork this without that
# directory, remove this line.
COPY packages ./packages
RUN pip install --no-cache-dir .

# Persistent FHIR package + terminology cache. The validator writes to
# ~/.fhir under the process's HOME (== /root here); mount a volume here in
# production so IGs/ValueSets survive container restarts.
VOLUME ["/root/.fhir"]

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
