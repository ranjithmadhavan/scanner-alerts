from google.cloud.firestore_v1._helpers import pbs_for_set_with_merge

from app.store import CachedStore, FirestoreStore, MemoryStore, db_stats


def test_firestore_update_replaces_each_field_whole():
    """A key removed from a map field has to go: the write's mask names the field, not the keys inside it."""
    masks = []

    class Ref:
        def collection(self, name):
            return self

        def document(self, doc_id):
            return self

        def set(self, data, merge=False):
            pb = pbs_for_set_with_merge("projects/p/databases/d/documents/c/x", data, merge=merge)[0]
            masks.append(sorted(pb.update_mask.field_paths))

    s = FirestoreStore.__new__(FirestoreStore)
    s._db = Ref()
    s.update("ma_scanners", "x", {"state": {"INFY": {"level": 1}}, "error": ""})
    s.update("ma_scanners", "x", {"state": {}})
    assert masks == [["error", "state"], ["state"]]


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
