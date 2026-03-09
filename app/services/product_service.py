"""
Product Catalog Service.
Manages the product code-to-name mapping system with CRUD operations,
import/export, and fuzzy search capabilities.
"""

import csv
import io
import logging
from typing import List, Dict, Optional

from app.database import db

logger = logging.getLogger(__name__)


class ProductService:
    """Service layer for product catalog management."""

    def __init__(self):
        self.db = db

    def get_all_products(self, limit: int = 0, offset: int = 0) -> List[Dict]:
        """Get active products from the catalog (paginated)."""
        return self.db.get_all_products(active_only=True, limit=limit, offset=offset)

    def count_products(self) -> int:
        """Return total count of active products."""
        return self.db.count_products(active_only=True)

    def get_product(self, code: str) -> Optional[Dict]:
        """Get a single product by code."""
        return self.db.get_product_by_code(code.upper())

    def add_product(
        self,
        code: str,
        name: str,
        category: str = "",
        unit: str = "Piece",
    ) -> Dict:
        """
        Add a new product to the catalog.

        Args:
            code: Unique product code (e.g., "ABC").
            name: Full product name (e.g., "1L Exterior Paint").
            category: Product category (e.g., "Paint").
            unit: Unit of measurement (e.g., "Litre").

        Returns:
            The created product dict.

        Raises:
            ValueError: If product code already exists.
        """
        code = code.upper().strip()
        if not code:
            raise ValueError("Product code cannot be empty.")
        if not name.strip():
            raise ValueError("Product name cannot be empty.")

        logger.debug(f"add_product: code={code!r}, name={name!r}, category={category!r}, unit={unit!r}")
        existing = self.db.get_product_by_code(code)
        if existing:
            raise ValueError(f"Product code '{code}' already exists.")

        product = self.db.add_product(code, name.strip(), category.strip(), unit.strip())
        logger.info(f"Product added: {code} → {name}")
        return product

    def update_product(self, code: str, **kwargs) -> Optional[Dict]:
        """
        Update an existing product.

        Args:
            code: Product code to update.
            **kwargs: Fields to update (product_name, category, unit).

        Returns:
            Updated product dict or None if not found.
        """
        code = code.upper().strip()
        existing = self.db.get_product_by_code(code)
        if not existing:
            raise ValueError(f"Product code '{code}' not found.")

        result = self.db.update_product(code, **kwargs)
        logger.info(f"Product updated: {code}")
        return result

    def delete_product(self, code: str) -> bool:
        """
        Soft-delete a product from the catalog.

        Args:
            code: Product code to delete.

        Returns:
            True if deleted successfully.
        """
        code = code.upper().strip()
        result = self.db.delete_product(code)
        if result:
            logger.info(f"Product deleted: {code}")
        return result

    def search_products(self, query: str) -> List[Dict]:
        """Search products by code or name."""
        logger.debug(f"search_products(query={query!r})")
        results = self.db.search_products(query)
        logger.debug(f"search_products({query!r}) → {len(results)} results")
        return results

    def get_product_code_map(self) -> Dict[str, str]:
        """Get simple code → name mapping for the parser."""
        return self.db.get_product_code_map()

    def get_product_catalog_full(self) -> Dict[str, Dict]:
        """Get full catalog keyed by product code."""
        return self.db.get_product_catalog_full()

    def import_from_csv(self, csv_content: str) -> Dict:
        """
        Import products from CSV content.

        Expected CSV format: product_code,product_name,category,unit

        Args:
            csv_content: CSV string content.

        Returns:
            Import result with counts.
        """
        logger.debug(f"import_from_csv: content length={len(csv_content)} chars")
        reader = csv.DictReader(io.StringIO(csv_content))
        added = 0
        skipped = 0
        errors = []

        for row in reader:
            try:
                code = row.get("product_code", "").strip()
                name = row.get("product_name", "").strip()
                category = row.get("category", "").strip()
                unit = row.get("unit", "Piece").strip()

                if not code or not name:
                    errors.append(f"Missing code or name in row: {row}")
                    continue

                existing = self.db.get_product_by_code(code.upper())
                if existing:
                    skipped += 1
                    continue

                self.db.add_product(code, name, category, unit)
                added += 1

            except Exception as e:
                errors.append(f"Error importing row {row}: {e}")

        logger.info(f"CSV import: {added} added, {skipped} skipped, {len(errors)} errors")
        return {
            "added": added,
            "skipped": skipped,
            "errors": errors,
        }

    def export_to_csv(self) -> str:
        """
        Export all products to CSV string.

        Returns:
            CSV content as string.
        """
        products = self.get_all_products()
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["product_code", "product_name", "category", "unit"],
        )
        writer.writeheader()
        for p in products:
            writer.writerow(
                {
                    "product_code": p["product_code"],
                    "product_name": p["product_name"],
                    "category": p["category"],
                    "unit": p["unit"],
                }
            )
        return output.getvalue()


# Singleton
product_service = ProductService()
