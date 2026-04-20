"""Unit tests for helio.ibkr_executor — the broker execution helper.

This is the code that will place real orders when a family flips to live.
Today nothing in the fleet is flipped (MODE="research_only" across Apollo;
paper-only across argus_flow). But before any live flip, the execution
path must be pinned — a silent rename or direction flip here is an
immediate loss.

Tests cover the pure logic: CLIENT_IDS uniqueness, order logging, direction
normalization, account-value fallback, get_positions shape. IBKR network
connectivity is fully mocked (ib_insync.IB is replaced with a MagicMock).
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _mk_executor(tmp_repo: Path, system: str = "titan", client_id: int = 60):
    """Build an executor with IB fully mocked."""
    from helio import ibkr_executor as ex
    with mock.patch.object(ex, "REPO", tmp_repo), \
         mock.patch.object(ex, "IB") as mock_ib_cls:
        mock_ib_cls.return_value = mock.MagicMock()
        inst = ex.IBKRExecutor(client_id=client_id, system=system)
        return inst, mock_ib_cls


class TestClientIDsAllocation(unittest.TestCase):
    """CLIENT_IDS table allocates a unique port per system. Collisions
    would make two runners fight over the same IBKR session."""

    def test_each_system_has_unique_client_id(self):
        from helio.ibkr_executor import CLIENT_IDS
        ids = list(CLIENT_IDS.values())
        self.assertEqual(len(ids), len(set(ids)),
            f"duplicate client_ids in CLIENT_IDS: {CLIENT_IDS}")

    def test_client_ids_are_positive_ints(self):
        from helio.ibkr_executor import CLIENT_IDS
        for name, cid in CLIENT_IDS.items():
            self.assertIsInstance(cid, int)
            self.assertGreater(cid, 0, f"{name} has non-positive client_id {cid}")


class TestConnectDisconnect(unittest.TestCase):
    def test_connect_returns_true_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.connect = mock.MagicMock(return_value=None)
            self.assertTrue(inst.connect())

    def test_connect_returns_false_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.connect = mock.MagicMock(side_effect=ConnectionError("refused"))
            self.assertFalse(inst.connect())

    def test_disconnect_tolerates_already_disconnected(self):
        """disconnect() must never raise — gets called in finally blocks."""
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.isConnected = mock.MagicMock(side_effect=RuntimeError("boom"))
            # Must not raise
            inst.disconnect()

    def test_is_connected_returns_false_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.isConnected = mock.MagicMock(side_effect=RuntimeError("boom"))
            self.assertFalse(inst.is_connected())


class TestOrderLogging(unittest.TestCase):
    """_log_order must append to orders.csv in the system's logs dir."""

    def test_log_order_creates_header_on_first_write(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._log_order(
                action="ENTRY", symbol="NVDA", direction="long",
                quantity=10, price=180.5, stop=170.0, target=200.0,
                order_id="12345",
            )
            with open(inst.orders_log, encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0][:2], ["ts", "action"])  # header
        self.assertEqual(rows[1][1], "ENTRY")
        self.assertEqual(rows[1][2], "NVDA")

    def test_log_order_appends_to_existing_csv(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            # Pre-populate
            inst._log_order(action="ENTRY", symbol="A", direction="long",
                            quantity=1, price=1.0, stop=0.5, target=1.5, order_id="1")
            inst._log_order(action="ENTRY", symbol="B", direction="short",
                            quantity=2, price=2.0, stop=2.5, target=1.5, order_id="2")
            with open(inst.orders_log, encoding="utf-8") as f:
                rows = list(csv.reader(f))
        # header + 2 data rows
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2][2], "B")


class TestSubmitMarket(unittest.TestCase):
    """submit_market: direction maps to IBKR BUY/SELL and logs the order."""

    def test_long_direction_maps_to_buy(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "Stock") as mock_stock, \
             mock.patch.object(ex, "MarketOrder") as mock_mo:
            inst, _ = _mk_executor(Path(td))
            fake_trade = mock.MagicMock()
            fake_trade.order.orderId = 12345
            inst._ib.placeOrder = mock.MagicMock(return_value=fake_trade)
            order_id = inst.submit_market("NVDA", "long", 10)
        # MarketOrder called with "BUY" for long
        args, kwargs = mock_mo.call_args
        self.assertEqual(args[0], "BUY")
        self.assertEqual(args[1], 10)
        self.assertEqual(order_id, "12345")

    def test_short_direction_maps_to_sell(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "Stock"), \
             mock.patch.object(ex, "MarketOrder") as mock_mo:
            inst, _ = _mk_executor(Path(td))
            fake_trade = mock.MagicMock()
            fake_trade.order.orderId = 99
            inst._ib.placeOrder = mock.MagicMock(return_value=fake_trade)
            inst.submit_market("NVDA", "short", 5)
        args, _ = mock_mo.call_args
        self.assertEqual(args[0], "SELL")

    def test_failure_returns_none(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "Stock") as mock_stock:
            inst, _ = _mk_executor(Path(td))
            mock_stock.side_effect = RuntimeError("IBKR unreachable")
            result = inst.submit_market("NVDA", "long", 10)
        self.assertIsNone(result)


class TestSubmitBracket(unittest.TestCase):
    """Bracket orders: parent + stop + target placed as a triad."""

    def test_bracket_submits_three_orders(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "Stock"):
            inst, _ = _mk_executor(Path(td))
            # Mock bracketOrder to return three orders
            parent = mock.MagicMock()
            parent.orderId = 1001
            stop_o = mock.MagicMock()
            target_o = mock.MagicMock()
            fake_bracket = [parent, stop_o, target_o]
            fake_bracket_obj = mock.MagicMock()
            fake_bracket_obj.parent = parent
            fake_bracket_obj.__iter__ = lambda s: iter([parent, stop_o, target_o])
            inst._ib.bracketOrder = mock.MagicMock(return_value=fake_bracket_obj)

            order_id = inst.submit_bracket(
                symbol="NVDA", direction="long", quantity=10,
                entry_price=180.0, stop_price=170.0, target_price=200.0,
            )
        self.assertEqual(order_id, "1001")
        # placeOrder called 3 times (parent + stop + target)
        self.assertEqual(inst._ib.placeOrder.call_count, 3)


class TestGetAccountValue(unittest.TestCase):
    """get_account_value returns NetLiquidation USD, or falls back to model equity."""

    def test_returns_net_liquidation_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            fake_value = mock.MagicMock()
            fake_value.tag = "NetLiquidation"
            fake_value.currency = "USD"
            fake_value.value = "12345.67"
            inst._ib.accountValues = mock.MagicMock(return_value=[fake_value])
            val = inst.get_account_value()
        self.assertAlmostEqual(val, 12345.67)

    def test_falls_back_to_model_equity_when_api_empty(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.accountValues = mock.MagicMock(return_value=[])
            val = inst.get_account_value()
        self.assertEqual(val, inst.model_equity_usd)

    def test_falls_back_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.accountValues = mock.MagicMock(side_effect=RuntimeError("no conn"))
            val = inst.get_account_value()
        self.assertEqual(val, inst.model_equity_usd)


class TestGetPositions(unittest.TestCase):
    def test_maps_ib_positions_to_dicts(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            pos1 = mock.MagicMock()
            pos1.contract.symbol = "NVDA"
            pos1.position = 10
            pos1.avgCost = 180.5
            pos2 = mock.MagicMock()
            pos2.contract.symbol = "MSFT"
            pos2.position = -5
            pos2.avgCost = 420.0
            inst._ib.positions = mock.MagicMock(return_value=[pos1, pos2])
            result = inst.get_positions()
        self.assertEqual(len(result), 2)
        by_sym = {p["symbol"]: p for p in result}
        self.assertEqual(by_sym["NVDA"]["side"], "long")
        self.assertEqual(by_sym["MSFT"]["side"], "short")
        self.assertEqual(by_sym["NVDA"]["quantity"], 10)

    def test_zero_position_filtered_out(self):
        """IBKR sometimes returns flat positions (qty=0). These must not
        appear in get_positions output."""
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            flat = mock.MagicMock()
            flat.contract.symbol = "XXX"
            flat.position = 0
            real = mock.MagicMock()
            real.contract.symbol = "AAA"
            real.position = 5
            real.avgCost = 100
            inst._ib.positions = mock.MagicMock(return_value=[flat, real])
            result = inst.get_positions()
        symbols = {p["symbol"] for p in result}
        self.assertIn("AAA", symbols)
        self.assertNotIn("XXX", symbols)

    def test_exception_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.positions = mock.MagicMock(side_effect=RuntimeError())
            self.assertEqual(inst.get_positions(), [])


class TestClosePosition(unittest.TestCase):
    """close_position issues a reverse market order for any open position."""

    def test_long_position_closed_with_sell(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "MarketOrder") as mock_mo:
            inst, _ = _mk_executor(Path(td))
            pos = mock.MagicMock()
            pos.contract.symbol = "NVDA"
            pos.position = 10  # long
            pos.contract = mock.MagicMock()
            pos.contract.symbol = "NVDA"
            inst._ib.positions = mock.MagicMock(return_value=[pos])
            result = inst.close_position("NVDA")
        self.assertTrue(result)
        args, _ = mock_mo.call_args
        self.assertEqual(args[0], "SELL")
        self.assertEqual(args[1], 10)

    def test_short_position_closed_with_buy(self):
        from helio import ibkr_executor as ex
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ex, "MarketOrder") as mock_mo:
            inst, _ = _mk_executor(Path(td))
            pos = mock.MagicMock()
            pos.contract.symbol = "NVDA"
            pos.position = -5  # short
            inst._ib.positions = mock.MagicMock(return_value=[pos])
            inst.close_position("NVDA")
        args, _ = mock_mo.call_args
        self.assertEqual(args[0], "BUY")
        self.assertEqual(args[1], 5)

    def test_no_position_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            inst, _ = _mk_executor(Path(td))
            inst._ib.positions = mock.MagicMock(return_value=[])
            self.assertFalse(inst.close_position("NVDA"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
