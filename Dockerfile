# The HA build system passes --build-arg BUILD_ARCH=aarch64 (or amd64, armv7, etc.)
# but does NOT pass BUILD_FROM when build.yaml is absent.
# We resolve the correct architecture-specific base image here directly.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.24

# Clear any existing entrypoint from the base image
ENTRYPOINT []

# Install Python and required system packages
RUN apk add --no-cache python3 py3-pip bash curl jq

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

# BRIDGE_VERSION in generate_nibe_mqtt.py matches the version: field in config.yaml 
# so they can't drift apart without the test catching it.
COPY config.yaml /

# Copy and make run script executable
COPY run.sh /
RUN chmod a+x /run.sh

# Python becomes PID 1 via exec in run.sh and receives signals directly
CMD ["/run.sh"]