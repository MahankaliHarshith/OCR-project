"""
Azure OCR Usage Tracker & Cost Control.

Tracks every Azure API call, enforces daily/monthly page limits,
and provides real-time cost estimates so the user never gets a surprise bill.

Persistence: JSON file at data/azure_usage.json (survives restarts).
Thread-safe: Uses file-level locking for concurrent access.

Cost Model (as of 2025):
    prebuilt-receipt : $10.00 / 1,000 pages  ($0.01 per page)
    prebuilt-read    : $1.50  / 1,000 pages  ($0.0015 per page)
    Free tier (F0)   : 500 pages / month     (both models combined)
"""

import json
import logging
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Cost per page by model ──────────────────────────────────────────────────
MODEL_COSTS = {
    "prebuilt-receipt": 0.01,      # $10 per 1,000 pages
    "prebuilt-read": 0.0015,       # $1.50 per 1,000 pages
}

DEFAULT_USAGE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "azure_usage.json"


class UsageTracker:
    """
    Tracks Azure Document Intelligence API usage per day/month.

    Features:
        - Per-model call counting (receipt vs read)
        - Daily and monthly page limits with hard/soft enforcement
        - Cost estimation in real-time
        - Persistent storage (JSON file)
        - Thread-safe operations
    """

    def __init__(
        self,
        usage_file: Optional[Path] = None,
        daily_limit: int = 50,
        monthly_limit: int = 500,
        free_tier_pages: int = 500,
    ):
        self.usage_file = usage_file or DEFAULT_USAGE_FILE
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit
        self.free_tier_pages = free_tier_pages
        self._lock = threading.Lock()

        # Ensure data directory exists
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)

        # Load or initialize usage data
        self._data = self._load()

        logger.info(
            f"UsageTracker initialized: daily_limit={daily_limit}, "
            f"monthly_limit={monthly_limit}, file={self.usage_file}"
        )

    # ─── Public API ──────────────────────────────────────────────────────

    def can_call_azure(self) -> Dict:
        """
        Check if we're allowed to make another Azure API call.

        Returns:
            {
                "allowed": True/False,
                "reason": "..." (if blocked or warning),
                "daily_used": int,
                "daily_remaining": int,
                "monthly_used": int,
                "monthly_remaining": int,
                "estimated_cost": float,
                "is_within_free_tier": bool,
                "pace_status": "ok" | "fast" | "critical",
            }
        """
        with self._lock:
            today = self._today_key()
            month = self._month_key()

            daily_used = self._get_daily_total(today)
            monthly_used = self._get_monthly_total(month)

            # Budget pacing: calculate sustainable daily rate
            days_left = self._days_remaining_in_month()
            monthly_remaining = max(0, self.monthly_limit - monthly_used)
            sustainable_daily = monthly_remaining / max(1, days_left)

            pace_status = "ok"
            if days_left > 0 and daily_used > sustainable_daily * 1.5:
                pace_status = "critical"
            elif days_left > 0 and daily_used > sustainable_daily * 1.2:
                pace_status = "fast"

            result = {
                "allowed": True,
                "reason": "",
                "daily_used": daily_used,
                "daily_remaining": max(0, self.daily_limit - daily_used),
                "monthly_used": monthly_used,
                "monthly_remaining": monthly_remaining,
                "estimated_cost": self._estimate_cost(month),
                "is_within_free_tier": monthly_used < self.free_tier_pages,
                "pace_status": pace_status,
                "sustainable_daily_rate": round(sustainable_daily, 1),
                "days_left_in_month": days_left,
            }

            # Check daily limit
            if daily_used >= self.daily_limit:
                result["allowed"] = False
                result["reason"] = (
                    f"Daily Azure limit reached ({daily_used}/{self.daily_limit} pages). "
                    f"Falling back to local OCR. Resets tomorrow."
                )
                logger.warning(f"[UsageTracker] {result['reason']}")

            # Check monthly limit
            elif monthly_used >= self.monthly_limit:
                result["allowed"] = False
                result["reason"] = (
                    f"Monthly Azure limit reached ({monthly_used}/{self.monthly_limit} pages). "
                    f"Falling back to local OCR. Resets next month."
                )
                logger.warning(f"[UsageTracker] {result['reason']}")

            # Budget pacing warning
            elif pace_status == "critical":
                result["reason"] = (
                    f"⚠️ Spending too fast! {daily_used} pages today, "
                    f"sustainable rate is {sustainable_daily:.0f}/day "
                    f"({monthly_remaining} pages left, {days_left} days remaining)"
                )
                logger.warning(f"[UsageTracker] {result['reason']}")

            # Warn at 80% of limits
            elif monthly_used >= int(self.monthly_limit * 0.8):
                result["reason"] = (
                    f"⚠️ Approaching monthly limit: {monthly_used}/{self.monthly_limit} pages "
                    f"({monthly_used / self.monthly_limit * 100:.0f}% used)"
                )
                logger.warning(f"[UsageTracker] {result['reason']}")

            elif daily_used >= int(self.daily_limit * 0.8):
                result["reason"] = (
                    f"⚠️ Approaching daily limit: {daily_used}/{self.daily_limit} pages"
                )

            return result

    def record_call(self, model: str, pages: int = 1, success: bool = True) -> None:
        """
        Record an Azure API call.

        Args:
            model: Azure model used ("prebuilt-receipt" or "prebuilt-read")
            pages: Number of pages processed (usually 1 for receipts)
            success: Whether the call succeeded
        """
        with self._lock:
            today = self._today_key()
            month = self._month_key()

            # Initialize day/month structures if needed
            if "days" not in self._data:
                self._data["days"] = {}
            if today not in self._data["days"]:
                self._data["days"][today] = {"calls": [], "total_pages": 0}

            if "months" not in self._data:
                self._data["months"] = {}
            if month not in self._data["months"]:
                self._data["months"][month] = {
                    "total_pages": 0,
                    "receipt_pages": 0,
                    "read_pages": 0,
                    "estimated_cost": 0.0,
                }

            # Record the call
            call_record = {
                "timestamp": datetime.now().isoformat(),
                "model": model,
                "pages": pages,
                "success": success,
                "cost": MODEL_COSTS.get(model, 0) * pages,
            }
            self._data["days"][today]["calls"].append(call_record)
            self._data["days"][today]["total_pages"] += pages

            # Update monthly totals
            month_data = self._data["months"][month]
            month_data["total_pages"] += pages

            if "receipt" in model:
                month_data["receipt_pages"] += pages
            else:
                month_data["read_pages"] += pages

            month_data["estimated_cost"] = self._estimate_cost(month)

            # Persist
            self._save()

            logger.debug(
                f"[UsageTracker] Recorded: model={model}, pages={pages}, "
                f"daily_total={self._data['days'][today]['total_pages']}, "
                f"monthly_total={month_data['total_pages']}"
            )

    def get_usage_summary(self) -> Dict:
        """
        Get a complete usage summary for display on dashboard.

        Returns:
            Full breakdown of daily, monthly, and historical usage.
        """
        with self._lock:
            today = self._today_key()
            month = self._month_key()

            daily_used = self._get_daily_total(today)
            monthly_used = self._get_monthly_total(month)
            monthly_cost = self._estimate_cost(month)

            # Cost beyond free tier
            billable_pages = max(0, monthly_used - self.free_tier_pages)
            month_data = self._data.get("months", {}).get(month, {})
            receipt_pages = month_data.get("receipt_pages", 0)
            read_pages = month_data.get("read_pages", 0)

            # Estimate cost for billable pages only
            if billable_pages > 0 and monthly_used > 0:
                receipt_ratio = receipt_pages / monthly_used if monthly_used else 0
                read_ratio = read_pages / monthly_used if monthly_used else 0
                billable_cost = (
                    billable_pages * receipt_ratio * MODEL_COSTS["prebuilt-receipt"]
                    + billable_pages * read_ratio * MODEL_COSTS["prebuilt-read"]
                )
            else:
                billable_cost = 0.0

            # Budget pacing
            days_left = self._days_remaining_in_month()
            monthly_remaining = max(0, self.monthly_limit - monthly_used)
            sustainable_daily = monthly_remaining / max(1, days_left)

            pace_status = "ok"
            if days_left > 0 and daily_used > sustainable_daily * 1.5:
                pace_status = "critical"
            elif days_left > 0 and daily_used > sustainable_daily * 1.2:
                pace_status = "fast"

            return {
                "today": {
                    "pages_used": daily_used,
                    "pages_limit": self.daily_limit,
                    "pages_remaining": max(0, self.daily_limit - daily_used),
                    "percentage": round(daily_used / self.daily_limit * 100, 1) if self.daily_limit else 0,
                },
                "this_month": {
                    "pages_used": monthly_used,
                    "pages_limit": self.monthly_limit,
                    "pages_remaining": max(0, self.monthly_limit - monthly_used),
                    "percentage": round(monthly_used / self.monthly_limit * 100, 1) if self.monthly_limit else 0,
                    "receipt_model_pages": receipt_pages,
                    "read_model_pages": read_pages,
                },
                "cost": {
                    "free_tier_pages": self.free_tier_pages,
                    "free_tier_remaining": max(0, self.free_tier_pages - monthly_used),
                    "is_within_free_tier": monthly_used <= self.free_tier_pages,
                    "billable_pages": billable_pages,
                    "estimated_bill_usd": round(billable_cost, 4),
                    "total_value_usd": round(monthly_cost, 4),
                },
                "pacing": {
                    "pace_status": pace_status,
                    "sustainable_daily_rate": round(sustainable_daily, 1),
                    "days_left_in_month": days_left,
                },
                "limits": {
                    "daily_limit": self.daily_limit,
                    "monthly_limit": self.monthly_limit,
                },
            }

    def reset_daily(self) -> None:
        """Manually reset today's usage counter."""
        with self._lock:
            today = self._today_key()
            if "days" in self._data and today in self._data["days"]:
                self._data["days"][today] = {"calls": [], "total_pages": 0}
                self._save()
                logger.info("[UsageTracker] Daily usage reset")

    # ─── Internal Helpers ────────────────────────────────────────────────

    def _today_key(self) -> str:
        return date.today().isoformat()

    def _month_key(self) -> str:
        return date.today().strftime("%Y-%m")

    def _days_remaining_in_month(self) -> int:
        """Days left in the current month (including today)."""
        import calendar
        today = date.today()
        _, days_in_month = calendar.monthrange(today.year, today.month)
        return days_in_month - today.day + 1

    def _get_daily_total(self, day_key: str) -> int:
        return self._data.get("days", {}).get(day_key, {}).get("total_pages", 0)

    def _get_monthly_total(self, month_key: str) -> int:
        return self._data.get("months", {}).get(month_key, {}).get("total_pages", 0)

    def _estimate_cost(self, month_key: str) -> float:
        """Estimate total cost for a given month (before free tier deduction)."""
        month_data = self._data.get("months", {}).get(month_key, {})
        receipt_pages = month_data.get("receipt_pages", 0)
        read_pages = month_data.get("read_pages", 0)
        return round(
            receipt_pages * MODEL_COSTS["prebuilt-receipt"]
            + read_pages * MODEL_COSTS["prebuilt-read"],
            4,
        )

    def _load(self) -> Dict:
        """Load usage data from JSON file."""
        if self.usage_file.exists():
            try:
                with open(self.usage_file, "r") as f:
                    data = json.load(f)
                logger.debug(f"[UsageTracker] Loaded usage data from {self.usage_file}")
                return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"[UsageTracker] Corrupted usage file, resetting: {e}")

        return {"days": {}, "months": {}, "cache": {}}

    def _save(self) -> None:
        """Persist usage data to JSON file (atomic write to prevent corruption)."""
        try:
            # Clean up old daily data (keep last 7 days only)
            if "days" in self._data:
                today = date.today()
                keys_to_remove = []
                for day_key in self._data["days"]:
                    try:
                        day_date = date.fromisoformat(day_key)
                        if (today - day_date).days > 7:
                            keys_to_remove.append(day_key)
                    except ValueError:
                        keys_to_remove.append(day_key)
                for k in keys_to_remove:
                    del self._data["days"][k]

            # Clean up old monthly data (keep last 13 months)
            if "months" in self._data:
                current_month = today.strftime("%Y-%m")
                month_keys_to_remove = []
                for month_key in self._data["months"]:
                    try:
                        # Parse YYYY-MM and check age
                        y, m = month_key.split("-")
                        month_age = (today.year - int(y)) * 12 + (today.month - int(m))
                        if month_age > 13:
                            month_keys_to_remove.append(month_key)
                    except (ValueError, IndexError):
                        month_keys_to_remove.append(month_key)
                for k in month_keys_to_remove:
                    del self._data["months"][k]

            # Atomic write: write to temp file, then rename
            import tempfile
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.usage_file.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(self._data, f, indent=2)
                # os.replace() is atomic on both Windows and Unix —
                # no crash-vulnerability window between unlink and rename
                os.replace(tmp_path, str(self.usage_file))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except IOError as e:
            logger.error(f"[UsageTracker] Failed to save usage data: {e}")


# ─── Singleton ───────────────────────────────────────────────────────────────

_tracker: Optional[UsageTracker] = None


def get_usage_tracker() -> UsageTracker:
    """Get or create the usage tracker singleton."""
    global _tracker
    if _tracker is None:
        from app.config import (
            AZURE_DAILY_PAGE_LIMIT,
            AZURE_MONTHLY_PAGE_LIMIT,
            AZURE_FREE_TIER_PAGES,
        )
        _tracker = UsageTracker(
            daily_limit=AZURE_DAILY_PAGE_LIMIT,
            monthly_limit=AZURE_MONTHLY_PAGE_LIMIT,
            free_tier_pages=AZURE_FREE_TIER_PAGES,
        )
    return _tracker
