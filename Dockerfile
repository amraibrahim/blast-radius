# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The exact package set the commits were labeled with, so tests run here behave as they did there.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, from sqlglot's own repository, byte-compiled so the read-only run
# never needs to write caches.
COPY tools/fetch_sqlglot.py /app/fetch_sqlglot.py
RUN python /app/fetch_sqlglot.py eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
    && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
    && chmod -R a+rX /opt/sqlglot

# Runtime: selector, verifier and the maps built from the pinned commit's own test run.
COPY selector.py verify.py verify_driver.py index.json.gz /app/
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV BR_SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
