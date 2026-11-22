# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The exact environment the commits were labeled in, so tests run here behave as they did there.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit (from sqlglot's own repository), byte-compiled so a read-only run
# never needs to write caches.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<'EOF'
import io, os, shutil, tarfile, urllib.request
commit = os.environ.get('SQLGLOT_COMMIT', 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa')
url = f'https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz'
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall('/opt', filter='data')
shutil.move(f'/opt/sqlglot-{commit}', '/opt/sqlglot')
EOF
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
    && python -m compileall -q /usr/local/lib/python3.13 \
    && chmod -R a+rX /opt/sqlglot

COPY selector.py verify.py verify_driver.py index.json.gz /app/
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV BR_SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
