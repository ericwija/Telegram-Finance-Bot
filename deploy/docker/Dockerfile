# Telegram-Finance-Bot V2 image: `finbot`, `finbot-migrate` and `finbot-admin` (Step 5A).
#
# V1 has its own root Dockerfile and is not affected by this file. This file's build context
# filter is deploy/docker/Dockerfile.dockerignore (BuildKit picks it up because it sits next to
# the Dockerfile), so no root .dockerignore is needed, and V1 builds are unaffected.
#
# Pinning:
#   * PYTHON_BASE defaults to the reviewed base below: exact tag AND multi-arch index digest.
#     Changing it is a reviewed commit (README §6.1). A build-arg override must also be an
#     exact 3.13 slim-trixie tag plus digest, or the build stops (check in the build stage).
#   * The image is built for linux/amd64 only (the VPS is x86_64; uv below is the x86_64
#     release). Any other target platform stops the build.
#   * uv is the CI version and is verified against the same SHA-256 (.github/workflows/ci.yml).
#   * Python dependencies come only from the committed uv.lock (`uv sync --frozen`, hashes).
#
# Runtime: no shell entrypoint, no secrets baked in, read-only root filesystem (compose), and
# a non-root numeric user chosen by compose. The image has no default command; compose sets it.

# python:3.13.17-slim-trixie, verified read-only against registry-1.docker.io on 2026-10-25:
#   index (multi-arch) sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
#     = Docker-Content-Digest header = SHA-256 of the index body = Docker Hub tag digest
#   linux/amd64 manifest sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18
#     config sha256:72036a705a7cff38924efd674e60654666cb283faf7b3fad160b34007f7395bd
#     (os=linux, architecture=amd64, PYTHON_VERSION=3.13.17, created 2026-10-21T22:36:12Z)
ARG PYTHON_BASE=python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

FROM ${PYTHON_BASE} AS build

# Refuse a floating base: exactly python:3.13.<patch>-slim-trixie@sha256:<digest>.
ARG PYTHON_BASE
RUN python -c "import os, re, sys; sys.exit(0 if re.fullmatch(r'python:3\.13\.\d+-slim-trixie@sha256:[0-9a-f]{64}', os.environ['PYTHON_BASE']) else 'PYTHON_BASE must be python:3.13.<patch>-slim-trixie@sha256:<digest>')"

# Refuse any platform but linux/amd64 (set by BuildKit from --platform).
ARG TARGETPLATFORM
RUN test "${TARGETPLATFORM:-linux/amd64}" = "linux/amd64" \
 || { echo "build for linux/amd64 only (got ${TARGETPLATFORM})" >&2; exit 1; }

ARG UV_VERSION=0.12.23
ARG UV_SHA256=9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6

# Fetch uv with the standard library only (the slim image has no curl) and refuse a mismatch.
RUN python - <<'PY'
import hashlib, io, os, tarfile, urllib.request
version, expected = os.environ["UV_VERSION"], os.environ["UV_SHA256"]
url = (
    f"https://github.com/astral-sh/uv/releases/download/{version}/"
    "uv-x86_64-unknown-linux-gnu.tar.gz"
)
data = urllib.request.urlopen(url, timeout=60).read()
actual = hashlib.sha256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"uv checksum mismatch: {actual}")
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    member = archive.getmember("uv-x86_64-unknown-linux-gnu/uv")
    with open("/usr/local/bin/uv", "wb") as out:
        out.write(archive.extractfile(member).read())
os.chmod("/usr/local/bin/uv", 0o755)
PY

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1

WORKDIR /app
# Dependencies first (layer cache), then the project. --frozen: uv.lock is the only source.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev \
 && /app/.venv/bin/finbot version \
 && /app/.venv/bin/python -c "import finbot.adapters.telegram.runtime, finbot.adapters.cli.migrate"

FROM ${PYTHON_BASE} AS runtime

# The project is installed in development mode (a .pth path to /app/src), so finbot-migrate
# finds /app/alembic.ini and /app/migrations exactly as in the repository layout.
COPY --from=build /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1

WORKDIR /app
USER 65534:65534
LABEL org.finbot.component=app
