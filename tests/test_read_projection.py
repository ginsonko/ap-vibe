from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from ap_mind.read_projection import ReadProjectionCache


def test_same_key_shares_work_and_other_project_is_independent():
    cache = ReadProjectionCache()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"project_id": "a"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(cache.read, "a", slow)
        assert entered.wait(1)
        others = [pool.submit(cache.read, "a", slow) for _ in range(5)]
        assert cache.read("b", lambda: {"project_id": "b"})["project_id"] == "b"
        release.set()
        assert all(f.result()["project_id"] == "a" for f in [first, *others])
    assert len(calls) == 1


def test_failure_retry_expiry_and_invalidation(monkeypatch):
    now = [1.0]
    monkeypatch.setattr("ap_mind.read_projection.time.monotonic", lambda: now[0])
    cache = ReadProjectionCache()
    with pytest.raises(ValueError):
        cache.read("a", lambda: (_ for _ in ()).throw(ValueError("unavailable")))
    assert cache.read("a", lambda: {"revision": 1})["revision"] == 1
    now[0] = 4.0
    assert cache.read("a", lambda: {"revision": 2})["revision"] == 2
    entered, release = threading.Event(), threading.Event()
    def old_read():
        entered.set()
        assert release.wait(2)
        return {"revision": 3}
    cache.invalidate()
    with ThreadPoolExecutor() as pool:
        old = pool.submit(cache.read, "a", old_read)
        assert entered.wait(1)
        cache.invalidate()
        assert cache.read("a", lambda: {"revision": 4})["revision"] == 4
        release.set()
        assert old.result()["revision"] == 3
    assert cache.read("a", lambda: {"revision": 5})["revision"] == 4
