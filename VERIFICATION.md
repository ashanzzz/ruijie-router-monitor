# Verification Record

Upstream repository reviewed:

```text
https://github.com/ashanzzz/ruijie-router-monitor
```

Reviewed latest commit:

```text
ad6bd267b4b6d4f2544b3f9e0d9d097bba0f0ce7
```

Validation performed on this repaired tree:

```bash
python -m compileall -q backend
pytest -q
node --check /tmp/ruijie-frontend.js
git diff --check
```

Result:

```text
7 passed
Python compilation passed
Frontend JavaScript syntax passed
Git whitespace check passed
```

Test coverage includes:

- settings/config API contract when the active database is unavailable;
- SQLite temporary-table read/write verification;
- RSSI parsing and persistence;
- incremental session traffic calculation;
- 30-day normal / 180-day starred retention behavior;
- preservation of device identities, aliases and star flags;
- disabled self-restart behavior;
- password-only router login guard;
- absence of a fixed reset password in the frontend.

Docker was not available in the execution environment, so a complete `docker build` was not executed here. The Dockerfile and Compose file were statically reviewed, and the Python application was exercised through FastAPI TestClient.
