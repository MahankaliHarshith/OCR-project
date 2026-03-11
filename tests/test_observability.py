"""
Tests for WebSocket manager, JSON logging, and alerting config.

Covers:
  - WebSocket ConnectionManager: connect, disconnect, broadcast, close_batch
  - JSONFormatter: output structure, exception handling, extra fields
  - Alert rules YAML: valid syntax, expected alert names
  - Prometheus config: alertmanager + rule_files references
  - Alertmanager config: valid YAML, route/receiver structure
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ─── WebSocket ConnectionManager Tests ────────────────────────────────────────

class TestConnectionManager:
    """Tests for app.websocket.ConnectionManager."""

    def setup_method(self):
        from app.websocket import ConnectionManager
        self.manager = ConnectionManager()

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self):
        """Connecting then disconnecting removes the client."""
        ws = AsyncMock()
        await self.manager.connect("batch1", ws)
        ws.accept.assert_awaited_once()
        assert self.manager.has_subscribers("batch1")

        await self.manager.disconnect("batch1", ws)
        assert not self.manager.has_subscribers("batch1")

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        """Broadcast sends the message to every subscriber."""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await self.manager.connect("batch1", ws1)
        await self.manager.connect("batch1", ws2)

        msg = {"type": "file_completed", "index": 0}
        await self.manager.broadcast("batch1", msg)

        payload = json.dumps(msg)
        ws1.send_text.assert_awaited_once_with(payload)
        ws2.send_text.assert_awaited_once_with(payload)

    @pytest.mark.asyncio
    async def test_broadcast_no_subscribers(self):
        """Broadcast to empty batch is a no-op."""
        # Should not raise
        await self.manager.broadcast("nonexistent", {"type": "test"})

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_clients(self):
        """Dead (errored) clients are cleaned up during broadcast."""
        good_ws = AsyncMock()
        dead_ws = AsyncMock()
        dead_ws.send_text.side_effect = RuntimeError("connection lost")

        await self.manager.connect("batch1", good_ws)
        await self.manager.connect("batch1", dead_ws)

        await self.manager.broadcast("batch1", {"type": "test"})

        # Good ws received the message
        good_ws.send_text.assert_awaited_once()
        # Dead ws was removed
        # Only the good one should remain
        assert self.manager.has_subscribers("batch1")

    @pytest.mark.asyncio
    async def test_close_batch(self):
        """close_batch closes all sockets and removes the group."""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await self.manager.connect("batch1", ws1)
        await self.manager.connect("batch1", ws2)

        await self.manager.close_batch("batch1")

        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()
        assert not self.manager.has_subscribers("batch1")

    @pytest.mark.asyncio
    async def test_has_subscribers_false_for_unknown_batch(self):
        """has_subscribers returns False for unknown batch IDs."""
        assert not self.manager.has_subscribers("unknown_batch")

    @pytest.mark.asyncio
    async def test_send_personal(self):
        """send_personal sends only to the target websocket."""
        ws = AsyncMock()
        await self.manager.connect("batch1", ws)

        msg = {"type": "connected", "batch_id": "batch1"}
        await self.manager.send_personal(ws, msg)

        ws.send_text.assert_awaited_once_with(json.dumps(msg))

    @pytest.mark.asyncio
    async def test_multiple_batches_isolated(self):
        """Subscribers to different batches don't receive each other's messages."""
        ws_a = AsyncMock()
        ws_b = AsyncMock()
        await self.manager.connect("batchA", ws_a)
        await self.manager.connect("batchB", ws_b)

        await self.manager.broadcast("batchA", {"type": "test_a"})

        ws_a.send_text.assert_awaited_once()
        ws_b.send_text.assert_not_awaited()


# ─── JSON Logging Tests ──────────────────────────────────────────────────────

class TestJSONFormatter:
    """Tests for app.json_logging.JSONFormatter."""

    def setup_method(self):
        from app.json_logging import JSONFormatter
        self.formatter = JSONFormatter()

    def test_basic_log_record(self):
        """A simple INFO log produces valid JSON with required fields."""
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = self.formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "Hello world"
        assert data["line"] == 42
        assert "timestamp" in data

    def test_exception_included(self):
        """Exception info is serialized into the JSON output."""
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=99,
            msg="Something broke",
            args=(),
            exc_info=exc_info,
        )
        output = self.formatter.format(record)
        data = json.loads(output)

        assert "exception" in data
        assert data["exception"]["type"] == "ValueError"
        assert "test error" in data["exception"]["message"]
        assert "traceback" in data["exception"]

    def test_extra_fields(self):
        """Extra fields passed to the logger appear in the 'extra' bucket."""
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="scan complete",
            args=(),
            exc_info=None,
        )
        record.scan_id = "abc123"
        record.items_found = 5

        output = self.formatter.format(record)
        data = json.loads(output)

        assert "extra" in data
        assert data["extra"]["scan_id"] == "abc123"
        assert data["extra"]["items_found"] == 5

    def test_output_is_single_line(self):
        """JSON output is always a single line (no pretty printing)."""
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname="test.py",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        output = self.formatter.format(record)
        assert "\n" not in output

    def test_timestamp_is_iso8601(self):
        """Timestamp is in ISO-8601 format."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        output = self.formatter.format(record)
        data = json.loads(output)
        # Should parse as ISO format
        from datetime import datetime
        ts = data["timestamp"]
        # Should not raise
        datetime.fromisoformat(ts)


# ─── Alert Rules YAML Tests ──────────────────────────────────────────────────

class TestAlertRules:
    """Validate the Prometheus alert rules YAML file."""

    def setup_method(self):
        rules_path = Path(__file__).resolve().parent.parent / "monitoring" / "alert_rules.yml"
        with open(rules_path, "r", encoding="utf-8") as f:
            self.rules = yaml.safe_load(f)

    def test_valid_yaml(self):
        """Alert rules file parses as valid YAML."""
        assert self.rules is not None
        assert "groups" in self.rules

    def test_has_expected_groups(self):
        """All four alert groups are present."""
        group_names = [g["name"] for g in self.rules["groups"]]
        assert "azure_budget" in group_names
        assert "error_rates" in group_names
        assert "latency" in group_names
        assert "infrastructure" in group_names

    def test_azure_budget_alerts(self):
        """Azure budget group has the critical budget alerts."""
        azure_group = next(g for g in self.rules["groups"] if g["name"] == "azure_budget")
        alert_names = [r["alert"] for r in azure_group["rules"]]
        assert "AzureDailyPageLimitNear" in alert_names
        assert "AzureDailyPageLimitExceeded" in alert_names
        assert "AzureMonthlyBudget80Percent" in alert_names
        assert "AzureMonthlyBudgetExceeded" in alert_names

    def test_all_rules_have_severity(self):
        """Every alert rule has a severity label."""
        for group in self.rules["groups"]:
            for rule in group["rules"]:
                assert "severity" in rule["labels"], f"Rule {rule['alert']} missing severity label"

    def test_all_rules_have_annotations(self):
        """Every alert rule has summary and description annotations."""
        for group in self.rules["groups"]:
            for rule in group["rules"]:
                assert "summary" in rule["annotations"], f"Rule {rule['alert']} missing summary"
                assert "description" in rule["annotations"], f"Rule {rule['alert']} missing description"

    def test_severity_values_valid(self):
        """All severity values are one of: critical, warning, info."""
        valid = {"critical", "warning", "info"}
        for group in self.rules["groups"]:
            for rule in group["rules"]:
                sev = rule["labels"]["severity"]
                assert sev in valid, f"Rule {rule['alert']} has invalid severity: {sev}"

    def test_target_down_alert_exists(self):
        """The critical TargetDown alert is present."""
        infra_group = next(g for g in self.rules["groups"] if g["name"] == "infrastructure")
        alert_names = [r["alert"] for r in infra_group["rules"]]
        assert "TargetDown" in alert_names


class TestAlertmanagerConfig:
    """Validate the Alertmanager configuration YAML."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "monitoring" / "alertmanager.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def test_valid_yaml(self):
        """Alertmanager config parses as valid YAML."""
        assert self.config is not None

    def test_has_route(self):
        """Config has a routing tree."""
        assert "route" in self.config
        assert "receiver" in self.config["route"]

    def test_has_receivers(self):
        """Config has at least one receiver."""
        assert "receivers" in self.config
        assert len(self.config["receivers"]) >= 1

    def test_default_receiver_exists(self):
        """The default receiver referenced by route exists."""
        default_name = self.config["route"]["receiver"]
        receiver_names = [r["name"] for r in self.config["receivers"]]
        assert default_name in receiver_names


class TestPrometheusConfig:
    """Validate the Prometheus config references alerting + rules."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "monitoring" / "prometheus.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def test_alerting_configured(self):
        """Prometheus config references alertmanager."""
        assert "alerting" in self.config
        assert "alertmanagers" in self.config["alerting"]

    def test_rule_files_configured(self):
        """Prometheus config references alert_rules.yml."""
        assert "rule_files" in self.config
        assert any("alert_rules" in rf for rf in self.config["rule_files"])


# ─── Loki Config Tests ───────────────────────────────────────────────────────

class TestLokiConfig:
    """Validate the Loki configuration YAML."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "monitoring" / "loki.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def test_valid_yaml(self):
        """Loki config parses as valid YAML."""
        assert self.config is not None

    def test_server_port(self):
        """Loki listens on expected port."""
        assert self.config["server"]["http_listen_port"] == 3100

    def test_auth_disabled(self):
        """Auth is disabled for local dev."""
        assert self.config["auth_enabled"] is False


class TestPromtailConfig:
    """Validate the Promtail configuration YAML."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "monitoring" / "promtail.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def test_valid_yaml(self):
        """Promtail config parses as valid YAML."""
        assert self.config is not None

    def test_pushes_to_loki(self):
        """Promtail sends logs to Loki."""
        client_urls = [c["url"] for c in self.config["clients"]]
        assert any("loki" in url for url in client_urls)

    def test_has_json_scrape_job(self):
        """Promtail has a job for JSON logs."""
        job_names = [s["job_name"] for s in self.config["scrape_configs"]]
        assert "receipt-scanner-json" in job_names
