from app.store import CachedStore, MemoryStore, db_stats


def test_cache_serves_reads_and_writes_invalidate():
    backend = MemoryStore()
    s = CachedStore(backend, ttl=300)
    stats = [0, 0.0]
    token = db_stats.set(stats)
    try:
        s.put("alerts", "a", {"user": "u", "status": "active"})
        assert s.list("alerts", user="u")[0]["status"] == "active"
        calls = stats[0]
        s.list("alerts", user="u"); s.get("alerts", "a"); s.get("alerts", "a")
        assert stats[0] == calls + 1  # list cached; one get miss, then cached

        s.update("alerts", "a", {"status": "triggered"})  # write drops cached doc + lists
        assert s.get("alerts", "a")["status"] == "triggered"
        assert s.list("alerts", user="u")[0]["status"] == "triggered"
        assert s.list("alerts", status="active") == []

        s.delete("alerts", "a")
        assert s.get("alerts", "a") is None and s.list("alerts", user="u") == []

        # callers can't corrupt the cache by mutating what they got back
        s.put("users", "x", {"modules": ["scanner"]})
        s.get("users", "x")["modules"].append("broker")
        assert s.get("users", "x")["modules"] == ["scanner"]
    finally:
        db_stats.reset(token)
