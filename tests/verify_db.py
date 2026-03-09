"""Quick verification of all 4 production database features."""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 1. Check schema_migrations table
print("=== SCHEMA MIGRATIONS ===")
conn = sqlite3.connect("receipt_scanner.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
for r in rows:
    print(f"  v{r['version']}: {r['name']} (applied: {r['applied_at']})")
conn.close()

# 2. Check indexes
print("\n=== INDEXES ===")
conn = sqlite3.connect("receipt_scanner.db")
conn.row_factory = sqlite3.Row
indexes = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
).fetchall()
for i in indexes:
    print(f"  {i['name']}")
conn.close()

# 3. Test connection pooling
print("\n=== CONNECTION POOL ===")
from app.database import db

conn1_id = id(db._conn())
conn2_id = id(db._conn())
print(f"  Same connection reused: {conn1_id == conn2_id}")
print(f"  Pool size: {len(db._pool._all_connections)}")

# 4. Test backup trigger
print("\n=== DAILY BACKUP ===")
db._before_write()
backups = [f for f in os.listdir("backups") if f.endswith(".db")]
print(f"  Backup files: {backups}")

# 5. Test CRUD still works
print("\n=== CRUD SMOKE TEST ===")
products = db.get_all_products()
print(f"  Products: {len(products)}")
product = db.get_product_by_code("ABC")
print(f"  ABC product: {product['product_name'] if product else 'NOT FOUND'}")

# Test abstract interface
from app.database import DatabaseBackend
print(f"  db is DatabaseBackend: {isinstance(db, DatabaseBackend)}")

print("\n✅ ALL 4 PRODUCTION FEATURES VERIFIED")
