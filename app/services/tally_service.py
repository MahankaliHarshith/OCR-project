"""
Tally Integration Service
Generates Tally-compatible XML for importing receipt data into
TallyPrime / Tally.ERP 9 as Purchase Vouchers.

Tally accepts XML data via its HTTP API (default port 9000) using the
ENVELOPE/HEADER/BODY/IMPORTDATA structure.

Supported export formats:
  1. Tally XML  — ready to POST to Tally's HTTP API or import via Gateway > XML
  2. Tally JSON — structured data for custom integrations
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

from app.config import EXPORT_DIR

logger = logging.getLogger(__name__)


class TallyService:
    """Generate Tally-compatible exports from receipt data."""

    # Default Tally ledger names — users can override per export
    DEFAULT_PURCHASE_LEDGER = "Purchase Account"
    DEFAULT_PARTY_LEDGER = "Cash"
    DEFAULT_VOUCHER_TYPE = "Purchase"

    def _safe_xml_text(self, text: str | None) -> str:
        """Sanitise text for XML content."""
        if not text:
            return ""
        # Strip control chars except newline/tab
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text))

    def _tally_date(self, date_str: str | None) -> str:
        """Convert YYYY-MM-DD to Tally's YYYYMMDD format."""
        if not date_str:
            return datetime.now().strftime("%Y%m%d")
        try:
            dt = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
            return dt.strftime("%Y%m%d")
        except (ValueError, TypeError):
            return datetime.now().strftime("%Y%m%d")

    def _tally_display_date(self, date_str: str | None) -> str:
        """Convert to Tally's display format: d-Mon-YYYY."""
        if not date_str:
            dt = datetime.now()
        else:
            try:
                dt = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
            except (ValueError, TypeError):
                dt = datetime.now()
        # Windows-compatible: format then strip leading zero
        return dt.strftime("%d-%b-%Y").lstrip("0")

    def generate_xml(
        self,
        receipts: list[dict],
        company_name: str = "My Company",
        purchase_ledger: str | None = None,
        party_ledger: str | None = None,
        output_path: str | None = None,
    ) -> str:
        """
        Generate a Tally-compatible XML file containing Purchase Vouchers.

        Each receipt becomes one Purchase Voucher with inventory entries for
        each line item.

        Args:
            receipts: List of receipt dicts from the database (with nested 'items').
            company_name: The Tally company name to target.
            purchase_ledger: Ledger name for purchase account (default: "Purchase Account").
            party_ledger: Ledger for the party/cash (default: "Cash").
            output_path: Custom output file path.

        Returns:
            Path to the generated XML file.
        """
        purchase_ledger = purchase_ledger or self.DEFAULT_PURCHASE_LEDGER
        party_ledger = party_ledger or self.DEFAULT_PARTY_LEDGER

        # Build XML structure
        envelope = Element("ENVELOPE")

        header = SubElement(envelope, "HEADER")
        SubElement(header, "TALLYREQUEST").text = "Import Data"

        body = SubElement(envelope, "BODY")
        import_data = SubElement(body, "IMPORTDATA")

        # Request descriptor
        req_desc = SubElement(import_data, "REQUESTDESC")
        SubElement(req_desc, "REPORTNAME").text = "Vouchers"
        static_vars = SubElement(req_desc, "STATICVARIABLES")
        SubElement(static_vars, "SVCURRENTCOMPANY").text = company_name

        # Request data — vouchers
        req_data = SubElement(import_data, "REQUESTDATA")

        for receipt in receipts:
            self._build_voucher(
                req_data, receipt, purchase_ledger, party_ledger
            )

        # Serialize to pretty XML
        raw_xml = tostring(envelope, encoding="unicode")
        dom = parseString(raw_xml)
        pretty_xml = dom.toprettyxml(indent="  ")
        
        # toprettyxml() adds its own XML declaration - remove it so we can add a clean one
        lines = pretty_xml.split("\n")
        # Remove the declaration line and any empty lines at the start
        clean_lines = [line for line in lines if line.strip() and not line.strip().startswith("<?xml")]
        
        # Add a single, clean XML declaration at the start
        final_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(clean_lines)

        # Write to file
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(EXPORT_DIR / f"Tally_Import_{timestamp}.xml")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_xml)

        logger.info(f"Tally XML generated: {output_path} ({len(receipts)} vouchers)")
        return output_path

    def _build_voucher(
        self,
        parent: Element,
        receipt: dict,
        purchase_ledger: str,
        party_ledger: str,
    ) -> None:
        """Build a single TALLYMESSAGE > VOUCHER element."""
        tallymsg = SubElement(parent, "TALLYMESSAGE", xmlns_UDF="TallyUDF")

        voucher = SubElement(
            tallymsg,
            "VOUCHER",
            REMOTEID="",
            VCHTYPE=self.DEFAULT_VOUCHER_TYPE,
            ACTION="Create",
        )

        date_str = receipt.get("scan_date") or receipt.get("receipt_date")
        tally_date = self._tally_date(date_str)

        SubElement(voucher, "DATE").text = tally_date
        SubElement(voucher, "VOUCHERTYPENAME").text = self.DEFAULT_VOUCHER_TYPE
        SubElement(voucher, "VOUCHERNUMBER").text = self._safe_xml_text(
            receipt.get("receipt_number", "")
        )
        SubElement(voucher, "REFERENCE").text = self._safe_xml_text(
            receipt.get("receipt_number", "")
        )
        SubElement(voucher, "NARRATION").text = self._safe_xml_text(
            f"Receipt #{receipt.get('receipt_number', '')} "
            f"Store: {receipt.get('store_name', 'N/A')} "
            f"Scanned: {receipt.get('scan_date', '')} {receipt.get('scan_time', '')}"
        )

        items = receipt.get("items", [])
        bill_total = receipt.get("bill_total", 0) or 0

        # If no individual item amounts, try using bill_total
        computed_total = sum(it.get("line_total", 0) or 0 for it in items)
        if computed_total == 0 and bill_total > 0:
            computed_total = bill_total

        # Party ledger entry (debit — who we're paying)
        party_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        SubElement(party_entry, "LEDGERNAME").text = party_ledger
        SubElement(party_entry, "ISDEEMEDPOSITIVE").text = "Yes"
        SubElement(party_entry, "AMOUNT").text = f"-{computed_total:.2f}"

        # Purchase ledger entry (credit — the expense account)
        purchase_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        SubElement(purchase_entry, "LEDGERNAME").text = purchase_ledger
        SubElement(purchase_entry, "ISDEEMEDPOSITIVE").text = "No"
        SubElement(purchase_entry, "AMOUNT").text = f"{computed_total:.2f}"

        # Inventory entries (one per line item)
        for item in items:
            inv = SubElement(voucher, "ALLINVENTORYENTRIES.LIST")
            stock_name = self._safe_xml_text(
                item.get("product_name") or item.get("product_code") or "Unknown Item"
            )
            SubElement(inv, "STOCKITEMNAME").text = stock_name

            qty = item.get("quantity", 0) or 0
            unit = item.get("unit", "Nos")
            rate = item.get("unit_price", 0) or 0
            amount = item.get("line_total", 0) or 0

            # If no line_total but we have qty × rate
            if amount == 0 and qty > 0 and rate > 0:
                amount = qty * rate

            SubElement(inv, "ISDEEMEDPOSITIVE").text = "Yes"
            SubElement(inv, "RATE").text = f"{rate:.2f}/{unit}"
            SubElement(inv, "AMOUNT").text = f"-{amount:.2f}"

            # Batch allocations (required by Tally)
            batch = SubElement(inv, "BATCHALLOCATIONS.LIST")
            SubElement(batch, "GODOWNNAME").text = "Main Location"
            SubElement(batch, "BATCHNAME").text = "Primary Batch"
            SubElement(batch, "AMOUNT").text = f"-{amount:.2f}"
            SubElement(batch, "ACTUALQTY").text = f"{qty} {unit}"
            SubElement(batch, "BILLEDQTY").text = f"{qty} {unit}"

    def generate_json_export(
        self, receipts: list[dict], output_path: str | None = None
    ) -> str:
        """
        Generate a structured JSON file suitable for custom Tally integrations.

        This format is easier to parse for middleware tools that convert to Tally XML
        or use TallyConnector libraries.
        """
        import json

        export_data = {
            "export_type": "tally_purchase_vouchers",
            "export_date": datetime.now().isoformat(),
            "version": "1.0",
            "voucher_count": len(receipts),
            "vouchers": [],
        }

        for receipt in receipts:
            items = receipt.get("items", [])
            bill_total = receipt.get("bill_total", 0) or 0
            computed_total = sum(it.get("line_total", 0) or 0 for it in items) or bill_total

            voucher = {
                "voucher_type": self.DEFAULT_VOUCHER_TYPE,
                "voucher_number": receipt.get("receipt_number", ""),
                "date": receipt.get("scan_date", ""),
                "party_ledger": self.DEFAULT_PARTY_LEDGER,
                "purchase_ledger": self.DEFAULT_PURCHASE_LEDGER,
                "narration": (
                    f"Receipt #{receipt.get('receipt_number', '')} "
                    f"Store: {receipt.get('store_name', 'N/A')}"
                ),
                "total_amount": round(computed_total, 2),
                "items": [
                    {
                        "stock_item": it.get("product_name") or it.get("product_code") or "Unknown",
                        "product_code": it.get("product_code", ""),
                        "quantity": it.get("quantity", 0) or 0,
                        "unit": it.get("unit", "Nos"),
                        "rate": round(it.get("unit_price", 0) or 0, 2),
                        "amount": round(it.get("line_total", 0) or 0, 2),
                    }
                    for it in items
                ],
            }
            export_data["vouchers"].append(voucher)

        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(EXPORT_DIR / f"Tally_Import_{timestamp}.json")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Tally JSON generated: {output_path}")
        return output_path


# Singleton
tally_service = TallyService()
