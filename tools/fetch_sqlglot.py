"""Build step: download sqlglot at the pinned commit from sqlglot's own GitHub repository."""
import io
import shutil
import sys
import tarfile
import time
import urllib.request

commit, dest = sys.argv[1], sys.argv[2]
url = f'https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz'
for attempt in range(5):
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
        break
    except OSError as exc:
        print(f'download failed ({exc}); retrying', file=sys.stderr)
        time.sleep(3 * (attempt + 1))
else:
    raise SystemExit('could not download sqlglot')
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall('/tmp/sqlglot-src', filter='data')
shutil.move(f'/tmp/sqlglot-src/sqlglot-{commit}', dest)
print('sqlglot', commit, '->', dest, file=sys.stderr)
