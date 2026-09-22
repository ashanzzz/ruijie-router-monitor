# Verification Record

Verification date: 2026-09-22

## Static and unit checks

```text
python -m compileall -q backend
pytest -q
node --check scratch/temp_frontend.js
git diff --check
```

Result:

```text
11 passed
Python compilation passed
Frontend JavaScript syntax passed
Git whitespace check passed
```

The test set covers:

- database status when the active database is unavailable;
- SQLite temporary-table read and write verification;
- RSSI parsing and persistence;
- incremental session traffic calculation;
- normal and starred retention periods;
- preservation of device identity and user metadata;
- the eWeb AES login encryption vector;
- SID-based API authentication;
- both eWeb request signature headers;
- direct concurrent requests for topology and clients;
- 24-hour and 30-day retention behavior;
- traffic and RSSI aggregation;
- short same-AP dropout inference;
- one-year inactive-client purge for client materials and history;

## Live device verification

The new Python HTTP client connected to the configured local eWeb device.

Observed result:

```text
first discovery:        3430 ms
warm poll:              559 ms
network nodes:          10
clients:                64
snapshot complete:      true
```

Both `local_topology` and `user_list` returned HTTP 200 with business code 0.

No router configuration endpoint was called.

## Not run

Docker was not available in this environment. A complete image build was not run.
