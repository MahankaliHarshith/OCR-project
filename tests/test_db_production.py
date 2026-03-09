"""
Comprehensive test suite for the 4 production database upgrades.

Tests:
  1. Connection Pool — thread-local reuse, multi-thread isolation
  2. Schema Migrations — version tracking, idempotence
  3. Daily Backup — snapshot creation, prune logic
  4. PostgreSQL Abstraction — interface compliance, factory routing
  5. Full CRUD — products, receipts, items, logs (regression)
  6. Concurrency — multi-thread writes don't corrupt data
  7. Rollback safety — errors don't leave partial state
  8. Shutdown — pool cleanup
"""

import os
import sys
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0
errors = []


def test(name):
    """Decorator to run a test and track pass/fail."""
    def decorator(fn):
        global passed, failed
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} → {e}")
            failed += 1
            errors.append((name, str(e)))
    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Use a temporary DB so we don't touch the real one
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TEMP_DIR = Path(tempfile.mkdtemp(prefix="db_test_"))
TEST_DB = TEMP_DIR / "test.db"
BACKUP_DIR = TEMP_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

print(f"\nTest DB: {TEST_DB}")
print(f"Backup dir: {BACKUP_DIR}\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. CONNECTION POOL TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("── 1. CONNECTION POOL ──")

from app.database import ConnectionPool

pool = ConnectionPool(TEST_DB)

@test("Same thread reuses same connection")
def _():
    c1 = pool.get()
    c2 = pool.get()
    assert c1 is c2, f"Expected same object, got id {id(c1)} vs {id(c2)}"

@test("Different threads get different connections")
def _():
    results = {}
    def worker(name):
        c = pool.get()
        results[name] = id(c)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["A"] != results["B"], "Threads should have different connections"

@test("Pool tracks all connections")
def _():
    assert len(pool._all_connections) >= 3, f"Expected ≥3, got {len(pool._all_connections)}"

@test("Stale connection is replaced")
def _():
    # Close the current thread's connection behind its back
    old_conn = pool.get()
    old_conn.close()
    new_conn = pool.get()
    # It should work (not raise ProgrammingError)
    new_conn.execute("SELECT 1").fetchone()

@test("close_all() clears all connections")
def _():
    pool.close_all()
    assert len(pool._all_connections) == 0, "Pool should be empty after close_all"

pool.close_all()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. SCHEMA MIGRATIONS TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 2. SCHEMA MIGRATIONS ──")

from app.database import MigrationManager, ConnectionPool as CP

# Fresh DB for migration tests
mig_db = TEMP_DIR / "mig_test.db"
mig_pool = CP(mig_db)

@test("Migration manager creates schema_migrations table")
def _():
    mm = MigrationManager(mig_pool)
    conn = mig_pool.get()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    assert row is not None, "schema_migrations table not created"

@test("Baseline migration (v1) created all tables")
def _():
    conn = mig_pool.get()
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    for t in ["products", "receipts", "receipt_items", "processing_logs"]:
        assert t in tables, f"Table {t} missing after v1 migration"

@test("v2 composite index was created")
def _():
    conn = mig_pool.get()
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_items_code_qty'"
    ).fetchone()
    assert idx is not None, "Composite index not created by v2"

@test("Current schema version is 2")
def _():
    mm = MigrationManager(mig_pool)
    assert mm._current_version() == 2, f"Expected v2, got v{mm._current_version()}"

@test("Migrations are idempotent (re-run doesn't crash)")
def _():
    # Creating another MigrationManager runs apply_pending again — should be no-op
    mm2 = MigrationManager(mig_pool)
    assert mm2._current_version() == 2

@test("Migration audit trail has timestamps")
def _():
    conn = mig_pool.get()
    rows = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert len(rows) == 2, f"Expected 2 migration records, got {len(rows)}"
    for r in rows:
        assert r["applied_at"] is not None, f"v{r['version']} missing applied_at"

mig_pool.close_all()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. DAILY BACKUP TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 3. DAILY BACKUP ──")

from app.database import BackupManager

# Create a small test DB file to back up
backup_src = TEMP_DIR / "backup_src.db"
conn = sqlite3.connect(str(backup_src))
conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
conn.execute("INSERT INTO test VALUES (1)")
conn.commit()
conn.close()

bk_dir = TEMP_DIR / "bk_test"
bk_dir.mkdir(exist_ok=True)
bm = BackupManager(backup_src, bk_dir, keep_days=3)

@test("First call creates today's backup")
def _():
    bm.ensure_daily_backup()
    today_file = bk_dir / f"receipt_scanner_{date.today().isoformat()}.db"
    assert today_file.exists(), f"Backup file not created: {today_file}"

@test("Second call is a no-op (idempotent)")
def _():
    today_file = bk_dir / f"receipt_scanner_{date.today().isoformat()}.db"
    mtime_before = today_file.stat().st_mtime
    time.sleep(0.1)
    bm.ensure_daily_backup()
    mtime_after = today_file.stat().st_mtime
    assert mtime_before == mtime_after, "Backup file was modified on 2nd call"

@test("Backup file contains valid data")
def _():
    today_file = bk_dir / f"receipt_scanner_{date.today().isoformat()}.db"
    conn = sqlite3.connect(str(today_file))
    row = conn.execute("SELECT id FROM test").fetchone()
    conn.close()
    assert row[0] == 1, f"Backup data corrupt: expected 1, got {row[0]}"

@test("Old backups are pruned")
def _():
    # Create a fake old backup (10 days ago)
    old_date = (date.today() - timedelta(days=10)).isoformat()
    old_file = bk_dir / f"receipt_scanner_{old_date}.db"
    old_file.write_text("fake")
    assert old_file.exists()

    # Force re-run (reset internal date tracker)
    bm._last_backup_date = None
    bm.ensure_daily_backup()

    assert not old_file.exists(), f"Old backup not pruned: {old_file}"

@test("Concurrent backup calls don't duplicate")
def _():
    bm._last_backup_date = None  # reset
    threads = [threading.Thread(target=bm.ensure_daily_backup) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    backups = list(bk_dir.glob("receipt_scanner_*.db"))
    assert len(backups) == 1, f"Expected 1 backup, got {len(backups)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. ABSTRACT INTERFACE & FACTORY TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 4. ABSTRACTION & FACTORY ──")

from app.database import DatabaseBackend, Database, get_database

@test("Database implements DatabaseBackend ABC")
def _():
    assert issubclass(Database, DatabaseBackend)

@test("get_database() returns Database for sqlite backend")
def _():
    db = get_database()
    assert isinstance(db, Database), f"Expected Database, got {type(db).__name__}"
    db.shutdown()

@test("DatabaseBackend has all required abstract methods")
def _():
    import inspect
    abstract_methods = {
        name for name, _ in inspect.getmembers(DatabaseBackend)
        if getattr(getattr(DatabaseBackend, name, None), "__isabstractmethod__", False)
    }
    expected = {
        "get_all_products", "get_product_by_code", "add_product", "update_product",
        "delete_product", "search_products", "get_product_code_map", "get_product_catalog_full",
        "create_receipt", "add_receipt_items", "get_receipt", "get_recent_receipts",
        "get_receipts_by_date", "update_receipt_item", "delete_receipt", "add_receipt_item",
        "add_processing_log", "add_processing_logs_batch", "get_processing_logs", "shutdown",
    }
    missing = expected - abstract_methods
    assert not missing, f"Missing abstract methods: {missing}"

@test("Database instance has all interface methods")
def _():
    db = Database(TEMP_DIR / "iface_test.db")
    for method_name in [
        "get_all_products", "get_product_by_code", "add_product", "update_product",
        "delete_product", "search_products", "get_product_code_map", "get_product_catalog_full",
        "create_receipt", "add_receipt_items", "get_receipt", "get_recent_receipts",
        "get_receipts_by_date", "update_receipt_item", "delete_receipt", "add_receipt_item",
        "add_processing_log", "add_processing_logs_batch", "get_processing_logs", "shutdown",
    ]:
        assert hasattr(db, method_name), f"Missing method: {method_name}"
        assert callable(getattr(db, method_name)), f"{method_name} not callable"
    db.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. FULL CRUD REGRESSION TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 5. FULL CRUD (regression) ──")

crud_db = Database(TEMP_DIR / "crud_test.db")

@test("Default products are seeded (10 products)")
def _():
    products = crud_db.get_all_products()
    assert len(products) == 10, f"Expected 10 seeded products, got {len(products)}"

@test("get_product_by_code finds ABC")
def _():
    p = crud_db.get_product_by_code("ABC")
    assert p is not None, "Product ABC not found"
    assert p["product_name"] == "1L Exterior Paint"

@test("add_product creates new product")
def _():
    p = crud_db.add_product("NEW1", "Test Product", "Test", "Box")
    assert p is not None
    assert p["product_code"] == "NEW1"
    assert p["product_name"] == "Test Product"

@test("update_product modifies name")
def _():
    p = crud_db.update_product("NEW1", product_name="Updated Product")
    assert p["product_name"] == "Updated Product"

@test("search_products finds by partial name")
def _():
    results = crud_db.search_products("Updated")
    assert len(results) >= 1
    assert any(r["product_code"] == "NEW1" for r in results)

@test("delete_product soft-deletes (is_active=0)")
def _():
    result = crud_db.delete_product("NEW1")
    assert result is True
    p = crud_db.get_product_by_code("NEW1")
    assert p is None, "Deleted product should not be found"

@test("add_product reactivates soft-deleted product")
def _():
    p = crud_db.add_product("NEW1", "Reactivated", "Test", "Box")
    assert p is not None
    assert p["product_name"] == "Reactivated"
    assert p["is_active"] == 1

@test("get_product_code_map returns dict")
def _():
    m = crud_db.get_product_code_map()
    assert isinstance(m, dict)
    assert "ABC" in m
    assert m["ABC"] == "1L Exterior Paint"

@test("get_product_catalog_full returns nested dict")
def _():
    c = crud_db.get_product_catalog_full()
    assert "ABC" in c
    assert "name" in c["ABC"]
    assert "unit" in c["ABC"]

@test("create_receipt returns integer ID")
def _():
    rid = crud_db.create_receipt("RCPT-001", "img.jpg", "proc.jpg")
    assert isinstance(rid, int)
    assert rid > 0

@test("add_receipt_items batch inserts correctly")
def _():
    rid = crud_db.create_receipt("RCPT-002")
    items = [
        {"code": "ABC", "product": "Paint", "quantity": 3, "unit": "Litre", "confidence": 0.95},
        {"code": "XYZ", "product": "Paint2", "quantity": 1, "unit": "Litre", "confidence": 0.88},
        {"code": "MNO", "product": "Brush", "quantity": 5, "unit": "Piece", "confidence": 0.70},
    ]
    crud_db.add_receipt_items(rid, items)

    receipt = crud_db.get_receipt(rid)
    assert receipt is not None
    assert receipt["total_items"] == 3
    assert receipt["processing_status"] == "completed"
    assert len(receipt["items"]) == 3
    # avg confidence = (0.95+0.88+0.70)/3 ≈ 0.843
    assert abs(receipt["ocr_confidence_avg"] - 0.843) < 0.01

@test("get_recent_receipts respects limit")
def _():
    r = crud_db.get_recent_receipts(limit=1)
    assert len(r) == 1

@test("get_receipts_by_date returns items with batch IN query")
def _():
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    results = crud_db.get_receipts_by_date(today)
    assert len(results) >= 1
    # At least RCPT-002 should have items
    rcpt2 = [r for r in results if r["receipt_number"] == "RCPT-002"]
    assert len(rcpt2) == 1
    assert len(rcpt2[0]["items"]) == 3

@test("update_receipt_item changes product and marks edited")
def _():
    receipt = crud_db.get_receipt(
        crud_db.create_receipt("RCPT-003")
    )
    # Need a receipt with items
    rid = crud_db.create_receipt("RCPT-004")
    crud_db.add_receipt_items(rid, [
        {"code": "ABC", "product": "Old", "quantity": 1, "confidence": 0.5},
    ])
    receipt = crud_db.get_receipt(rid)
    item_id = receipt["items"][0]["id"]

    ok = crud_db.update_receipt_item(item_id, "DEF", "New Name", 99)
    assert ok is True
    updated = crud_db.get_receipt(rid)
    item = updated["items"][0]
    assert item["product_code"] == "DEF"
    assert item["product_name"] == "New Name"
    assert item["quantity"] == 99
    assert item["manually_edited"] == 1

@test("update_receipt_item returns False for nonexistent")
def _():
    ok = crud_db.update_receipt_item(999999, "X", "X", 0)
    assert ok is False

@test("add_receipt_item adds to existing receipt")
def _():
    rid = crud_db.create_receipt("RCPT-005")
    crud_db.add_receipt_items(rid, [
        {"code": "ABC", "product": "P1", "quantity": 1, "confidence": 0.9},
    ])
    new_id = crud_db.add_receipt_item(rid, "XYZ", "P2", 5)
    assert isinstance(new_id, int)
    receipt = crud_db.get_receipt(rid)
    assert receipt["total_items"] == 2
    assert len(receipt["items"]) == 2

@test("add_receipt_item raises ValueError for missing receipt")
def _():
    try:
        crud_db.add_receipt_item(999999, "X", "X", 1)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

@test("delete_receipt removes receipt and cascades items")
def _():
    rid = crud_db.create_receipt("RCPT-DEL")
    crud_db.add_receipt_items(rid, [
        {"code": "ABC", "product": "P", "quantity": 1, "confidence": 0.9},
    ])
    ok = crud_db.delete_receipt(rid)
    assert ok is True
    assert crud_db.get_receipt(rid) is None

@test("delete_receipt returns False for nonexistent")
def _():
    ok = crud_db.delete_receipt(999999)
    assert ok is False

@test("add_processing_log creates log entry")
def _():
    rid = crud_db.create_receipt("RCPT-LOG")
    crud_db.add_processing_log(rid, "ocr", "success", 150, "")
    logs = crud_db.get_processing_logs(rid)
    assert len(logs) == 1
    assert logs[0]["stage"] == "ocr"
    assert logs[0]["duration_ms"] == 150

@test("add_processing_logs_batch inserts multiple")
def _():
    rid = crud_db.create_receipt("RCPT-LOGB")
    crud_db.add_processing_logs_batch([
        (rid, "preprocess", "success", 50, ""),
        (rid, "ocr", "success", 200, ""),
        (rid, "parse", "success", 30, ""),
    ])
    logs = crud_db.get_processing_logs(rid)
    assert len(logs) == 3

@test("add_processing_logs_batch with empty list is no-op")
def _():
    crud_db.add_processing_logs_batch([])  # Should not raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. CONCURRENCY TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 6. CONCURRENCY ──")

@test("10 threads writing receipts simultaneously — no corruption")
def _():
    conc_db = Database(TEMP_DIR / "conc_test.db")
    results = {"ok": 0, "err": 0}
    lock = threading.Lock()

    def writer(i):
        try:
            rid = conc_db.create_receipt(f"CONC-{i:03d}")
            conc_db.add_receipt_items(rid, [
                {"code": "ABC", "product": f"Item-{i}", "quantity": i, "confidence": 0.9},
            ])
            with lock:
                results["ok"] += 1
        except Exception as e:
            with lock:
                results["err"] += 1

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["ok"] == 10, f"Only {results['ok']}/10 succeeded, {results['err']} errors"
    all_receipts = conc_db.get_recent_receipts(limit=20)
    conc_receipts = [r for r in all_receipts if r["receipt_number"].startswith("CONC-")]
    assert len(conc_receipts) == 10, f"Expected 10 concurrent receipts, got {len(conc_receipts)}"
    conc_db.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. ROLLBACK SAFETY TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 7. ROLLBACK SAFETY ──")

@test("Duplicate receipt_number raises and doesn't leave partial state")
def _():
    rb_db = Database(TEMP_DIR / "rollback_test.db")
    rb_db.create_receipt("DUPE-001")
    try:
        rb_db.create_receipt("DUPE-001")  # duplicate
        assert False, "Should have raised"
    except Exception:
        pass
    # DB should still be usable
    r = rb_db.get_recent_receipts(limit=5)
    dupes = [x for x in r if x["receipt_number"] == "DUPE-001"]
    assert len(dupes) == 1, f"Expected 1, got {len(dupes)}"
    rb_db.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. SHUTDOWN TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 8. SHUTDOWN ──")

@test("shutdown() closes pool and DB stays intact after")
def _():
    sd_db = Database(TEMP_DIR / "shutdown_test.db")
    sd_db.create_receipt("SD-001")
    sd_db.shutdown()

    # Re-open and verify data survived
    sd_db2 = Database(TEMP_DIR / "shutdown_test.db")
    r = sd_db2.get_recent_receipts(limit=5)
    assert any(x["receipt_number"] == "SD-001" for x in r), "Data lost after shutdown"
    sd_db2.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. EXISTING DB BACKWARD COMPATIBILITY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("\n── 9. BACKWARD COMPAT (existing DB) ──")

@test("Opening the real production DB works (singleton import)")
def _():
    from app.database import db
    assert isinstance(db, DatabaseBackend)
    products = db.get_all_products()
    assert len(products) >= 10, f"Expected ≥10 products in production DB, got {len(products)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cleanup & Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

crud_db.shutdown()

# Clean up temp files
try:
    shutil.rmtree(TEMP_DIR)
except Exception:
    pass

print(f"\n{'='*50}")
print(f"  PASSED: {passed}")
print(f"  FAILED: {failed}")
print(f"{'='*50}")

if errors:
    print("\nFailed tests:")
    for name, err in errors:
        print(f"  ❌ {name}: {err}")

sys.exit(0 if failed == 0 else 1)
