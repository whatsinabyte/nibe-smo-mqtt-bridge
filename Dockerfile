# The HA build system passes --build-arg BUILD_ARCH=aarch64 (or amd64, armv7, etc.)
# but does NOT pass BUILD_FROM when build.yaml is absent.
# We resolve the correct architecture-specific base image here directly.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.24

# Clear any existing entrypoint from the base image
ENTRYPOINT []

# Install Python and required system packages.
# iputils provides `ping`, used by the "Test API Connection" debug button's
# connectivity check (nibe_connectivity_check.py) — explicit rather than
# relying on whatever busybox applets happen to be built into the base image.
# openssl provides the `openssl` CLI, used by tests/test_api_integration.py's
# TestTlsCertificateValidationAgainstARealServer to generate a throwaway
# self-signed cert — needed for the "Run Test Suite" debug button to pass
# inside this container, not by the bridge itself at runtime.
RUN apk add --no-cache python3 py3-pip bash curl jq iputils openssl

# Set working directory
WORKDIR /app

# Copy requirements first to maximise Docker layer cache reuse —
# these change less frequently than application code.
COPY app/requirements.txt ./
COPY requirements-test.txt ./

RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages \
 && pip3 install --no-cache-dir -r requirements-test.txt --break-system-packages

# Copy application code
COPY app/ ./

# Copy tests (used by the nightly test suite runner in debug mode).
# pytest.ini goes to / (addon root) so the runner finds it at /pytest.ini
# and pythonpath=app / testpaths=tests resolve to /app and /tests correctly.
COPY tests/ /tests/
COPY pytest.ini /

# translations/ is otherwise only read by the HA Supervisor directly from
# the add-on's source directory (not needed by the running container) —
# copied here anyway so TestConfigTranslationsParity (config.yaml vs.
# translations/*.yaml drift) can actually run for real when the test
# suite executes inside the deployed container, not just from a dev
# machine's repo checkout.
COPY translations/ /translations/

# BRIDGE_VERSION in generate_nibe_mqtt.py matches the version: field in config.yaml 
# so they can't drift apart without the test catching it.
COPY config.yaml /

# Copy and make run script executable
COPY run.sh /
RUN chmod a+x /run.sh

# OCI image labels (previously in build.yaml — moved here per Supervisor requirement)
LABEL org.opencontainers.image.title="Nibe S-Series MQTT Bridge"
LABEL org.opencontainers.image.description="MQTT bridge for Nibe S-series heat pump controllers"
LABEL org.opencontainers.image.source="https://github.com/whatsinabyte/nibe-smo-mqtt-bridge"
LABEL org.opencontainers.image.licenses="MIT"

# Python becomes PID 1 via exec in run.sh and receives signals directly
CMD ["/run.sh"]
