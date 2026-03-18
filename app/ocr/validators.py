"""
Smart Validation Rules Engine for parsed receipt data.

Post-parse validation that catches OCR errors the parser missed:
1. Impossible quantity detection (zero, negative, absurdly high)
2. Price sanity checks (missing prices, extreme deviations, math errors)
3. Duplicate item flagging (same code appears multiple times)
4. Cross-receipt anomaly detection (qty far exceeds historical patterns)
"""

import logging

logger = logging.getLogger(__name__)


class ReceiptValidator:
    """Validates parsed receipt data and flags suspicious entries."""

    # Quantity thresholds
    MAX_REASONABLE_QTY = 100
    MAX_ABSOLUTE_QTY = 999

    # Price deviation threshold: OCR price > 5× catalog price = suspicious
    MAX_PRICE_DEVIATION = 5.0

    def validate(
        self,
        items: list[dict],
        catalog: dict[str, dict] | None = None,
        historical_stats: dict | None = None,
    ) -> dict:
        """Run all validation rules on parsed receipt items.

        Args:
            items: Parsed receipt items.
            catalog: Full product catalog {code: {name, unit_price, ...}}.
            historical_stats: Past receipt statistics for anomaly detection.

        Returns:
            Dict with warnings, corrections applied, and overall status.
        """
        warnings: list[dict] = []
        corrections: list[dict] = []

        # ── Rule 1: Impossible Quantity Detection ────────────────────────
        for item in items:
            qty = item.get("quantity", 0)
            code = item.get("code", "")

            if qty <= 0:
                warnings.append({
                    "rule": "zero_quantity",
                    "severity": "high",
                    "item_code": code,
                    "message": f"Item '{code}' has zero/negative quantity ({qty})",
                    "suggestion": "Set quantity to 1",
                })
                corrections.append({
                    "item_code": code,
                    "field": "quantity",
                    "old_value": qty,
                    "new_value": 1.0,
                    "reason": "zero_quantity_auto_fix",
                })
                item["quantity"] = 1.0
                item["needs_review"] = True

            elif qty > self.MAX_REASONABLE_QTY and not item.get("unit_price", 0):
                # High qty without price data is suspicious
                warnings.append({
                    "rule": "suspicious_quantity",
                    "severity": "medium",
                    "item_code": code,
                    "message": (
                        f"Item '{code}' has unusually high quantity ({qty}) "
                        f"without price data"
                    ),
                    "suggestion": "Verify quantity is correct",
                })
                item["needs_review"] = True

        # ── Rule 2: Price Sanity Checks ──────────────────────────────────
        if catalog:
            for item in items:
                code = item.get("code", "")
                unit_price = item.get("unit_price", 0)
                line_total = item.get("line_total", 0)
                qty = item.get("quantity", 0)

                if code not in catalog:
                    continue

                cat_price = catalog[code].get("unit_price", 0)

                # Auto-fill missing prices from catalog
                if unit_price == 0 and cat_price > 0:
                    item["unit_price"] = cat_price
                    item["line_total"] = round(qty * cat_price, 2)
                    item["price_source"] = "validator_auto"
                    corrections.append({
                        "item_code": code,
                        "field": "unit_price",
                        "old_value": 0,
                        "new_value": cat_price,
                        "reason": "catalog_price_auto_fill",
                    })

                # Check for extreme price deviation
                elif unit_price > 0 and cat_price > 0:
                    ratio = unit_price / cat_price
                    if ratio > self.MAX_PRICE_DEVIATION or ratio < (1 / self.MAX_PRICE_DEVIATION):
                        warnings.append({
                            "rule": "price_deviation",
                            "severity": "medium",
                            "item_code": code,
                            "message": (
                                f"Item '{code}' unit price {unit_price} deviates "
                                f"significantly from catalog {cat_price}"
                            ),
                            "ocr_price": unit_price,
                            "catalog_price": cat_price,
                        })
                        item["needs_review"] = True

                # Line total math check
                if unit_price > 0 and qty > 0 and line_total > 0:
                    expected = round(qty * unit_price, 2)
                    tolerance = max(1.0, expected * 0.01)  # 1% or 1 unit
                    if abs(line_total - expected) > tolerance:
                        warnings.append({
                            "rule": "line_total_mismatch",
                            "severity": "low",
                            "item_code": code,
                            "message": (
                                f"Item '{code}': qty({qty}) × price({unit_price}) = "
                                f"{expected}, but line total = {line_total}"
                            ),
                            "expected": expected,
                            "actual": line_total,
                        })

        # ── Rule 3: Duplicate Item Flagging ──────────────────────────────
        code_counts: dict[str, int] = {}
        for item in items:
            code = item.get("code", "")
            if code:
                code_counts[code] = code_counts.get(code, 0) + 1

        duplicates = {c: n for c, n in code_counts.items() if n > 1}
        if duplicates:
            for code, count in duplicates.items():
                warnings.append({
                    "rule": "duplicate_item",
                    "severity": "low",
                    "item_code": code,
                    "message": f"Item '{code}' appears {count} times in this receipt",
                    "suggestion": "Items may need consolidation",
                })

        # ── Rule 4: Cross-Receipt Anomaly Detection ──────────────────────
        if historical_stats:
            for item in items:
                code = item.get("code", "")
                qty = item.get("quantity", 0)

                if code in historical_stats:
                    stats = historical_stats[code]
                    avg_qty = stats.get("avg_quantity", 0)
                    max_qty = stats.get("max_quantity", 0)

                    # Qty is 3× higher than historical max → likely OCR misread
                    if avg_qty > 0 and max_qty > 0 and qty > max_qty * 3:
                        warnings.append({
                            "rule": "quantity_anomaly",
                            "severity": "medium",
                            "item_code": code,
                            "message": (
                                f"Item '{code}' qty={qty} is 3× higher than "
                                f"historical max ({max_qty})"
                            ),
                            "historical_avg": avg_qty,
                            "historical_max": max_qty,
                        })
                        item["needs_review"] = True

        # ── Summary ──────────────────────────────────────────────────────
        high = sum(1 for w in warnings if w["severity"] == "high")
        medium = sum(1 for w in warnings if w["severity"] == "medium")

        return {
            "valid": high == 0,
            "warnings": warnings,
            "corrections": corrections,
            "summary": {
                "total_warnings": len(warnings),
                "high": high,
                "medium": medium,
                "low": len(warnings) - high - medium,
                "auto_corrections": len(corrections),
            },
        }

    def get_historical_stats(self, db_instance) -> dict:
        """Get historical quantity statistics per product code.

        Returns dict: {code: {avg_quantity, max_quantity, count}}
        """
        try:
            return db_instance.get_item_quantity_stats()
        except Exception as e:
            logger.debug(f"Historical stats unavailable: {e}")
            return {}


# Singleton
receipt_validator = ReceiptValidator()
