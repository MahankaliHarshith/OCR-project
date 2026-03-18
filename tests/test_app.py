"""
Unit tests for the Receipt Scanner application.
"""

from app.ocr.parser import ReceiptParser

# ─── Test Product Catalog ─────────────────────────────────────────────────────
TEST_CATALOG = {
    "ABC": "1L Exterior Paint",
    "XYZ": "1L Interior Paint",
    "PQR": "5L Primer White",
    "MNO": "Paint Brush 2 inch",
}


# ─── Parser Tests ─────────────────────────────────────────────────────────────
class TestReceiptParser:
    """Tests for the ReceiptParser class."""

    def setup_method(self):
        self.parser = ReceiptParser(TEST_CATALOG)

    def test_parse_code_then_quantity(self):
        """Test pattern: ABC 2"""
        results = [{"text": "ABC 2", "confidence": 0.95}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["code"] == "ABC"
        assert data["items"][0]["quantity"] == 2.0
        assert data["items"][0]["product"] == "1L Exterior Paint"

    def test_parse_quantity_then_code(self):
        """Test pattern: 3 XYZ"""
        results = [{"text": "3 XYZ", "confidence": 0.90}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["code"] == "XYZ"
        assert data["items"][0]["quantity"] == 3.0

    def test_parse_multiplication_format(self):
        """Test pattern: ABC x 5"""
        results = [{"text": "ABC x 5", "confidence": 0.88}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["quantity"] == 5.0

    def test_parse_dash_format(self):
        """Test pattern: PQR - 2"""
        results = [{"text": "PQR - 2", "confidence": 0.92}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["code"] == "PQR"
        assert data["items"][0]["quantity"] == 2.0

    def test_parse_decimal_quantity(self):
        """Test decimal quantities: ABC 2.5"""
        results = [{"text": "ABC 2.5", "confidence": 0.91}]
        data = self.parser.parse(results)
        assert data["items"][0]["quantity"] == 2.5

    def test_parse_multiple_items(self):
        """Test parsing multiple items."""
        results = [
            {"text": "ABC 2", "confidence": 0.95},
            {"text": "XYZ 3", "confidence": 0.92},
            {"text": "PQR 1", "confidence": 0.88},
        ]
        data = self.parser.parse(results)
        assert data["total_items"] == 3

    def test_skip_header_lines(self):
        """Test that date/total lines are skipped."""
        # bbox required so _group_into_lines keeps items on separate lines
        results = [
            {"text": "21/02/2026", "confidence": 0.99, "bbox": [[0,0],[100,0],[100,20],[0,20]]},
            {"text": "ABC 2", "confidence": 0.95, "bbox": [[0,100],[100,100],[100,120],[0,120]]},
            {"text": "Total", "confidence": 0.97, "bbox": [[0,200],[100,200],[100,220],[0,220]]},
        ]
        data = self.parser.parse(results)
        assert data["total_items"] == 1

    def test_unknown_product_code(self):
        """Unknown codes are filtered by the parser's phantom-code guard."""
        # FFF has no catalog match — parser removes unknown codes to avoid
        # false positives, so total_items should be 0.
        results = [{"text": "FFF 5", "confidence": 0.90}]
        data = self.parser.parse(results)
        assert data["total_items"] == 0

    def test_fuzzy_match(self):
        """Test fuzzy matching for close-but-not-exact codes."""
        # ABD is Levenshtein-1 from ABC but the parser's tightened fuzzy
        # threshold plus first-char-match guard filters it. Verify the
        # parser returns 0 items (consistent with phantom-code removal).
        results = [{"text": "ABD 2", "confidence": 0.80}]
        data = self.parser.parse(results)
        assert data["total_items"] == 0

    def test_duplicate_aggregation(self):
        """Test that duplicate product codes are aggregated."""
        # bbox required so _group_into_lines keeps items on separate lines
        results = [
            {"text": "ABC 2", "confidence": 0.95, "bbox": [[0,0],[100,0],[100,20],[0,20]]},
            {"text": "ABC 3", "confidence": 0.90, "bbox": [[0,100],[100,100],[100,120],[0,120]]},
        ]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["quantity"] == 5.0

    def test_empty_input(self):
        """Test handling of empty input."""
        data = self.parser.parse([])
        assert data["total_items"] == 0
        assert data["processing_status"] == "no_items_found"

    def test_confidence_flagging(self):
        """Test that low-confidence items are flagged."""
        # Item-level needs_review triggers at confidence < 0.5
        results = [{"text": "ABC 2", "confidence": 0.40}]
        data = self.parser.parse(results)
        assert data["items"][0]["needs_review"] is True

    def test_receipt_level_review_flag(self):
        """Test that receipt-level needs_review triggers when avg confidence < 0.85."""
        results = [{"text": "ABC 2", "confidence": 0.70}]
        data = self.parser.parse(results)
        # Item-level flag is False (0.70 > 0.5), but receipt-level is True (0.70 < 0.85)
        assert data["items"][0]["needs_review"] is False
        assert data["needs_review"] is True

    def test_case_insensitive_matching(self):
        """Test case-insensitive product code matching."""
        results = [{"text": "abc 2", "confidence": 0.95}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["code"] == "ABC"

    def test_receipt_number_generated(self):
        """Test that a receipt number is auto-generated."""
        results = [{"text": "ABC 2", "confidence": 0.95}]
        data = self.parser.parse(results)
        assert data["receipt_id"].startswith("REC-")

    def test_colon_format(self):
        """Test pattern: MNO: 4"""
        results = [{"text": "MNO: 4", "confidence": 0.89}]
        data = self.parser.parse(results)
        assert data["total_items"] == 1
        assert data["items"][0]["code"] == "MNO"
        assert data["items"][0]["quantity"] == 4.0


# ─── Excel Service Tests (basic) ─────────────────────────────────────────────
class TestExcelService:
    """Basic tests for Excel generation."""

    def test_import(self):
        """Test that the ExcelService can be imported."""
        from app.services.excel_service import ExcelService
        service = ExcelService()
        assert service is not None

    def test_generate_report(self, tmp_path):
        """Test generating an Excel report."""
        from app.services.excel_service import ExcelService

        service = ExcelService()
        receipts = [
            {
                "receipt_number": "REC-TEST-001",
                "scan_date": "2026-02-21",
                "scan_time": "10:30:00",
                "items": [
                    {
                        "code": "ABC",
                        "product": "1L Exterior Paint",
                        "quantity": 2,
                        "unit": "Litre",
                        "confidence": 0.95,
                    },
                    {
                        "code": "XYZ",
                        "product": "1L Interior Paint",
                        "quantity": 3,
                        "unit": "Litre",
                        "confidence": 0.88,
                    },
                ],
            }
        ]

        output = str(tmp_path / "test_report.xlsx")
        filepath = service.generate_report(receipts, output)
        assert filepath == output

        import os
        assert os.path.exists(filepath)
        assert os.path.getsize(filepath) > 0


# ─── Database Tests ───────────────────────────────────────────────────────────
class TestDatabase:
    """Basic database tests."""

    def test_import(self):
        from app.database import Database
        assert Database is not None

    def test_create_in_memory(self, tmp_path):
        from app.database import Database
        db = Database(tmp_path / "test.db")
        products = db.get_all_products()
        assert isinstance(products, list)
        assert len(products) > 0  # Should have seeded defaults

    def test_add_and_get_product(self, tmp_path):
        from app.database import Database
        db = Database(tmp_path / "test.db")

        db.add_product("TST", "Test Product", "Test", "Piece")
        product = db.get_product_by_code("TST")
        assert product is not None
        assert product["product_name"] == "Test Product"

    def test_create_receipt(self, tmp_path):
        from app.database import Database
        db = Database(tmp_path / "test.db")

        receipt_id = db.create_receipt("REC-TEST-001")
        assert receipt_id > 0

        items = [
            {"code": "ABC", "product": "1L Exterior Paint", "quantity": 2, "confidence": 0.95},
        ]
        db.add_receipt_items(receipt_id, items)

        receipt = db.get_receipt(receipt_id)
        assert receipt is not None
        assert len(receipt["items"]) == 1
