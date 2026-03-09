"""
PostgreSQL database backend for the Receipt Scanner application.

Drop-in replacement for the default SQLite backend.  Activate by setting::

    DB_BACKEND=postgresql

in your ``.env`` file along with the ``POSTGRES_*`` connection variables.

Requirements::

    pip install psycopg2-binary

All methods return the same data shapes as the SQLite implementation,
so services and routes need **zero** changes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import ThreadedConnectionPool
except ImportError:
    raise ImportError(
        "PostgreSQL backend requires psycopg2.\n"
        "Install it with:  pip install psycopg2-binary"
    )

from app.config import (
    POSTGRES_HOST,
    POSTGRES_PORT,
    POSTGRES_DB,
    POSTGRES_USER,
    POSTGRES_PASSWORD,
    POSTGRES_MIN_CONN,
    POSTGRES_MAX_CONN,
)
from app.database import DatabaseBackend

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Schema Migrations (PostgreSQL dialect)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PG_MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "baseline_schema",
        """
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            product_code VARCHAR(10) UNIQUE NOT NULL,
            product_name VARCHAR(200) NOT NULL,
            category VARCHAR(50),
            unit VARCHAR(20) DEFAULT 'Piece',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            is_active BOOLEAN DEFAULT TRUE
        );
        CREATE INDEX IF NOT EXISTS idx_product_code ON products(product_code);
        CREATE INDEX IF NOT EXISTS idx_product_name ON products(product_name);

        CREATE TABLE IF NOT EXISTS receipts (
            id SERIAL PRIMARY KEY,
            receipt_number VARCHAR(50) UNIQUE NOT NULL,
            scan_date DATE NOT NULL,
            scan_time TIME NOT NULL,
            image_path VARCHAR(500),
            processed_image_path VARCHAR(500),
            processing_status VARCHAR(20) DEFAULT 'pending',
            total_items INTEGER DEFAULT 0,
            ocr_confidence_avg REAL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_receipt_date ON receipts(scan_date);

        CREATE TABLE IF NOT EXISTS receipt_items (
            id SERIAL PRIMARY KEY,
            receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
            product_code VARCHAR(10) NOT NULL,
            product_name VARCHAR(200),
            quantity REAL NOT NULL,
            unit VARCHAR(20),
            ocr_confidence REAL,
            manually_edited BOOLEAN DEFAULT FALSE
        );
        CREATE INDEX IF NOT EXISTS idx_receipt_items ON receipt_items(receipt_id);

        CREATE TABLE IF NOT EXISTS processing_logs (
            id SERIAL PRIMARY KEY,
            receipt_id INTEGER REFERENCES receipts(id),
            stage VARCHAR(50),
            status VARCHAR(20),
            duration_ms INTEGER,
            error_message TEXT,
            timestamp TIMESTAMP DEFAULT NOW()
        );
        """,
    ),
    (
        2,
        "composite_item_index",
        "CREATE INDEX IF NOT EXISTS idx_items_code_qty ON receipt_items(product_code, quantity);",
    ),
    # ── add future migrations here ──────────────────────────────────────
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PostgreSQL Implementation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PostgreSQLDatabase(DatabaseBackend):
    """PostgreSQL database backend with connection pooling and migrations."""

    def __init__(self) -> None:
        self._pool = ThreadedConnectionPool(
            minconn=POSTGRES_MIN_CONN,
            maxconn=POSTGRES_MAX_CONN,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
        )
        logger.info(
            "PostgreSQL pool created (%s@%s:%s/%s, %d–%d conns)",
            POSTGRES_USER, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
            POSTGRES_MIN_CONN, POSTGRES_MAX_CONN,
        )
        self._run_migrations()
        self._seed_default_products()

    # ── connection helpers ────────────────────────────────────────────────

    def _execute(
        self,
        query: str,
        params: tuple = (),
        *,
        fetch_one: bool = False,
        fetch_all: bool = False,
        returning_id: bool = False,
    ):
        """Run a single query and optionally fetch results.

        Uses a connection from the pool with auto-commit and auto-return.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                if returning_id:
                    result = cur.fetchone()["id"]
                elif fetch_one:
                    row = cur.fetchone()
                    result = dict(row) if row else None
                elif fetch_all:
                    result = [dict(r) for r in cur.fetchall()]
                else:
                    result = cur.rowcount
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _executemany(self, query: str, params_list: list[tuple]) -> None:
        """Run a parameterized query for each tuple in *params_list*."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.executemany(query, params_list)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ── migrations ────────────────────────────────────────────────────────

    def _run_migrations(self) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                conn.commit()

                cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
                current = cur.fetchone()["v"]

                for version, name, sql in _PG_MIGRATIONS:
                    if version <= current:
                        continue
                    for stmt in sql.split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                        (version, name),
                    )
                    conn.commit()
                    logger.info("PG migration applied: v%d — %s", version, name)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ── seeding ───────────────────────────────────────────────────────────

    def _seed_default_products(self) -> None:
        defaults = [
            ("ABC", "1L Exterior Paint", "Paint", "Litre"),
            ("XYZ", "1L Interior Paint", "Paint", "Litre"),
            ("PQR", "5L Primer White", "Paint", "Litre"),
            ("MNO", "Paint Brush 2 inch", "Accessories", "Piece"),
            ("DEF", "1L Wood Varnish", "Paint", "Litre"),
            ("GHI", "Sandpaper Sheet", "Accessories", "Piece"),
            ("JKL", "Putty Knife 4 inch", "Tools", "Piece"),
            ("STU", "Wall Filler 1kg", "Material", "Kg"),
            ("VWX", "Masking Tape 1 inch", "Accessories", "Roll"),
            ("RST", "Thinner 500ml", "Solvent", "Bottle"),
        ]
        self._executemany(
            "INSERT INTO products (product_code, product_name, category, unit) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (product_code) DO NOTHING",
            defaults,
        )
        logger.info("Default products seeded (PostgreSQL).")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Product CRUD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_all_products(self, active_only: bool = True) -> List[Dict]:
        query = "SELECT * FROM products"
        if active_only:
            query += " WHERE is_active = TRUE"
        query += " ORDER BY product_code"
        return self._execute(query, fetch_all=True)

    def get_product_by_code(self, code: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM products WHERE product_code = %s AND is_active = TRUE",
            (code.upper(),),
            fetch_one=True,
        )

    def add_product(self, code: str, name: str, category: str = "", unit: str = "Piece") -> Dict:
        # Try reactivating a soft-deleted product first
        reactivated = self._execute(
            "UPDATE products SET product_name = %s, category = %s, unit = %s, "
            "is_active = TRUE, updated_at = NOW() "
            "WHERE product_code = %s AND is_active = FALSE RETURNING *",
            (name, category, unit, code.upper()),
            fetch_one=True,
        )
        if reactivated:
            return reactivated

        self._execute(
            "INSERT INTO products (product_code, product_name, category, unit) "
            "VALUES (%s, %s, %s, %s)",
            (code.upper(), name, category, unit),
        )
        return self.get_product_by_code(code)

    def update_product(self, code: str, **kwargs) -> Optional[Dict]:
        fields, values = [], []
        for key, val in kwargs.items():
            if key in ("product_name", "category", "unit", "is_active"):
                fields.append(f"{key} = %s")
                values.append(val)
        if not fields:
            return self.get_product_by_code(code)

        fields.append("updated_at = NOW()")
        values.append(code.upper())
        self._execute(
            f"UPDATE products SET {', '.join(fields)} WHERE product_code = %s",
            tuple(values),
        )
        return self.get_product_by_code(code)

    def delete_product(self, code: str) -> bool:
        if not self.get_product_by_code(code):
            return False
        self.update_product(code, is_active=False)
        return True

    def search_products(self, query: str) -> List[Dict]:
        return self._execute(
            "SELECT * FROM products WHERE is_active = TRUE "
            "AND (product_code ILIKE %s ESCAPE '\\' OR product_name ILIKE %s ESCAPE '\\') "
            "ORDER BY product_code",
            (f"%{query}%", f"%{query}%"),
            fetch_all=True,
        )

    def get_product_code_map(self) -> Dict[str, str]:
        products = self.get_all_products()
        return {p["product_code"]: p["product_name"] for p in products}

    def get_product_catalog_full(self) -> Dict[str, Dict]:
        products = self.get_all_products()
        return {
            p["product_code"]: {
                "name": p["product_name"],
                "category": p["category"],
                "unit": p["unit"],
            }
            for p in products
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Receipt CRUD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def create_receipt(self, receipt_number: str, image_path: str = "", processed_image_path: str = "") -> int:
        now = datetime.now()
        return self._execute(
            "INSERT INTO receipts "
            "(receipt_number, scan_date, scan_time, image_path, processed_image_path) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                receipt_number,
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                image_path,
                processed_image_path,
            ),
            returning_id=True,
        )

    def add_receipt_items(self, receipt_id: int, items: List[Dict]) -> None:
        self._executemany(
            "INSERT INTO receipt_items "
            "(receipt_id, product_code, product_name, quantity, unit, ocr_confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (
                    receipt_id,
                    it.get("code", ""),
                    it.get("product", ""),
                    it.get("quantity", 0),
                    it.get("unit", "Piece"),
                    it.get("confidence", 0),
                )
                for it in items
            ],
        )
        avg_conf = sum(i.get("confidence", 0) for i in items) / max(len(items), 1)
        self._execute(
            "UPDATE receipts SET total_items = %s, processing_status = 'completed', "
            "ocr_confidence_avg = %s WHERE id = %s",
            (len(items), avg_conf, receipt_id),
        )

    def get_receipt(self, receipt_id: int) -> Optional[Dict]:
        receipt = self._execute(
            "SELECT * FROM receipts WHERE id = %s", (receipt_id,), fetch_one=True
        )
        if not receipt:
            return None
        items = self._execute(
            "SELECT * FROM receipt_items WHERE receipt_id = %s ORDER BY id",
            (receipt_id,),
            fetch_all=True,
        )
        receipt["items"] = items
        return receipt

    def get_recent_receipts(self, limit: int = 10) -> List[Dict]:
        return self._execute(
            "SELECT * FROM receipts ORDER BY created_at DESC LIMIT %s",
            (limit,),
            fetch_all=True,
        )

    def get_receipts_by_date(self, date_str: str) -> List[Dict]:
        rows = self._execute(
            "SELECT * FROM receipts WHERE scan_date = %s ORDER BY scan_time",
            (date_str,),
            fetch_all=True,
        )
        if not rows:
            return []

        receipt_ids = [r["id"] for r in rows]
        placeholders = ",".join(["%s"] * len(receipt_ids))
        items_rows = self._execute(
            f"SELECT * FROM receipt_items "
            f"WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id, id",
            tuple(receipt_ids),
            fetch_all=True,
        )

        items_by_receipt: Dict[int, List[Dict]] = {}
        for item in items_rows:
            items_by_receipt.setdefault(item["receipt_id"], []).append(item)

        for row in rows:
            row["items"] = items_by_receipt.get(row["id"], [])
        return rows

    def update_receipt_item(
        self, item_id: int, product_code: str, product_name: str, quantity: float
    ) -> bool:
        affected = self._execute(
            "UPDATE receipt_items SET product_code = %s, product_name = %s, "
            "quantity = %s, manually_edited = TRUE WHERE id = %s",
            (product_code, product_name, quantity, item_id),
        )
        return affected > 0

    def delete_receipt(self, receipt_id: int) -> bool:
        exists = self._execute(
            "SELECT id FROM receipts WHERE id = %s", (receipt_id,), fetch_one=True
        )
        if not exists:
            return False
        self._execute("DELETE FROM processing_logs WHERE receipt_id = %s", (receipt_id,))
        self._execute("DELETE FROM receipts WHERE id = %s", (receipt_id,))
        return True

    def add_receipt_item(
        self, receipt_id: int, product_code: str, product_name: str, quantity: float
    ) -> int:
        exists = self._execute(
            "SELECT id FROM receipts WHERE id = %s", (receipt_id,), fetch_one=True
        )
        if not exists:
            raise ValueError(f"Receipt not found: id={receipt_id}")

        # Look up unit from product catalog (default to Piece)
        unit = "Piece"
        product = self.get_product_by_code(product_code)
        if product:
            unit = product.get("unit", "Piece") or "Piece"

        new_id = self._execute(
            "INSERT INTO receipt_items "
            "(receipt_id, product_code, product_name, quantity, unit, ocr_confidence, manually_edited) "
            "VALUES (%s, %s, %s, %s, %s, 1.0, TRUE) RETURNING id",
            (receipt_id, product_code, product_name, quantity, unit),
            returning_id=True,
        )
        self._execute(
            "UPDATE receipts SET total_items = "
            "(SELECT COUNT(*) FROM receipt_items WHERE receipt_id = %s) WHERE id = %s",
            (receipt_id, receipt_id),
        )
        return new_id

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Processing Logs
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def add_processing_log(
        self, receipt_id: int, stage: str, status: str,
        duration_ms: int = 0, error_message: str = "",
    ) -> None:
        self._execute(
            "INSERT INTO processing_logs "
            "(receipt_id, stage, status, duration_ms, error_message) "
            "VALUES (%s, %s, %s, %s, %s)",
            (receipt_id, stage, status, duration_ms, error_message),
        )

    def add_processing_logs_batch(self, logs: List[Tuple]) -> None:
        if not logs:
            return
        self._executemany(
            "INSERT INTO processing_logs "
            "(receipt_id, stage, status, duration_ms, error_message) "
            "VALUES (%s, %s, %s, %s, %s)",
            logs,
        )

    def get_processing_logs(self, receipt_id: int) -> List[Dict]:
        return self._execute(
            "SELECT * FROM processing_logs WHERE receipt_id = %s ORDER BY timestamp",
            (receipt_id,),
            fetch_all=True,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Lifecycle
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def shutdown(self) -> None:
        """Close the connection pool."""
        self._pool.closeall()
        logger.info("PostgreSQL connection pool closed")
