"""Synthetic fixtures only: none of these tests claim genuine market fills."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper


def at(day="2026-09-28", time="10:00:00"):
    return f"{day}T{time}+08:00"


def calendar(mode="demo", ident=None, days=None):
    days = days or ["2026-09-28", "2026-09-29", "2026-10-09"]
    return {"calendar_id": ident or mode + "-fixture-v1", "mode": mode, "exchange": "XSHG",
            "timezone": "Asia/Shanghai", "provenance": "synthetic" if mode == "demo" else "verified",
            "source": "Explicit synthetic test fixture" if mode == "demo" else "Test-only provider attestation",
            "source_url": "https://example.test/calendar", "verified_by": "unit test",
            "verification_note": "Synthetic test data, not actual exchange verification",
            "complete": True, "coverage_start": "2026-09-28", "coverage_end": "2026-10-09",
            "sessions": [{"date": d, "intervals": [[at(d, "09:30:00"), at(d, "11:30:00")],
                                                       [at(d, "13:00:00"), at(d, "15:00:00")]]} for d in days]}


def decision(side="buy", mode="demo", symbol=None, quantity=100, day="2026-09-28", time="10:00:00", **overrides):
    result = {"mode": mode, "symbol": symbol or ("DEMO01" if mode == "demo" else "600000"),
              "side": side, "quantity": 0 if side == "hold" else quantity, "submitted_at": at(day, time),
              "evidence_at": at(day, time), "source": "assistant", "reason": "Explicit synthetic analyst hypothesis",
              "thesis": "test only", "risks": "test only", "invalidation": "test only"}
    result.update(overrides)
    return result


def quote(mode="demo", symbol=None, day="2026-09-28", time="10:00:01", **overrides):
    result = {"quote_id": f"{day}-{time}", "mode": mode, "symbol": symbol or ("DEMO01" if mode == "demo" else "600000"),
              "observed_at": at(day, time), "received_at": at(day, time), "source": "synthetic test source",
              "source_url": "https://example.test/quote", "provenance": "synthetic" if mode == "demo" else "verified",
              "source_verified": True, "exchange": "XSHG", "currency": "CNY", "instrument_type": "A_SHARE",
              "market_status": "open", "suspended": False, "limit_state": "normal", "lower_limit": 9, "upper_limit": 11,
              "price": 10, "bid": 9.99, "ask": 10.01, "bid_size": 10000, "ask_size": 10000}
    result.update(overrides)
    return result


def session(mode="demo", day="2026-09-28", time="10:00:01", **overrides):
    result = {"calendar_id": mode + "-fixture-v1", "as_of": at(day, time), "clock": "replay"}
    result.update(overrides)
    return result


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)

    def tearDown(self):
        self.conn.close()

    def ready(self, mode="demo", days=None):
        paper.import_calendar(self.conn, calendar(mode, days=days))

    def submit(self, **kwargs):
        return paper.submit_decision(self.conn, decision(**kwargs))

    def run_quote(self, q=None, s=None, mode="demo"):
        return paper.process_pending(self.conn, mode, [q or quote(mode)], s or session(mode))

    def buy(self):
        self.ready()
        self.submit()
        result = self.run_quote()
        self.assertEqual(result["filled"], 1, result)
        return result

    def test_initial_accounts_isolated_and_idempotent(self):
        paper.init_paper(self.conn)
        for mode in ("demo", "imported"):
            state = paper.paper_state(self.conn, mode)
            self.assertEqual(state["cash"], 100000)
            self.assertEqual(state["initial_capital"], 100000)
            self.assertEqual(state["fills"], [])
            self.assertEqual(state["status"], "hold_missing_calendar")
            self.assertFalse(state["ai_api_integrated"])
            self.assertFalse(state["broker_connected"])
            json.dumps(state, allow_nan=False)
        with self.assertRaises(ValueError):
            paper.paper_state(self.conn, "live")

    def test_no_calendar_means_hold(self):
        self.submit()
        result = self.run_quote()
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["pending"], 1)
        self.assertIn("missing_verified_exchange_calendar", str(result["blocked"]))
        self.assertEqual(paper.paper_state(self.conn, "demo")["cash"], 100000)

    def test_ordinary_snapshot_does_not_become_executable(self):
        self.ready()
        self.submit()
        q = {"symbol": "DEMO01", "mode": "demo", "price": 10, "observed_at": at(time="10:00:01"), "source": "ordinary"}
        result = self.run_quote(q)
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(result["blocked"])

    def test_calendar_validation_and_immutability(self):
        base = calendar()
        paper.import_calendar(self.conn, base)
        paper.import_calendar(self.conn, base)
        changed = copy.deepcopy(base)
        changed["source"] = "changed"
        with self.assertRaises(ValueError):
            paper.import_calendar(self.conn, changed)
        for field, value in [("complete", False), ("provenance", "verified"), ("timezone", "UTC"), ("sessions", []), ("exchange", "UNKNOWN")]:
            changed = copy.deepcopy(base)
            changed["calendar_id"] = "bad-" + field
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                paper.import_calendar(self.conn, changed)
        bad = calendar("imported")
        bad.pop("verified_by")
        with self.assertRaises(ValueError):
            paper.import_calendar(self.conn, bad)
        bad = calendar("imported")
        bad["source_url"] = "http://example.test"
        with self.assertRaises(ValueError):
            paper.import_calendar(self.conn, bad)

    def test_hold_is_logged_without_fees(self):
        self.ready()
        d = self.submit(side="hold")
        self.assertEqual(d["status"], "hold")
        self.run_quote()
        state = paper.paper_state(self.conn, "demo")
        self.assertEqual(state["cash"], 100000)
        self.assertEqual(state["fees_paid"], 0)
        self.assertEqual(state["fills"], [])

    def test_validate_decision_lots_timestamps_and_source(self):
        for change in [{"quantity": 101}, {"quantity": True}, {"quantity": -100}, {"side": "short"},
                       {"reason": ""}, {"source": "ai_api"}, {"submitted_at": "2026-09-28T10:00:00"},
                       {"evidence_at": at(time="10:01:00")}, {"submitted_at": "2999-01-01T10:00:00Z"},
                       {"symbol": "600000; DROP TABLE"}, {"side": "hold", "quantity": 100}]:
            data = decision()
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                paper.submit_decision(self.conn, data)

    def test_duplicate_decision_exactly_once_and_key_conflict(self):
        self.ready()
        first = self.submit(decision_id="same")
        second = self.submit(decision_id="same")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(ValueError):
            self.submit(decision_id="same", quantity=200)
        result = self.run_quote()
        self.assertEqual(result["filled"], 1)
        self.assertEqual(self.run_quote()["filled"], 0)
        self.assertEqual(self.submit(decision_id="same")["status"], "filled")
        self.assertEqual(len(paper.paper_state(self.conn, "demo")["fills"]), 1)

    def test_default_idempotency_normalizes_timezones(self):
        a = self.submit()
        b = self.submit(submitted_at="2026-09-28T02:00:00Z", evidence_at="2026-09-28T02:00:00Z")
        self.assertEqual(a["id"], b["id"])

    def test_exact_duplicate_quotes_in_batch_are_deduplicated(self):
        self.ready()
        self.submit()
        result = paper.process_pending(self.conn, "demo", [quote(), quote()], session())
        self.assertEqual(result["filled"], 1)

    def test_strictly_next_quote_not_decision_evidence_quote(self):
        self.ready()
        self.submit()
        result = self.run_quote(quote(time="10:00:00"), session(time="10:00:00"))
        self.assertEqual(result["filled"], 0)
        result = self.run_quote()
        self.assertEqual(result["filled"], 1)
        self.assertEqual(result["fills"][0]["filled_at"], "2026-09-28T02:00:01.000000+00:00")

    def test_buy_fee_slippage_and_cash_math(self):
        result = self.buy()
        fill = result["fills"][0]
        self.assertEqual(fill["price"], 10.02)  # ask10.01 +5bps rounds adversely
        self.assertEqual(fill["gross"], 1002)
        self.assertEqual(fill["fees"], {"commission": 5, "stamp": 0, "transfer": .01})
        self.assertEqual(fill["total_fees"], 5.01)
        state = paper.paper_state(self.conn, "demo")
        self.assertEqual(state["cash"], 98992.99)
        self.assertEqual(state["equity"], 99991.99)
        self.assertEqual(state["positions"][0]["quantity"], 100)
        self.assertEqual(state["positions"][0]["cost"], 1007.01)
        self.assertEqual(state["track_record"], "synthetic_demo")
        self.assertEqual(paper.paper_state(self.conn, "imported")["cash"], 100000)

    def test_configuration_changes_are_explicit_and_cap_cannot_be_raised(self):
        paper.configure_paper(self.conn, "demo", {"slippage_bps": 0, "minimum_commission": 6})
        result = self.buy()
        self.assertEqual(result["fills"][0]["price"], 10.01)
        self.assertEqual(result["fills"][0]["fees"]["commission"], 6)
        for settings in [{"single_security_cap": .11}, {"single_security_cap": 0},
                         {"commission_rate": -1}, {"order_ttl_seconds": 0},
                         {"max_quote_age_seconds": 600}, {"initial_capital": 200000},
                         {"slippage_bps": float("nan")}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                paper.configure_paper(self.conn, "demo", settings)

    def test_cap_and_no_leverage(self):
        self.ready()
        self.submit(quantity=1000)
        result = self.run_quote()
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertIn("single_security_cap_exceeded", str(result))
        self.submit(quantity=100000, time="10:00:02")
        result = self.run_quote(quote(time="10:00:03", ask_size=100000), session(time="10:00:03"))
        self.assertIn("insufficient_cash_no_leverage", str(result))
        self.assertEqual(paper.paper_state(self.conn, "demo")["cash"], 100000)

    def test_no_shorting(self):
        self.ready()
        self.submit(side="sell")
        result = self.run_quote()
        self.assertIn("insufficient_position_no_shorting", str(result))
        self.assertEqual(result["rejected"], 1)

    def test_t_plus_one_same_day_rejected(self):
        self.buy()
        self.submit(side="sell", time="10:00:02")
        result = self.run_quote(quote(time="10:00:03"), session(time="10:00:03"))
        self.assertEqual(result["filled"], 0)
        self.assertIn("t_plus_one_locked", str(result))
        self.assertEqual(paper.paper_state(self.conn, "demo")["positions"][0]["quantity"], 100)

    def test_t_plus_one_next_explicit_session_and_sell_fee_pnl(self):
        self.buy()
        self.submit(side="sell", day="2026-09-29")
        result = self.run_quote(quote(day="2026-09-29"), session(day="2026-09-29"))
        self.assertEqual(result["filled"], 1, result)
        fill = result["fills"][0]
        self.assertEqual(fill["price"], 9.98)
        self.assertEqual(fill["fees"], {"commission": 5, "stamp": .5, "transfer": .01})
        self.assertEqual(fill["realized_pnl"], -14.52)
        state = paper.paper_state(self.conn, "demo")
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["cash"], 99985.48)
        self.assertEqual(state["realized_pnl"], -14.52)
        self.assertEqual(state["fees_paid"], 10.52)

    def test_calendar_does_not_guess_weekday_or_holidays(self):
        self.ready(days=["2026-09-28", "2026-10-09"])
        self.submit()
        self.run_quote()
        self.submit(side="sell", day="2026-09-29")
        result = self.run_quote(quote(day="2026-09-29"), session(day="2026-09-29"))
        self.assertIn("not_exchange_trading_session", str(result))
        self.assertEqual(result["filled"], 0)
        # September 29 is a weekday but is not a listed session in this synthetic fixture.

    def test_lunch_close_and_missing_coverage_hold(self):
        self.ready()
        self.submit()
        for day, time, reason in [("2026-09-28", "12:00:00", "outside_trading_interval"),
                                  ("2026-09-28", "15:00:00", "outside_trading_interval"),
                                  ("2026-09-27", "10:00:00", "outside_calendar_coverage")]:
            with self.subTest(time=time):
                result = self.run_quote(quote(day=day, time=time), session(day=day, time=time))
                self.assertIn(reason, str(result))

    def test_new_calendar_missing_buy_day_cannot_release_lots(self):
        self.buy()
        cal = calendar(ident="demo-short-coverage", days=["2026-09-29"])
        cal["coverage_start"] = "2026-09-29"
        paper.import_calendar(self.conn, cal)
        self.submit(side="sell", day="2026-09-29")
        result = self.run_quote(quote(day="2026-09-29"), session(day="2026-09-29", calendar_id=cal["calendar_id"]))
        self.assertIn("calendar_coverage_missing", str(result))
        self.assertEqual(result["filled"], 0)

    def test_unknown_suspension_limit_bounds_and_bad_numeric_hold(self):
        self.ready()
        self.submit()
        cases = [{"suspended": True}, {"suspended": None}, {"limit_state": "upper"},
                 {"limit_state": "unknown"}, {"market_status": "closed"}, {"lower_limit": None},
                 {"bid": float("nan")}, {"ask": -1}, {"ask_size": None}, {"currency": "USD"},
                 {"exchange": "XSHE"}, {"instrument_type": "ETF"}, {"provenance": "verified"}]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.run_quote(quote(**changes))
                self.assertEqual(result["filled"], 0)
                self.assertTrue(result["blocked"])

    def test_future_stale_and_receipt_before_observation_hold(self):
        self.ready()
        self.submit()
        tests = [(quote(time="10:00:02"), session()),
                 (quote(received_at=at(time="09:59:59")), session()),
                 (quote(), session(time="10:02:00")),
                 (quote(received_at=at(time="10:01:00")), session())]
        for q, s in tests:
            with self.subTest(q=q):
                result = self.run_quote(q, s)
                self.assertEqual(result["filled"], 0)
                self.assertTrue(result["blocked"])

    def test_limit_after_adverse_slippage_holds(self):
        self.ready()
        self.submit()
        result = self.run_quote(quote(upper_limit=10.015))
        self.assertIn("slippage_reaches_price_limit", str(result))
        self.assertEqual(result["filled"], 0)

    def test_imported_requires_verified_attestation_and_is_separate(self):
        self.ready("imported")
        self.submit(mode="imported")
        for changes in [{"source_verified": False}, {"provenance": "synthetic"}, {"source_url": ""}]:
            result = self.run_quote(quote("imported", **changes), session("imported"), "imported")
            self.assertEqual(result["filled"], 0)
        result = self.run_quote(quote("imported"), session("imported"), "imported")
        self.assertEqual(result["filled"], 1)
        imported = paper.paper_state(self.conn, "imported")
        self.assertEqual(imported["track_record"], "imported_hypothetical_paper")
        self.assertEqual(imported["clock"], "replay")
        self.assertFalse(imported["calendars"][0]["independently_verified"])
        self.assertEqual(paper.paper_state(self.conn, "demo")["fills"], [])

    def test_mode_mismatch_and_wrong_calendar_hold(self):
        self.ready()
        self.ready("imported")
        self.submit()
        result = self.run_quote(quote("imported"))
        self.assertIn("quote_mode_mismatch", str(result))
        result = self.run_quote(s=session(calendar_id="imported-fixture-v1"))
        self.assertIn("missing_verified_exchange_calendar", str(result))

    def test_replay_clock_cannot_move_back_or_accept_hindsight_decision(self):
        self.ready()
        self.run_quote()
        with self.assertRaisesRegex(ValueError, "hindsight"):
            self.submit()
        result = self.run_quote(quote(time="10:00:00"), session(time="10:00:00"))
        self.assertIn("simulation_clock_cannot_move_backwards", str(result))

    def test_clock_track_records_cannot_be_mixed(self):
        self.ready()
        self.run_quote()
        class HistoricalClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 28, 2, 0, 2, tzinfo=timezone.utc)
        with patch.object(paper, "datetime", HistoricalClock):
            result = self.run_quote(quote(time="10:00:02"), session(time="10:00:02", clock="realtime"))
        self.assertIn("cannot_mix_replay_and_realtime_track_records", str(result))

    def test_realtime_rejects_historical_clock(self):
        self.ready()
        self.submit()
        result = self.run_quote(s=session(clock="realtime"))
        self.assertIn("realtime_clock_must_match_wall_clock", str(result))
        self.assertEqual(result["filled"], 0)

    def test_order_expiry_without_quotes(self):
        self.ready()
        self.submit()
        result = paper.process_pending(self.conn, "demo", [], session(time="10:16:00"))
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(paper.paper_state(self.conn, "demo")["decisions"][0]["block_reason"], "order_expired")

    def test_liquidity_shared_and_no_partial_fill(self):
        self.ready()
        self.submit(decision_id="one")
        self.submit(decision_id="two")
        result = self.run_quote(quote(ask_size=150))
        self.assertEqual(result["filled"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertIn("insufficient_displayed_liquidity", str(result))
        self.assertEqual(paper.paper_state(self.conn, "demo")["positions"][0]["quantity"], 100)
        result = self.run_quote(quote(ask_size=150))
        self.assertEqual(result["filled"], 0)

    def test_quote_revision_rejected_and_batch_atomic(self):
        self.buy()
        before = paper.paper_state(self.conn, "demo")
        with self.assertRaisesRegex(ValueError, "quote_id reused"):
            self.run_quote(quote(ask_size=20000))
        self.assertEqual(before, paper.paper_state(self.conn, "demo"))

    def test_conflicting_same_timestamp_quotes_do_not_pick_favorable_price(self):
        self.ready()
        self.submit()
        q = quote(quote_id="different-source-event", ask=10.02)
        result = paper.process_pending(self.conn, "demo", [quote(), q], session())
        self.assertEqual(result["filled"], 0)
        self.assertIn("ambiguous_same_timestamp_quotes", str(result))

    def test_missing_fresh_marks_prevent_new_buy(self):
        self.buy()
        self.submit(symbol="DEMO02", time="10:02:00")
        result = self.run_quote(quote(symbol="DEMO02", time="10:02:01"), session(time="10:02:01"))
        self.assertEqual(result["filled"], 0)
        self.assertIn("fresh_marks_required", str(result))
        self.assertEqual(result["pending"], 1)

    def test_same_timestamp_marks_available_without_lookahead(self):
        self.buy()
        self.submit(symbol="DEMO02", time="10:02:00")
        result = paper.process_pending(self.conn, "demo", [quote(symbol="DEMO02", time="10:02:01"),
                                                          quote(symbol="DEMO01", time="10:02:01")], session(time="10:02:01"))
        self.assertEqual(result["filled"], 1, result)

    def test_unsorted_batch_uses_earliest_later_quote_not_better_later_price(self):
        self.ready()
        self.submit()
        later = quote(time="10:00:02", price=9.5, bid=9.49, ask=9.51, lower_limit=8)
        result = paper.process_pending(self.conn, "demo", [later, quote()], session(time="10:00:02"))
        self.assertEqual(result["filled"], 1)
        self.assertEqual(result["fills"][0]["price"], 10.02)
        self.assertEqual(result["fills"][0]["quote_id"], quote()["quote_id"])

    def test_fifo_sells_old_lot_not_same_day_lot(self):
        self.buy()
        self.submit(quantity=100, day="2026-09-29", decision_id="second-day-buy")
        self.assertEqual(self.run_quote(quote(day="2026-09-29"), session(day="2026-09-29"))["filled"], 1)
        self.submit(side="sell", quantity=100, day="2026-09-29", time="10:00:02")
        self.assertEqual(self.run_quote(quote(day="2026-09-29", time="10:00:03"), session(day="2026-09-29", time="10:00:03"))["filled"], 1)
        state = paper.paper_state(self.conn, "demo")
        self.assertEqual(state["positions"][0]["quantity"], 100)
        self.assertEqual(state["positions"][0]["lots"][0]["buy_session"], "2026-09-29")
        self.submit(side="sell", quantity=100, day="2026-09-29", time="10:00:04")
        result = self.run_quote(quote(day="2026-09-29", time="10:00:05"), session(day="2026-09-29", time="10:00:05"))
        self.assertEqual(result["filled"], 0)
        self.assertIn("t_plus_one_locked", str(result))

    def test_commission_above_minimum_and_stamp_only_on_sell(self):
        parts, total = paper._fees(100000000, "buy", paper.DEFAULTS)
        self.assertEqual(parts, {"commission": 30000, "stamp": 0, "transfer": 1000})
        self.assertEqual(total, 31000)
        parts, total = paper._fees(100000000, "sell", paper.DEFAULTS)
        self.assertEqual(parts, {"commission": 30000, "stamp": 50000, "transfer": 1000})
        self.assertEqual(total, 81000)

    def test_future_quote_in_batch_not_used_to_mark_earlier_buy(self):
        self.buy()
        self.submit(symbol="DEMO02", time="10:02:00")
        result = paper.process_pending(self.conn, "demo", [quote(symbol="DEMO01", time="10:02:02"),
                                                          quote(symbol="DEMO02", time="10:02:01")], session(time="10:02:02"))
        self.assertEqual(result["filled"], 0)
        self.assertIn("fresh_marks_required", str(result))

    def test_partial_lot_sale_keeps_cost_basis_and_fifo(self):
        self.ready()
        self.submit(quantity=300)
        self.run_quote()
        original_cost = paper.paper_state(self.conn, "demo")["positions"][0]["cost"]
        self.submit(side="sell", quantity=100, day="2026-09-29")
        result = self.run_quote(quote(day="2026-09-29"), session(day="2026-09-29"))
        state = paper.paper_state(self.conn, "demo")
        self.assertEqual(result["filled"], 1)
        self.assertEqual(state["positions"][0]["quantity"], 200)
        self.assertAlmostEqual(state["positions"][0]["cost"], round(original_cost * 2 / 3, 2), places=2)

    def test_caller_controls_transactions(self):
        self.conn.commit()
        self.submit()
        self.conn.rollback()
        self.assertEqual(paper.paper_state(self.conn, "demo")["decisions"], [])
        self.submit()
        self.conn.commit()
        self.assertEqual(len(paper.paper_state(self.conn, "demo")["decisions"]), 1)

    def test_file_database_restart_preserves_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            paper.init_paper(conn)
            paper.import_calendar(conn, calendar())
            paper.submit_decision(conn, decision())
            paper.process_pending(conn, "demo", [quote()], session())
            conn.commit()
            conn.close()
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            paper.init_paper(conn)
            self.assertEqual(paper.submit_decision(conn, decision())["status"], "filled")
            self.assertEqual(paper.process_pending(conn, "demo", [quote()], session())["filled"], 0)
            self.assertEqual(len(paper.paper_state(conn, "demo")["fills"]), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
