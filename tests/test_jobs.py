# tests/test_jobs.py
import importlib

def test_job_helpers_set_get_and_evict(monkeypatch):
    import server; importlib.reload(server)
    server.JOBS.clear()
    for i in range(server.JOBS_MAX + 5):
        server.job_set(f"j{i}", {"state": "done"})
    # Never grows unbounded.
    assert len(server.JOBS) <= server.JOBS_MAX
    # Most-recent survive.
    last = f"j{server.JOBS_MAX + 4}"
    assert server.job_get(last) == {"state": "done"}
