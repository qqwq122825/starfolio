"""Optional, deterministic PAPER exit plans. No models, news fetching or real orders.

All fractions and reference prices are explicit TEST inputs, not validated edges.
Stages sell fixed share counts; trailing liquidation sells the plan remainder.
A signal quote can create an order but can never fill that order itself. A gap
can produce a worse subsequent bid; the threshold is not a guaranteed fill price.
Only valid, unambiguous, strictly later bids update the persisted high-water mark.
"""
from decimal import Decimal
import json

EXIT_DISCLAIMER = ("退出参数仅供规则与账本测试，未经收益/稳健性验证。阈值按人工参考价计算，"
                  "不是实际净收益保证；触发后仍须下一条合格报价，跳空、费用、滑点、T+1、停牌和"
                  "涨跌停可能延迟或阻止成交。无新闻自动推断、无 AI API、无真实下单。")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS paper_exit_plans (
    mode TEXT NOT NULL, plan_id TEXT NOT NULL, symbol TEXT NOT NULL,
    digest TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
    status TEXT NOT NULL, PRIMARY KEY(mode,plan_id))""",
    """CREATE TABLE IF NOT EXISTS paper_exit_events (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, plan_id TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE, event TEXT NOT NULL, at TEXT NOT NULL,
    details TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS paper_data_conflicts (
    mode TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
    reason TEXT NOT NULL, PRIMARY KEY(mode,symbol,observed_at))""",
]


def _p():
    import paper
    return paper


def _event(conn, mode, plan_id, event, at, details, key):
    p = _p()
    conn.execute("INSERT OR IGNORE INTO paper_exit_events(mode,plan_id,event_key,event,at,details) VALUES(?,?,?,?,?,?)",
                 (mode, plan_id, p._hash([mode, plan_id, event, key]), event, at, p._json(details)))


def _write(conn, mode, ident, state):
    conn.execute("UPDATE paper_exit_plans SET state=?,status=? WHERE mode=? AND plan_id=?",
                 (_p()._json(state), state["status"], mode, ident))


def _view(row):
    body, state = json.loads(row["payload"]), json.loads(row["state"])
    state.pop("_consumed_quote_keys", None)
    return dict(body, **state, simulation_only=True, validated_edge=False)


def _remember_information(state, *times):
    """Persist the latest receipt/resolution time, including legacy state defaults."""
    p = _p()
    times = [value for value in (state.get("information_available_at"), *times) if value]
    if times:
        state["information_available_at"] = p._stamp(max(map(p._time, times)))
    return state.get("information_available_at")


def configure_exit_plan(conn, data):
    """Create immutable plan over shares already held; exact duplicates are safe."""
    p = _p()
    allowed = {"plan_id", "mode", "symbol", "submitted_at", "source", "reason", "quantity",
               "reference_price", "stages", "trailing", "test_only"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("unknown exit plan field or invalid object")
    mode = p._mode(data.get("mode"))
    ident = p._text(data.get("plan_id"), "plan_id", 120)
    symbol = p._symbol(data.get("symbol"), mode)
    if data.get("test_only") is not True:
        raise ValueError("exit plans require test_only:true; no validated edge")
    if data.get("source") not in {"assistant", "rules", "user"}:
        raise ValueError("exit plan source must be assistant, rules or user")
    submitted = p._time(data.get("submitted_at"))
    if submitted > p.datetime.now(p.timezone.utc):
        raise ValueError("exit plan cannot be submitted in the future")
    quantity = p._integer(data.get("quantity"), "quantity", 1)
    reference = p._num(data.get("reference_price"), "reference_price", "0.00000001")
    stages = data.get("stages", [])
    if not isinstance(stages, list) or len(stages) > 20:
        raise ValueError("stages must be a list with at most 20 entries")
    normalized, prior_gain, total = [], Decimal(0), 0
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {"gain_pct", "quantity"}:
            raise ValueError("stage requires only gain_pct and quantity")
        gain = p._num(stage["gain_pct"], "gain_pct", "0.00000001")
        size = p._integer(stage["quantity"], "stage quantity", 1)
        if gain <= prior_gain or gain > 10:
            raise ValueError("stage gains must strictly increase and be <=10")
        normalized.append({"gain_pct": str(gain), "quantity": size})
        prior_gain, total = gain, total + size
    trailing = data.get("trailing")
    if trailing is not None:
        if not isinstance(trailing, dict) or set(trailing) != {"activation_gain_pct", "distance_pct"}:
            raise ValueError("trailing requires activation_gain_pct and distance_pct")
        activation = p._num(trailing["activation_gain_pct"], "activation_gain_pct")
        distance = p._num(trailing["distance_pct"], "distance_pct")
        if activation > 10 or not 0 < distance < 1:
            raise ValueError("activation_gain_pct must be <=10 and 0<distance_pct<1")
        trailing = {"activation_gain_pct": str(activation), "distance_pct": str(distance)}
    if not stages and trailing is None:
        raise ValueError("enable stages or trailing")
    if total > quantity or (trailing is None and total != quantity) or (trailing is not None and total >= quantity):
        raise ValueError("stages must cover plan quantity, or leave a positive trailing remainder")
    body = {"plan_id": ident, "mode": mode, "symbol": symbol, "submitted_at": p._stamp(submitted),
            "source": data["source"], "reason": p._text(data.get("reason"), "reason"),
            "quantity": quantity, "reference_price": str(reference), "stages": normalized,
            "trailing": trailing, "test_only": True}
    digest = p._hash(body)
    with p._atomic(conn):
        account = p._account(conn, mode)
        row = conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND plan_id=?", (mode, ident)).fetchone()
        if row:
            if row["digest"] != digest:
                raise ValueError("plan_id reused with different content")
            return _view(row)
        if account["watermark"] and submitted < p._time(account["watermark"]):
            raise ValueError("exit plan predates processed simulation clock")
        if conn.execute("SELECT 1 FROM paper_exit_plans WHERE mode=? AND symbol=? AND status='active'", (mode, symbol)).fetchone():
            raise ValueError("one_active_exit_plan_per_symbol")
        if conn.execute("SELECT 1 FROM paper_decisions WHERE mode=? AND symbol=? AND status='pending'", (mode, symbol)).fetchone():
            raise ValueError("resolve_pending_decisions_before_exit_plan")
        lots = p._lots(conn, mode, symbol)
        if sum(lot["quantity"] for lot in lots) < quantity:
            raise ValueError("exit quantity exceeds currently owned position")
        if len({lot["exchange"] for lot in lots}) != 1:
            raise ValueError("exit plan requires one known position exchange")
        state = {"status": "active", "remaining_quantity": quantity, "high_water_mark": None,
                 "trailing_active": False, "completed_stages": [], "pending_decision_id": None,
                 "block_reason": "await_strictly_later_qualified_quote", "last_observed_at": None,
                 "intent": None, "attempt": 0, "exchange": lots[0]["exchange"], "information_available_at": None,
                 "_consumed_quote_keys": []}
        conn.execute("INSERT INTO paper_exit_plans VALUES(?,?,?,?,?,?,?)",
                     (mode, ident, symbol, digest, p._json(body), p._json(state), state["status"]))
        _event(conn, mode, ident, "plan_created", p._stamp(submitted), {"plan": body, "test_only": True}, "create")
        return _view(conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND plan_id=?", (mode, ident)).fetchone())


def _sync(conn, row, at, received_at=None):
    """Reconcile filled/rejected child order; decrement only after a durable fill."""
    p = _p()
    state = json.loads(row["state"])
    ident = state["pending_decision_id"]
    if not ident:
        return state
    order = conn.execute("SELECT * FROM paper_decisions WHERE mode=? AND decision_id=?", (row["mode"], ident)).fetchone()
    if order is None:
        raise ValueError("exit plan ledger inconsistent: missing generated decision")
    if order["status"] == "filled":
        fill = conn.execute("SELECT * FROM paper_fills WHERE decision_pk=?", (order["id"],)).fetchone()
        if fill is None or fill["quantity"] > state["remaining_quantity"]:
            raise ValueError("exit plan ledger inconsistent: fill/remaining quantity")
        # The next child depends on this fill's quantity, which is only knowable
        # once its execution quote has arrived, not merely at its observation.
        quote = conn.execute("SELECT payload FROM paper_quotes WHERE quote_key=?", (fill["quote_key"],)).fetchone()
        if quote is None:
            raise ValueError("exit plan ledger inconsistent: missing fill quote")
        _remember_information(state, at, received_at, json.loads(quote["payload"])["received_at"])
        state["remaining_quantity"] -= fill["quantity"]
        if state["intent"]["kind"] == "stage":
            state["completed_stages"].append(state["intent"]["stage_index"])
        _event(conn, row["mode"], row["plan_id"], "exit_filled", fill["filled_at"],
               {"decision_id": ident, "quantity": fill["quantity"], "remaining_quantity": state["remaining_quantity"],
                "trigger": state["intent"], "fill_price": float(fill["price"])}, ident)
        state["pending_decision_id"], state["intent"] = None, None
        state["block_reason"] = ""
        if state["remaining_quantity"] == 0:
            state["status"] = "completed"
    elif order["status"] in {"rejected", "cancelled"}:
        _remember_information(state, at, received_at)
        state["pending_decision_id"] = None
        state["block_reason"] = order["block_reason"]
        _event(conn, row["mode"], row["plan_id"], "exit_order_rejected", at,
               {"decision_id": ident, "reason": order["block_reason"], "remaining_quantity": state["remaining_quantity"]}, ident)
        # Retain the triggered exit intent for a fresh, later retry; never reuse
        # the expired decision or its old execution quote.
    else:
        state["block_reason"] = order["block_reason"] or "await_strictly_later_fill_quote"
    _write(conn, row["mode"], row["plan_id"], state)
    return state


def sync_all(conn, mode, at):
    for row in conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND status='active'", (mode,)).fetchall():
        _sync(conn, row, at)


def quarantine_ambiguous_history(conn, mode, at):
    """Stop plans whose consumed evidence became ambiguous, before any new fill.

    New plans retain exact quote provenance. Older persisted states have no
    tracker, so their consumed observation interval is treated conservatively.
    This never rewrites fills or releases the plan's manual-trading exclusion.
    """
    reason = "ambiguous_prior_quote_requires_cancel"
    ambiguous = conn.execute("""SELECT q.symbol,q.observed_at,q.quote_key FROM paper_quotes q
        JOIN (SELECT symbol,observed_at FROM paper_quotes WHERE mode=?
              GROUP BY symbol,observed_at HAVING COUNT(*)>1) conflicts
        ON q.symbol=conflicts.symbol AND q.observed_at=conflicts.observed_at
        WHERE q.mode=? ORDER BY q.observed_at,q.quote_key""", (mode, mode)).fetchall()
    if not ambiguous:
        return
    p = _p()
    # Surface ambiguity in already-used valuations/fills even without an active
    # plan. Unconsumed quotes quarantined on first sight are not retrospective
    # data conflicts. These warnings persist after marks move on or disappear.
    consumed_keys = {row["quote_key"] for row in conn.execute(
        "SELECT quote_key FROM paper_marks WHERE mode=? UNION SELECT quote_key FROM paper_fills WHERE mode=?",
        (mode, mode)).fetchall()}
    conflicts = {(q["symbol"], q["observed_at"]) for q in ambiguous if q["quote_key"] in consumed_keys}
    for row in conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND status='active'", (mode,)).fetchall():
        state = json.loads(row["state"])
        if not state["last_observed_at"]:
            continue
        body = json.loads(row["payload"])
        consumed = set(state["_consumed_quote_keys"]) if "_consumed_quote_keys" in state else None
        affected = [q for q in ambiguous if q["symbol"] == row["symbol"] and
                    p._time(body["submitted_at"]) < p._time(q["observed_at"]) <= p._time(state["last_observed_at"]) and
                    (consumed is None or q["quote_key"] in consumed)]
        conflicts.update((q["symbol"], q["observed_at"]) for q in affected)
        if not affected or state.get("data_conflict_at"):
            continue
        conflict = affected[0]
        if state["pending_decision_id"]:
            conn.execute("""UPDATE paper_decisions SET status='cancelled',block_reason=?
                WHERE mode=? AND decision_id=? AND status='pending'""",
                (reason, mode, state["pending_decision_id"]))
        state = _sync(conn, row, at)
        state.update(data_conflict_at=conflict["observed_at"], block_reason=reason)
        _write(conn, mode, row["plan_id"], state)
        _event(conn, mode, row["plan_id"], "plan_data_quarantined", at,
               {"ambiguous_observed_at": conflict["observed_at"], "reason": reason}, conflict["observed_at"])
    conn.executemany("""INSERT OR IGNORE INTO paper_data_conflicts(mode,symbol,observed_at,reason)
        VALUES(?,?,?,?)""", [(mode, symbol, observed, "late_ambiguous_quote") for symbol, observed in sorted(conflicts)])


def cancel_exit_plan(conn, mode, plan_id, reason):
    p = _p()
    p._mode(mode)
    p._text(plan_id, "plan_id", 120)
    reason = p._text(reason, "reason")
    with p._atomic(conn):
        row = conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND plan_id=?", (mode, plan_id)).fetchone()
        if row is None:
            raise ValueError("exit plan not found")
        if row["status"] != "active":
            return _view(row)
        account = p._account(conn, mode)
        times = [json.loads(row["payload"])["submitted_at"]]
        if account["watermark"]:
            times.append(account["watermark"])
        available = json.loads(row["state"]).get("information_available_at")
        if available:
            times.append(available)
        at = p._stamp(max(map(p._time, times)))
        state = _sync(conn, row, at)
        if state["status"] == "completed":
            return _view(conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND plan_id=?", (mode, plan_id)).fetchone())
        if state["pending_decision_id"]:
            conn.execute("UPDATE paper_decisions SET status='cancelled',block_reason='exit_plan_cancelled' WHERE mode=? AND decision_id=? AND status='pending'",
                         (mode, state["pending_decision_id"]))
        state.update(status="cancelled", pending_decision_id=None, block_reason=reason)
        state.pop("deferred_trailing", None)
        _write(conn, mode, plan_id, state)
        _event(conn, mode, plan_id, "plan_cancelled", at, {"reason": reason, "remaining_quantity": state["remaining_quantity"]}, "cancel")
        return _view(conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND plan_id=?", (mode, plan_id)).fetchone())


def evaluate(conn, q, cal):
    """Called after existing orders on this quote; all input gates already passed."""
    p = _p()
    rows = conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? AND symbol=? AND status='active'", (q["mode"], q["symbol"])).fetchall()
    for row in rows:
        body = json.loads(row["payload"])
        at = p._stamp(q["observed"])
        state = _sync(conn, row, at, q["received_at"])
        if state["status"] != "active" or state.get("data_conflict_at"):
            continue
        if q["exchange"] != state["exchange"]:
            continue
        if q["observed"] <= p._time(body["submitted_at"]) or (state["last_observed_at"] and q["observed"] <= p._time(state["last_observed_at"])):
            continue
        # Leave legacy trackers absent: inventing a fresh empty history would
        # forget their earlier evidence and defeat the conservative fallback.
        if "_consumed_quote_keys" in state:
            state["_consumed_quote_keys"].append(q["key"])
        state["last_observed_at"] = at
        information_at = _remember_information(state, q["received_at"])
        old_high = Decimal(state["high_water_mark"]) if state["high_water_mark"] is not None else None
        high = max(old_high, q["bid"]) if old_high is not None else q["bid"]
        state["high_water_mark"] = str(high)
        reference, trailing = Decimal(body["reference_price"]), body["trailing"]
        if trailing and q["bid"] >= reference * (1 + Decimal(trailing["activation_gain_pct"])):
            if not state["trailing_active"]:
                _event(conn, row["mode"], row["plan_id"], "trailing_activated", at,
                       {"bid": str(q["bid"]), "high_water_mark": str(high), "reference_price": str(reference)}, q["key"])
            state["trailing_active"] = True
        stop = high * (1 - Decimal(trailing["distance_pct"])) if trailing and state["trailing_active"] else None
        if (state["pending_decision_id"] and state["intent"]["kind"] == "stage" and
                not state.get("deferred_trailing") and stop is not None and q["bid"] <= stop):
            # Keep one child outstanding, but never lose a trailing crossing
            # while a stage waits for liquidity. Quantity is fixed on promotion,
            # after the stage's durable fill (if any) has reduced the remainder.
            state["deferred_trailing"] = {"kind": "trailing", "trigger_at": at,
                "trigger_bid": str(q["bid"]), "stop_level": str(stop), "high_water_mark": str(high),
                "information_available_at": information_at}
            _event(conn, row["mode"], row["plan_id"], "trailing_trigger_deferred", at,
                   state["deferred_trailing"], q["key"])
        if not state["pending_decision_id"]:
            # A triggered intent is latched. A later T+1 unlock or liquidity retry
            # does not erase the original exit signal merely because price bounces.
            deferred = state.pop("deferred_trailing", None)
            if deferred:
                information_at = _remember_information(state, deferred.get("information_available_at"))
                state["intent"] = {key: value for key, value in deferred.items() if key != "information_available_at"}
                state["intent"]["quantity"] = state["remaining_quantity"]
                _event(conn, row["mode"], row["plan_id"], "exit_triggered", at, state["intent"], q["key"])
            if stop is not None and q["bid"] <= stop and (not state["intent"] or state["intent"]["kind"] != "trailing"):
                state["intent"] = {"kind": "trailing", "quantity": state["remaining_quantity"],
                                   "trigger_at": at, "trigger_bid": str(q["bid"]), "stop_level": str(stop),
                                   "high_water_mark": str(high)}
                _event(conn, row["mode"], row["plan_id"], "exit_triggered", at, state["intent"], q["key"])
            if not state["intent"]:
                for index, stage in enumerate(body["stages"]):
                    if index in state["completed_stages"]:
                        continue
                    threshold = reference * (1 + Decimal(stage["gain_pct"]))
                    if q["bid"] >= threshold:
                        state["intent"] = {"kind": "stage", "stage_index": index, "quantity": stage["quantity"],
                                           "trigger_at": at, "trigger_bid": str(q["bid"]), "threshold": str(threshold)}
                        _event(conn, row["mode"], row["plan_id"], "exit_triggered", at, state["intent"], q["key"])
                    break  # sequential tranches; at most one outstanding order
            if state["intent"]:
                size = state["intent"]["quantity"]
                lots = p._lots(conn, row["mode"], q["symbol"])
                days = {entry["date"] for entry in cal["sessions"]}
                eligible = sum(lot["quantity"] for lot in lots if lot["exchange"] == q["exchange"] and
                               cal["coverage_start"] <= lot["buy_date"] < q["day"] and lot["buy_date"] in days)
                if sum(lot["quantity"] for lot in lots) < state["remaining_quantity"]:
                    state["block_reason"] = "position_changed_cancel_plan_for_review"
                elif eligible < size:
                    state["block_reason"] = "t_plus_one_locked_or_calendar_coverage_missing"
                else:
                    state["attempt"] += 1
                    ident = "paper-exit-" + p._hash([row["mode"], row["plan_id"], state["attempt"]])
                    context = {"plan_id": row["plan_id"], "trigger": state["intent"],
                               "high_water_mark": str(high), "information_available_at": information_at, "attempt": state["attempt"], "test_only": True}
                    reason = f"TEST ONLY: {state['intent']['kind']} exit for {row['plan_id']}; " + body["reason"]
                    order = p.submit_decision(conn, {"mode": row["mode"], "symbol": q["symbol"], "side": "sell",
                        "quantity": size, "submitted_at": information_at, "evidence_at": information_at, "source": "rules",
                        "reason": reason[:4000], "decision_id": ident}, _exit_context=context)
                    state["pending_decision_id"] = order["decision_id"]
                    state["block_reason"] = "await_strictly_later_fill_quote"
                    _event(conn, row["mode"], row["plan_id"], "exit_order_created", information_at,
                           dict(context, decision_id=ident, quantity=size), ident)
            else:
                state["block_reason"] = "await_exit_threshold"
        _write(conn, row["mode"], row["plan_id"], state)
        if state["block_reason"] not in {"await_exit_threshold", "await_strictly_later_fill_quote"}:
            _event(conn, row["mode"], row["plan_id"], "exit_blocked", at,
                   {"reason": state["block_reason"], "remaining_quantity": state["remaining_quantity"]}, q["key"])


def plan_state(conn, mode):
    return [_view(row) for row in conn.execute("SELECT * FROM paper_exit_plans WHERE mode=? ORDER BY rowid DESC", (mode,)).fetchall()]


def events(conn, mode, after=0, limit=200):
    return [{"id": row["id"], "plan_id": row["plan_id"], "event": row["event"], "at": row["at"],
             "details": json.loads(row["details"])} for row in conn.execute(
                 "SELECT * FROM paper_exit_events WHERE mode=? AND id>? ORDER BY id DESC LIMIT ?", (mode, after, limit)).fetchall()]
