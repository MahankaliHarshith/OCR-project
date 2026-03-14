"""
Production-grade database module for the Receipt Scanner application.

Features
────────
1. **Thread-local connection pool** — each thread reuses one SQLite connection
   instead of opening/closing on every call (10–50× fewer file-handle ops).
2. **Schema migration system** — versioned migrations with an audit trail in
   the ``schema_migrations`` table.  Add a new tuple to ``MIGRATIONS`` and the
   next startup applies it automatically.
3. **Automated daily backup** — before the first write of each day the DB file
   is copied to ``backups/``.  Old backups are pruned after *N* days.
4. **PostgreSQL-ready abstraction** — all business methods are declared in the
   ``DatabaseBackend`` ABC.  Swap ``DB_BACKEND=postgresql`` in your ``.env``
   to switch to the PostgreSQL implementation in ``db_postgres.py``.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import DATABASE_PATH, DB_BACKUP_DIR, DB_BACKUP_KEEP_DAYS

logger = logging.getLogger(__name__)

try:
    from app.metrics import set_db_connections as _set_db_connections
except Exception:
    def _set_db_connections(count):
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Thread-Local Connection Pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConnectionPool:
    """Thread-local SQLite connection pool.

    Each thread gets **one** persistent connection stored in
    ``threading.local()``.  Subsequent calls on the same thread reuse that
    connection — no open/close overhead per query.

    SQLite connections are **not** thread-safe, so one-per-thread is the
    correct pooling strategy.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_connections: list[sqlite3.Connection] = []
        self._closed = False
        logger.info(f"Connection pool initialised → {db_path}")

    # ── public API ────────────────────────────────────────────────────────

    def get(self) -> sqlite3.Connection:
        """Return the current thread's connection (creating one if needed)."""
        if self._closed:
            raise RuntimeError("Connection pool has been shut down")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")  # liveness check
                return conn
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                self._discard(conn)

        # Create a fresh connection for this thread
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._local.conn = conn

        with self._lock:
            self._all_connections.append(conn)
            _set_db_connections(len(self._all_connections))

        logger.debug(
            "Pooled connection created (thread=%s, total=%d)",
            threading.current_thread().name,
            len(self._all_connections),
        )
        return conn

    def close_all(self) -> None:
        """Close every connection in the pool (call at shutdown)."""
        self._closed = True  # Prevent new connections from any thread
        with self._lock:
            for c in self._all_connections:
                try:
                    c.close()
                except Exception:
                    pass
            closed = len(self._all_connections)
            self._all_connections.clear()
            _set_db_connections(0)
        self._local.conn = None
        logger.info("Connection pool closed (%d connections)", closed)

    # ── internals ─────────────────────────────────────────────────────────

    def _discard(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            try:
                self._all_connections.remove(conn)
            except ValueError:
                pass
            _set_db_connections(len(self._all_connections))
        try:
            conn.close()
        except Exception:
            pass
        self._local.conn = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Daily Backup Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BackupManager:
    """Copies the SQLite DB file before the first write of each calendar day.

    Old backups beyond ``keep_days`` are automatically pruned.
    """

    def __init__(self, db_path: Path, backup_dir: Path, keep_days: int = 7) -> None:
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.keep_days = keep_days
        self._last_backup_date: date | None = None
        self._lock = threading.Lock()
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def ensure_daily_backup(self) -> None:
        """No-op if today's backup already exists; otherwise snapshot + prune."""
        today = date.today()
        if self._last_backup_date == today:
            return  # fast path — already backed up today

        with self._lock:
            if self._last_backup_date == today:
                return  # re-check after acquiring lock

            self._snapshot(today)
            self._prune_old()
            self._last_backup_date = today

    def _snapshot(self, today: date) -> None:
        backup_name = f"receipt_scanner_{today.isoformat()}.db"
        dest = self.backup_dir / backup_name
        if dest.exists():
            return  # already created (e.g. earlier process today)

        if not self.db_path.exists():
            return  # nothing to back up yet

        try:
            # Use SQLite Online Backup API instead of shutil.copy2 —
            # file copy can capture a half-written WAL state, producing
            # a corrupt backup if a write transaction is in progress.
            source = sqlite3.connect(str(self.db_path))
            dest_conn = sqlite3.connect(str(dest))
            try:
                source.backup(dest_conn)
            finally:
                dest_conn.close()
                source.close()
            size_kb = dest.stat().st_size / 1024
            logger.info("Daily backup created: %s (%.1f KB)", dest.name, size_kb)
        except Exception as exc:
            logger.error("Backup failed: %s", exc)
            # Remove partial backup file if it was created
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass

    def _prune_old(self) -> None:
        cutoff = date.today() - timedelta(days=self.keep_days)
        for f in self.backup_dir.glob("receipt_scanner_*.db"):
            try:
                date_str = f.stem.replace("receipt_scanner_", "")
                file_date = date.fromisoformat(date_str)
                if file_date < cutoff:
                    f.unlink()
                    logger.info("Old backup removed: %s", f.name)
            except (ValueError, OSError):
                pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Schema Migration System
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _migration_v1_baseline(conn: sqlite3.Connection) -> None:
    """Create all baseline tables and indexes (idempotent)."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code VARCHAR(10) UNIQUE NOT NULL,
            product_name VARCHAR(200) NOT NULL,
            category VARCHAR(50),
            unit VARCHAR(20) DEFAULT 'Piece',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_code ON products(product_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_name ON products(product_name)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_number VARCHAR(50) UNIQUE NOT NULL,
            scan_date DATE NOT NULL,
            scan_time TIME NOT NULL,
            image_path VARCHAR(500),
            processed_image_path VARCHAR(500),
            processing_status VARCHAR(20) DEFAULT 'pending',
            total_items INTEGER DEFAULT 0,
            ocr_confidence_avg REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_receipt_date ON receipts(scan_date)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            product_code VARCHAR(10) NOT NULL,
            product_name VARCHAR(200),
            quantity REAL NOT NULL,
            unit VARCHAR(20),
            ocr_confidence REAL,
            manually_edited BOOLEAN DEFAULT 0,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_receipt_items ON receipt_items(receipt_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processing_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            stage VARCHAR(50),
            status VARCHAR(20),
            duration_ms INTEGER,
            error_message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id)
        )
    """)


def _migration_v2_composite_index(conn: sqlite3.Connection) -> None:
    """Add a composite index for faster analytics queries on receipt items."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_code_qty "
        "ON receipt_items(product_code, quantity)"
    )


def _migration_v3_add_prices(conn: sqlite3.Connection) -> None:
    """Add price columns to products, receipt_items, and receipts tables."""
    # Products: unit price for catalog-based price lookup
    try:
        conn.execute("ALTER TABLE products ADD COLUMN unit_price REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Receipt items: price per unit and line total (qty × price)
    try:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN unit_price REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN line_total REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass

    # Receipts: bill total (monetary grand total)
    try:
        conn.execute("ALTER TABLE receipts ADD COLUMN bill_total REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass

    # Seed default prices for existing products
    price_map = {
        "ABC": 200.0, "XYZ": 220.0, "PQR": 650.0, "MNO": 45.0,
        "DEF": 300.0, "GHI": 25.0, "JKL": 80.0, "STU": 180.0,
        "VWX": 35.0, "RST": 120.0,
        "TEW1": 250.0, "TEW4": 850.0, "TEW10": 1800.0, "TEW20": 3200.0,
        "PEPW1": 350.0, "PEPW4": 1200.0, "PEPW10": 2600.0, "PEPW20": 4800.0,
    }
    for code, price in price_map.items():
        conn.execute(
            "UPDATE products SET unit_price = ? WHERE product_code = ? AND unit_price = 0.0",
            (price, code),
        )


class MigrationManager:
    """Tracks and applies numbered schema migrations.

    How to add a new migration
    ──────────────────────────
    1. Write a function ``_migration_vN_<name>(conn)`` above this class.
    2. Append ``(N, "short_name", _migration_vN_<name>)`` to ``MIGRATIONS``.
    3. Restart the app — the migration runs automatically.

    The ``schema_migrations`` table keeps an audit trail of what was applied
    and when, so migrations never run twice.
    """

    MIGRATIONS: list[tuple[int, str, object]] = [
        (1, "baseline_schema", _migration_v1_baseline),
        (2, "composite_item_index", _migration_v2_composite_index),
        (3, "add_price_columns", _migration_v3_add_prices),
        # ── add future migrations here ──────────────────────────────────
    ]

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_tracking_table()
        self._apply_pending()

    # ── internals ─────────────────────────────────────────────────────────

    def _ensure_tracking_table(self) -> None:
        conn = self._pool.get()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version  INTEGER PRIMARY KEY,
                name     TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    def _current_version(self) -> int:
        conn = self._pool.get()
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        return (row["v"] or 0) if row else 0

    def _tables_exist(self) -> bool:
        """Check whether the old (pre-migration) tables are already present."""
        conn = self._pool.get()
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='products'"
        ).fetchone()
        return row is not None

    def _apply_pending(self) -> None:
        current = self._current_version()

        # Detect a pre-migration database (tables exist but no version record)
        if current == 0 and self._tables_exist():
            conn = self._pool.get()
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (1, "baseline_schema"),
            )
            conn.commit()
            current = 1
            logger.info("Schema migration: existing DB marked as v1 (baseline)")

        for version, name, migration in self.MIGRATIONS:
            if version <= current:
                continue

            conn = self._pool.get()
            try:
                if callable(migration):
                    migration(conn)
                elif isinstance(migration, str) and migration.strip():
                    for stmt in migration.split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            conn.execute(stmt)

                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
                conn.commit()
                logger.info("Schema migration applied: v%d — %s", version, name)
            except Exception as exc:
                conn.rollback()
                logger.error("Migration v%d (%s) failed: %s", version, name, exc)
                raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Abstract Database Interface (PostgreSQL-ready)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DatabaseBackend(ABC):
    """Abstract interface that **all** database backends must implement.

    Application code (services, routes) depends only on this interface,
    so swapping SQLite → PostgreSQL is a one-line config change.
    """

    # ── Products ──────────────────────────────────────────────────────────

    @abstractmethod
    def get_all_products(self, active_only: bool = True, limit: int = 0, offset: int = 0) -> List[Dict]: ...

    @abstractmethod
    def count_products(self, active_only: bool = True) -> int: ...

    @abstractmethod
    def get_product_by_code(self, code: str) -> Optional[Dict]: ...

    @abstractmethod
    def add_product(self, code: str, name: str, category: str = "", unit: str = "Piece") -> Dict: ...

    @abstractmethod
    def update_product(self, code: str, **kwargs) -> Optional[Dict]: ...

    @abstractmethod
    def delete_product(self, code: str) -> bool: ...

    @abstractmethod
    def search_products(self, query: str) -> List[Dict]: ...

    @abstractmethod
    def get_product_code_map(self) -> Dict[str, str]: ...

    @abstractmethod
    def get_product_catalog_full(self) -> Dict[str, Dict]: ...

    # ── Receipts ──────────────────────────────────────────────────────────

    @abstractmethod
    def create_receipt(self, receipt_number: str, image_path: str = "", processed_image_path: str = "") -> int: ...

    @abstractmethod
    def add_receipt_items(self, receipt_id: int, items: List[Dict]) -> None: ...

    @abstractmethod
    def get_receipt(self, receipt_id: int) -> Optional[Dict]: ...

    @abstractmethod
    def get_recent_receipts(self, limit: int = 10, offset: int = 0) -> List[Dict]: ...

    @abstractmethod
    def count_receipts(self) -> int: ...

    @abstractmethod
    def get_receipts_by_date(self, date_str: str) -> List[Dict]: ...

    @abstractmethod
    def update_receipt_item(self, item_id: int, product_code: str, product_name: str, quantity: float,
                            unit_price: float = 0.0, line_total: float = 0.0) -> bool: ...

    @abstractmethod
    def delete_receipt(self, receipt_id: int) -> bool: ...

    @abstractmethod
    def get_receipts_batch(self, receipt_ids: List[int]) -> List[Dict]: ...

    @abstractmethod
    def add_receipt_item(self, receipt_id: int, product_code: str, product_name: str, quantity: float,
                         unit_price: float = 0.0, line_total: float = 0.0) -> int: ...

    @abstractmethod
    def delete_receipt_item(self, item_id: int) -> bool: ...

    # ── Processing Logs ───────────────────────────────────────────────────

    @abstractmethod
    def add_processing_log(self, receipt_id: int, stage: str, status: str, duration_ms: int = 0, error_message: str = "") -> None: ...

    @abstractmethod
    def add_processing_logs_batch(self, logs: List[Tuple]) -> None: ...

    @abstractmethod
    def get_processing_logs(self, receipt_id: int) -> List[Dict]: ...

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @abstractmethod
    def shutdown(self) -> None:
        """Release all resources (connection pool, etc.)."""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. SQLite Implementation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Database(DatabaseBackend):
    """SQLite database backend with connection pooling, migrations, and backups."""

    def __init__(self, db_path: Path = DATABASE_PATH) -> None:
        self.db_path = db_path

        # Infrastructure
        self._pool = ConnectionPool(db_path)
        self._backup = BackupManager(db_path, DB_BACKUP_DIR, DB_BACKUP_KEEP_DAYS)

        # Enable WAL mode (persists in the DB file once set)
        conn = self._pool.get()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.commit()
            logger.info("SQLite WAL mode enabled (concurrent-safe)")
        except sqlite3.OperationalError as e:
            logger.warning(f"WAL mode failed ({e}), using DELETE mode (normal for OneDrive folders)")
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.commit()
            except Exception:
                pass

        # Run schema migrations
        self._migrations = MigrationManager(self._pool)

        # Seed default product catalog
        self._seed_default_products()

    # ── helpers ────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Shortcut: get the current thread's pooled connection."""
        return self._pool.get()

    def _before_write(self) -> None:
        """Trigger daily backup before the first mutating operation."""
        self._backup.ensure_daily_backup()

    # ── seeding ────────────────────────────────────────────────────────────

    def _seed_default_products(self) -> None:
        """Seed the database with default product catalog entries."""
        defaults = [
            # Pure-alpha codes (3 letters)                        (code, name, category, unit, price)
            ("ABC", "1L Exterior Paint", "Paint", "Litre", 200.0),
            ("XYZ", "1L Interior Paint", "Paint", "Litre", 220.0),
            ("PQR", "5L Primer White", "Paint", "Litre", 650.0),
            ("MNO", "Paint Brush 2 inch", "Accessories", "Piece", 45.0),
            ("DEF", "1L Wood Varnish", "Paint", "Litre", 300.0),
            ("GHI", "Sandpaper Sheet", "Accessories", "Piece", 25.0),
            ("JKL", "Putty Knife 4 inch", "Tools", "Piece", 80.0),
            ("STU", "Wall Filler 1kg", "Material", "Kg", 180.0),
            ("VWX", "Masking Tape 1 inch", "Accessories", "Roll", 35.0),
            ("RST", "Thinner 500ml", "Solvent", "Bottle", 120.0),
            # Alphanumeric codes (TEW = Thinnable Exterior Wash)
            ("TEW1", "1L Thinnable Exterior Wash", "Paint", "Litre", 250.0),
            ("TEW4", "4L Thinnable Exterior Wash", "Paint", "Litre", 850.0),
            ("TEW10", "10L Thinnable Exterior Wash", "Paint", "Litre", 1800.0),
            ("TEW20", "20L Thinnable Exterior Wash", "Paint", "Litre", 3200.0),
            # Alphanumeric codes (PEPW = Premium Exterior Premium Wash)
            ("PEPW1", "1L Premium Exterior Premium Wash", "Paint", "Litre", 350.0),
            ("PEPW4", "4L Premium Exterior Premium Wash", "Paint", "Litre", 1200.0),
            ("PEPW10", "10L Premium Exterior Premium Wash", "Paint", "Litre", 2600.0),
            ("PEPW20", "20L Premium Exterior Premium Wash", "Paint", "Litre", 4800.0),
        ]
        conn = self._conn()
        try:
            for code, name, category, unit, price in defaults:
                conn.execute(
                    "INSERT OR IGNORE INTO products (product_code, product_name, category, unit, unit_price) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, name, category, unit, price),
                )
            conn.commit()
            logger.info("Default products seeded.")
        except Exception as exc:
            conn.rollback()
            logger.error("Error seeding products: %s", exc)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Product CRUD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_all_products(self, active_only: bool = True, limit: int = 0, offset: int = 0) -> List[Dict]:
        """Get products from catalog with optional pagination.

        Args:
            active_only: Only return active (non-deleted) products.
            limit: Max rows to return (0 = unlimited).
            offset: Number of rows to skip.
        """
        logger.debug("get_all_products(active_only=%s, limit=%d, offset=%d)", active_only, limit, offset)
        conn = self._conn()
        query = "SELECT * FROM products"
        params: list = []
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY product_code"
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        products = [dict(row) for row in rows]
        logger.debug("get_all_products → returned %d products", len(products))
        return products

    def count_products(self, active_only: bool = True) -> int:
        """Return total count of products."""
        conn = self._conn()
        q = "SELECT COUNT(*) FROM products"
        if active_only:
            q += " WHERE is_active = 1"
        return conn.execute(q).fetchone()[0]

    def get_product_by_code(self, code: str) -> Optional[Dict]:
        """Get a single product by its code."""
        logger.debug("get_product_by_code(code=%r)", code)
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM products WHERE product_code = ? AND is_active = 1",
            (code.upper(),),
        ).fetchone()
        result = dict(row) if row else None
        logger.debug("get_product_by_code(%r) → %s", code, "found" if result else "NOT found")
        return result

    def add_product(self, code: str, name: str, category: str = "", unit: str = "Piece") -> Dict:
        """Add a new product to the catalog. Reactivates soft-deleted products."""
        self._before_write()
        conn = self._conn()
        try:
            # Check if product already exists (active)
            active = conn.execute(
                "SELECT * FROM products WHERE product_code = ? AND is_active = 1",
                (code.upper(),),
            ).fetchone()
            if active:
                raise ValueError(f"Product with code '{code.upper()}' already exists")

            # Check if a soft-deleted product with this code exists
            row = conn.execute(
                "SELECT * FROM products WHERE product_code = ? AND is_active = 0",
                (code.upper(),),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE products SET product_name = ?, category = ?, unit = ?, "
                    "is_active = 1, updated_at = ? WHERE product_code = ?",
                    (name, category, unit, datetime.now().isoformat(), code.upper()),
                )
                conn.commit()
                return self.get_product_by_code(code)

            conn.execute(
                "INSERT INTO products (product_code, product_name, category, unit) "
                "VALUES (?, ?, ?, ?)",
                (code.upper(), name, category, unit),
            )
            conn.commit()
            return self.get_product_by_code(code)
        except Exception:
            conn.rollback()
            raise

    def update_product(self, code: str, **kwargs) -> Optional[Dict]:
        """Update an existing product."""
        self._before_write()
        conn = self._conn()
        try:
            fields = []
            values = []
            for key, value in kwargs.items():
                if key in ("product_name", "category", "unit", "is_active"):
                    fields.append(f"{key} = ?")
                    values.append(value)

            if not fields:
                return self.get_product_by_code(code)

            fields.append("updated_at = ?")
            values.append(datetime.now().isoformat())
            values.append(code.upper())

            conn.execute(
                f"UPDATE products SET {', '.join(fields)} WHERE product_code = ?",
                values,
            )
            conn.commit()
            return self.get_product_by_code(code)
        except Exception:
            conn.rollback()
            raise

    def delete_product(self, code: str) -> bool:
        """Soft-delete a product (set is_active = 0)."""
        existing = self.get_product_by_code(code)
        if not existing:
            return False
        self.update_product(code, is_active=0)
        return True

    def search_products(self, query: str) -> List[Dict]:
        """Search products by code or name."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM products WHERE is_active = 1 "
            "AND (product_code LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\') "
            "ORDER BY product_code",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_product_code_map(self) -> Dict[str, str]:
        """Get a dict mapping product codes → product names."""
        products = self.get_all_products()
        return {p["product_code"]: p["product_name"] for p in products}

    def get_product_catalog_full(self) -> Dict[str, Dict]:
        """Get full product catalog keyed by code."""
        products = self.get_all_products()
        return {
            p["product_code"]: {
                "name": p["product_name"],
                "category": p["category"],
                "unit": p["unit"],
                "unit_price": p.get("unit_price", 0.0),
            }
            for p in products
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Receipt CRUD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def create_receipt(self, receipt_number: str, image_path: str = "", processed_image_path: str = "") -> int:
        """Create a new receipt record. Returns the receipt ID."""
        logger.debug("create_receipt(receipt_number=%r)", receipt_number)
        self._before_write()
        now = datetime.now()
        conn = self._conn()
        try:
            cursor = conn.execute(
                "INSERT INTO receipts "
                "(receipt_number, scan_date, scan_time, image_path, processed_image_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    receipt_number,
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M:%S"),
                    image_path,
                    processed_image_path,
                ),
            )
            conn.commit()
            receipt_id = cursor.lastrowid
            logger.info("Receipt created: id=%d, number=%s", receipt_id, receipt_number)
            return receipt_id
        except Exception:
            conn.rollback()
            raise

    def add_receipt_items(self, receipt_id: int, items: List[Dict]) -> None:
        """Add items to a receipt (batch insert)."""
        logger.debug("add_receipt_items(receipt_id=%d, items_count=%d)", receipt_id, len(items))
        self._before_write()
        conn = self._conn()
        try:
            conn.executemany(
                "INSERT INTO receipt_items "
                "(receipt_id, product_code, product_name, quantity, unit, ocr_confidence, unit_price, line_total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        receipt_id,
                        item.get("code", ""),
                        item.get("product", ""),
                        item.get("quantity", 0),
                        item.get("unit", "Piece"),
                        item.get("confidence", 0),
                        item.get("unit_price", 0.0),
                        item.get("line_total", 0.0),
                    )
                    for item in items
                ],
            )
            bill_total = sum(i.get("line_total", 0.0) for i in items)
            conn.execute(
                "UPDATE receipts SET total_items = ?, processing_status = 'completed', "
                "ocr_confidence_avg = ?, bill_total = ? WHERE id = ?",
                (
                    len(items),
                    sum(i.get("confidence", 0) for i in items) / max(len(items), 1),
                    bill_total,
                    receipt_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def get_receipt(self, receipt_id: int) -> Optional[Dict]:
        """Get a receipt with its items."""
        conn = self._conn()
        receipt = conn.execute(
            "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if not receipt:
            return None

        items = conn.execute(
            "SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY id",
            (receipt_id,),
        ).fetchall()

        result = dict(receipt)
        result["items"] = [dict(item) for item in items]
        return result

    def get_recent_receipts(self, limit: int = 10, offset: int = 0) -> List[Dict]:
        """Get most recent receipts with pagination."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_receipts(self) -> int:
        """Return total count of receipts."""
        conn = self._conn()
        return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def get_receipts_batch(self, receipt_ids: List[int]) -> List[Dict]:
        """Get multiple receipts with items in 2 queries (no N+1)."""
        if not receipt_ids:
            return []
        conn = self._conn()
        placeholders = ",".join("?" * len(receipt_ids))
        rows = conn.execute(
            f"SELECT * FROM receipts WHERE id IN ({placeholders}) ORDER BY created_at DESC",
            receipt_ids,
        ).fetchall()
        if not rows:
            return []

        items_rows = conn.execute(
            f"SELECT * FROM receipt_items WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id, id",
            receipt_ids,
        ).fetchall()

        items_by_receipt: Dict[int, List[Dict]] = {}
        for item in items_rows:
            rid = item["receipt_id"]
            items_by_receipt.setdefault(rid, []).append(dict(item))

        results = []
        for row in rows:
            receipt = dict(row)
            receipt["items"] = items_by_receipt.get(receipt["id"], [])
            results.append(receipt)
        return results

    def get_receipts_by_date(self, date_str: str) -> List[Dict]:
        """Get all receipts for a given date (YYYY-MM-DD).

        Uses a single batched IN (...) query for items rather than N+1
        individual queries, so 20 receipts = 2 queries instead of 21.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM receipts WHERE scan_date = ? ORDER BY scan_time",
            (date_str,),
        ).fetchall()
        if not rows:
            return []

        receipt_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(receipt_ids))
        items_rows = conn.execute(
            f"SELECT * FROM receipt_items "
            f"WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id, id",
            receipt_ids,
        ).fetchall()

        items_by_receipt: Dict[int, List[Dict]] = {}
        for item in items_rows:
            rid = item["receipt_id"]
            items_by_receipt.setdefault(rid, []).append(dict(item))

        results = []
        for row in rows:
            receipt = dict(row)
            receipt["items"] = items_by_receipt.get(receipt["id"], [])
            results.append(receipt)
        return results

    def update_receipt_item(
        self, item_id: int, product_code: str, product_name: str, quantity: float,
        unit_price: float = 0.0, line_total: float = 0.0,
    ) -> bool:
        """Update a receipt item (manual correction). Returns False if not found."""
        logger.debug("update_receipt_item(item_id=%d, code=%r, qty=%s)", item_id, product_code, quantity)
        self._before_write()
        conn = self._conn()
        try:
            cursor = conn.execute(
                "UPDATE receipt_items "
                "SET product_code = ?, product_name = ?, quantity = ?, "
                "unit_price = ?, line_total = ?, manually_edited = 1 "
                "WHERE id = ?",
                (product_code, product_name, quantity, unit_price, line_total, item_id),
            )
            if cursor.rowcount == 0:
                conn.commit()
                logger.warning("Receipt item not found: id=%d", item_id)
                return False
            # Recalculate parent receipt totals to stay consistent
            row = conn.execute(
                "SELECT receipt_id FROM receipt_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                rid = row["receipt_id"]
                conn.execute(
                    "UPDATE receipts SET total_items = "
                    "(SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?), "
                    "bill_total = "
                    "(SELECT COALESCE(SUM(line_total), 0) FROM receipt_items WHERE receipt_id = ?) "
                    "WHERE id = ?",
                    (rid, rid, rid),
                )
            conn.commit()
            logger.info("Receipt item updated: id=%d, code=%s, qty=%s", item_id, product_code, quantity)
            return True
        except Exception:
            conn.rollback()
            raise

    def delete_receipt(self, receipt_id: int) -> bool:
        """Delete a receipt, its items, and processing logs."""
        logger.debug("delete_receipt(receipt_id=%d)", receipt_id)
        self._before_write()
        conn = self._conn()
        try:
            row = conn.execute("SELECT id FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
            if not row:
                logger.warning("Receipt not found for deletion: id=%d", receipt_id)
                return False
            conn.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (receipt_id,))
            conn.execute("DELETE FROM processing_logs WHERE receipt_id = ?", (receipt_id,))
            conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
            conn.commit()
            logger.info("Receipt deleted: id=%d", receipt_id)
            return True
        except Exception as exc:
            conn.rollback()
            logger.error("Failed to delete receipt %d: %s", receipt_id, exc)
            raise  # Don't swallow — let API return 500 instead of misleading 404

    def add_receipt_item(
        self, receipt_id: int, product_code: str, product_name: str, quantity: float,
        unit_price: float = 0.0, line_total: float = 0.0,
    ) -> int:
        """Add a new item to an existing receipt. Returns the new item ID."""
        logger.debug("add_receipt_item(receipt_id=%d, code=%r, qty=%s)", receipt_id, product_code, quantity)
        self._before_write()
        conn = self._conn()
        try:
            row = conn.execute("SELECT id FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
            if not row:
                raise ValueError(f"Receipt not found: id={receipt_id}")
            # Look up unit from product catalog (default to Piece)
            unit = "Piece"
            product = self.get_product_by_code(product_code)
            if product:
                unit = product.get("unit", "Piece") or "Piece"
                # Use catalog price if caller didn't provide one
                if unit_price == 0.0 and product.get("unit_price", 0):
                    unit_price = product["unit_price"]
                    line_total = round(quantity * unit_price, 2)
            cursor = conn.execute(
                "INSERT INTO receipt_items "
                "(receipt_id, product_code, product_name, quantity, unit, ocr_confidence, manually_edited, unit_price, line_total) "
                "VALUES (?, ?, ?, ?, ?, 1.0, 1, ?, ?)",
                (receipt_id, product_code, product_name, quantity, unit, unit_price, line_total),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "UPDATE receipts SET total_items = "
                "(SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?), "
                "bill_total = "
                "(SELECT COALESCE(SUM(line_total), 0) FROM receipt_items WHERE receipt_id = ?) "
                "WHERE id = ?",
                (receipt_id, receipt_id, receipt_id),
            )
            conn.commit()
            logger.info("Receipt item added: id=%d, receipt=%d, code=%s", new_id, receipt_id, product_code)
            return new_id
        except Exception:
            conn.rollback()
            raise

    def delete_receipt_item(self, item_id: int) -> bool:
        """Delete a single receipt item and recalculate parent receipt totals."""
        logger.debug("delete_receipt_item(item_id=%d)", item_id)
        self._before_write()
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT receipt_id FROM receipt_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                logger.warning("Receipt item not found for deletion: id=%d", item_id)
                return False
            rid = row["receipt_id"]
            conn.execute("DELETE FROM receipt_items WHERE id = ?", (item_id,))
            # Recalculate parent receipt totals
            conn.execute(
                "UPDATE receipts SET total_items = "
                "(SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?), "
                "bill_total = "
                "(SELECT COALESCE(SUM(line_total), 0) FROM receipt_items WHERE receipt_id = ?) "
                "WHERE id = ?",
                (rid, rid, rid),
            )
            conn.commit()
            logger.info("Receipt item deleted: id=%d, receipt=%d", item_id, rid)
            return True
        except Exception:
            conn.rollback()
            raise

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Processing Logs
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def add_processing_log(
        self,
        receipt_id: int,
        stage: str,
        status: str,
        duration_ms: int = 0,
        error_message: str = "",
    ) -> None:
        """Log a processing step."""
        self._before_write()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO processing_logs "
                "(receipt_id, stage, status, duration_ms, error_message) "
                "VALUES (?, ?, ?, ?, ?)",
                (receipt_id, stage, status, duration_ms, error_message),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def add_processing_logs_batch(self, logs: List[Tuple]) -> None:
        """Batch-insert multiple processing log rows in a single DB round-trip.

        Args:
            logs: List of tuples (receipt_id, stage, status, duration_ms, error_message).
        """
        if not logs:
            return
        self._before_write()
        conn = self._conn()
        try:
            conn.executemany(
                "INSERT INTO processing_logs "
                "(receipt_id, stage, status, duration_ms, error_message) "
                "VALUES (?, ?, ?, ?, ?)",
                logs,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def get_processing_logs(self, receipt_id: int) -> List[Dict]:
        """Get processing logs for a receipt."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM processing_logs WHERE receipt_id = ? ORDER BY timestamp",
            (receipt_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Lifecycle
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def shutdown(self) -> None:
        """Close all pooled connections (call on app shutdown)."""
        self._pool.close_all()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Factory + Singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_database() -> DatabaseBackend:
    """Create the correct database backend based on ``DB_BACKEND`` config.

    Returns:
        A ``Database`` (SQLite) or ``PostgreSQLDatabase`` instance.
    """
    from app.config import DB_BACKEND

    if DB_BACKEND == "postgresql":
        from app.db_postgres import PostgreSQLDatabase
        return PostgreSQLDatabase()

    return Database()


# Backward-compatible singleton — ``from app.database import db`` still works.
db: DatabaseBackend = get_database()
