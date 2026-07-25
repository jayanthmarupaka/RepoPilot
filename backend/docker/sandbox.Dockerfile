# Minimal sandbox image for isolated test execution.
# This image is used by `docker run --rm` per test run.
# It intentionally has NO app code — only test tooling.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Pre-install common test deps — the repo's own deps
# are installed at runtime via `pip install -e .` or `pip install -r requirements.txt`
# inside the mounted /workspace directory.
RUN pip install --no-cache-dir pytest pytest-cov

WORKDIR /workspace

# Entry: runs pytest in /workspace (the bind-mounted repo clone)
CMD ["pytest", "--tb=short", "-q"]
