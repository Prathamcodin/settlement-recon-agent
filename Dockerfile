FROM python:3.11-slim

WORKDIR /app

# Install pinned deps at build time. run.py itself only needs the
# stdlib, but the FastAPI surface and the --record-live path need
# these — installing at build time keeps the runtime offline-clean.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Source, evidence, tests, fixtures, scripts, entrypoint.
# data/ is copied as-is (only .gitkeep in the repo); run.py regenerates
# the CSVs from the seeded generator on first run inside the container.
COPY src/       ./src/
COPY tests/     ./tests/
COPY scripts/   ./scripts/
COPY evidence/  ./evidence/
COPY data/      ./data/
COPY run.py     ./

# Non-root user.
RUN useradd -m -u 1000 recon && chown -R recon:recon /app
USER recon

# Default: run the full pipeline once and exit with the summary printed.
# No API key or network is required at runtime — the classifier replays
# from the committed evidence/llm_raw/ recordings.
CMD ["python", "run.py"]
