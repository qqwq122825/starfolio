"""Fail-closed, stdlib-only *paper* trading ledger. No broker or AI API calls.

Public API (all writes use savepoints; the caller owns commit/rollback):
    init_paper(conn)
    configure_paper(conn, mode, {fee/risk settings})
    import_calendar(conn, calendar_dict)
    submit_decision(conn, decision_dict) -> JSON-compatible decision dict
    process_pending(conn, mode, quotes, session) -> JSON-compatible run summary
    paper_state(conn, mode) -> JSON-compatible account/ledger dict
    configure_exit_plan(conn, plan_dict) / cancel_exit_plan(conn, mode, plan_id, reason)

Modes are ONLY ``demo`` and ``imported`` with independent CNY 100,000 accounts.
No calendar is installed by default. Missing/invalid metadata means HOLD, never
an assumed fill. Imported means user/adapter supplied data, NOT a verified live
feed. Imported replay results are hypothetical and cannot be presented as live
performance. ``source_verified`` and calendar verification are input attestations;
this module does not independently verify any provider or exchange publication.

Decision contract: mode, symbol, side (buy/sell/hold), integer quantity (hold=0),
submitted_at, evidence_at (timezone-aware ISO timestamps), nonempty reason,
source (assistant/rules/user), optional decision_id, thesis, risks, invalidation.
Evidence must precede/equal submission. Buy quantities must be 100-share lots.
Optional decision_id is an idempotency key; reusing it with changed data fails.
Without it the normalized complete decision is hashed. 'assistant' labels the
submitter, not an integrated model. Optional explicit TEST-ONLY exit plans may
create deterministic rules-source sell decisions when process_pending receives
qualified quotes. They never create buys or fetch data. See docs/paper-exits.md.

Calendar contract, persisted via import_calendar:
    {calendar_id, mode, exchange: 'XSHG'|'XSHE'|'XBEI',
     timezone:'Asia/Shanghai', provenance:'synthetic'|'verified', source,
     source_url, complete:true, coverage_start:'YYYY-MM-DD',
     coverage_end:'YYYY-MM-DD', sessions:[
       {date:'YYYY-MM-DD', intervals:[
          ['2026-09-28T09:30:00+08:00','2026-09-28T11:30:00+08:00'],
          ['2026-09-28T13:00:00+08:00','2026-09-28T15:00:00+08:00']]}]}
Imported calendars additionally need verified_by and verification_note plus an
HTTPS source_url; demo requires synthetic provenance. complete attests that the
listed sessions are the entire exchange calendar for that coverage, including
holiday omissions. No weekday/holiday guessing occurs. Calendar IDs are immutable.
Only explicit intervals are executable; closes are exclusive. T+1 requires a
strictly later listed exchange session, with coverage including the lot's buy
session. A different exchange's calendar cannot release a lot.

process_pending session = {calendar_id, as_of:ISO, clock:'realtime'|'replay'}.
Realtime as_of must be within five seconds of the actual UTC clock. Replay is
explicit, hypothetical, and uses as_of as its historical simulation clock. Each
mode is permanently bound to its first valid clock: replay and realtime can never
mix. Mode clocks are monotonic; a decision cannot be inserted behind the last run.
First-time historical imports cannot prove the analyst lacked hindsight; this is
explicitly disclosed. Quotes are processed in observation order, never using a
later quote to value a position for an earlier fill. Generated exits retain max received_at of their contributing
trigger/high-water evidence; submission cannot precede that availability.
Evidence <= decision < quote
<= received_at <= as_of; maximum quote/arrival age defaults to 60 seconds. Orders
expire after 15 minutes. Retries/duplicates cannot fill twice. Reusing a quote ID
with changed data fails; no retroactive corrected fills. Same-time competing
quotes remain quarantined on subset replay, including after restart. Successful processing of
an earlier quote prevents subsequently inserting a backdated decision. This is a
paper execution approximation, not a full order-book or market-impact simulator.

Executable quote requires existing snapshot fields symbol, mode, price,
observed_at, source, source_url, and these EXTRA fields:
    quote_id, received_at, exchange, currency:'CNY', instrument_type:'A_SHARE',
    provenance:'synthetic' (demo) or 'verified' (imported),
    source_verified:true (imported), market_status:'open', suspended:false,
    limit_state:'normal', lower_limit, upper_limit, bid, ask, bid_size, ask_size.
Sizes are shares. Known sufficient displayed size is required for a full fill;
liquidity is shared across decisions on a quote, with no partial fills. The ledger
uses bid/ask plus adverse slippage (5 bps, rounded adversely to CNY .01), not the
last trade. Any suspension, price-limit state or unknown bounds blocks execution.
Buy exposure is at most 10% of post-fee marked equity AND 10% of initial capital;
other holdings need fresh earlier-or-equal admissible marks for a new buy. Cash
never goes negative, shorting/leverage is prohibited, and sell lots are FIFO/T+1.
Lot rules apply only to instruments compatible with 100-share buys; this is not
an implementation of every board's special lot/order rules. Fee assumptions are
illustrative and configurable, NOT verified current broker tariffs: commission
0.03%, minimum CNY 5 per order; sell stamp duty 0.05%; transfer 0.001% each side.
No minimum fee is charged to HOLD/rejected decisions. Rounding is per charge to
CNY cents. No corporate-action, dividends, tax-lot, financing or settlement model.
"""
import paper_exits
from paper_exits import configure_exit_plan, cancel_exit_plan
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

MODES = {"demo", "imported"}
EXCHANGES = {"XSHG", "XSHE", "XBEI"}
TZ = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    "commission_rate": 0.0003, "minimum_commission": 5.0,
    "sell_stamp_rate": 0.0005, "transfer_rate": 0.00001,
    "slippage_bps": 5.0, "single_security_cap": 0.10,
    "max_quote_age_seconds": 60, "order_ttl_seconds": 900,
}
INITIAL_CENTS = 10_000_000
DISCLAIMER = ("纯虚拟模拟；无券商连接、无真实下单、无已接入 AI API。费用为可配置示例，"
              "导入数据/交易日历的 verified 标记只是提供者声明，未经本模块独立核验；"
              "历史回放可能存在事后选择偏差，不能当作真实实时业绩。")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a timezone-aware ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _stamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="microseconds")


def _num(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{name} must be finite numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    if not number.is_finite() or number < Decimal(str(minimum)):
        raise ValueError(f"invalid {name}")
    return number


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be integer >= {minimum}")
    return value


def _mode(mode):
    if mode not in MODES:
        raise ValueError("paper mode must be demo or imported; live is not integrated")
    return mode


def _text(value, name, maxlen=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maxlen:
        raise ValueError(f"{name} must be a nonempty string <= {maxlen} characters")
    return value.strip()


def _symbol(value, mode):
    if not isinstance(value, str) or not (re.fullmatch(r"\d{6}", value) or
            (mode == "demo" and re.fullmatch(r"DEMO[A-Z0-9_-]{1,20}", value))):
        raise ValueError("invalid symbol")
    return value


def _url(value):
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("verified source must have a credential-free HTTPS URL")
    return str(value)


def _cents(value, rounding=ROUND_HALF_UP):
    return int((value * 100).to_integral_value(rounding=rounding))


def _money(cents):
    return float(Decimal(cents) / 100)


@contextmanager
def _atomic(conn):
    # Does not commit unrelated caller work, unlike sqlite connection contexts.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT paper_operation")
    try:
        yield
        conn.execute("RELEASE SAVEPOINT paper_operation")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT paper_operation")
        conn.execute("RELEASE SAVEPOINT paper_operation")
        raise


def init_paper(conn):
    """Initialize isolated tables/accounts; repeated initialization is harmless."""
    statements = [
        """CREATE TABLE IF NOT EXISTS paper_accounts (
        mode TEXT PRIMARY KEY, initial_cents INTEGER NOT NULL, cash_cents INTEGER NOT NULL CHECK(cash_cents>=0),
        config TEXT NOT NULL, clock TEXT, watermark TEXT, realized_cents INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS paper_calendars (
        calendar_id TEXT PRIMARY KEY, mode TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS paper_decisions (
        id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, quantity INTEGER NOT NULL, submitted_at TEXT NOT NULL, evidence_at TEXT NOT NULL,
        source TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, block_reason TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(mode,decision_id))""",
        """CREATE TABLE IF NOT EXISTS paper_fills (
        id INTEGER PRIMARY KEY, decision_pk INTEGER UNIQUE NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL, gross_cents INTEGER NOT NULL,
        fee_cents INTEGER NOT NULL, realized_cents INTEGER NOT NULL, filled_at TEXT NOT NULL,
        quote_key TEXT NOT NULL, calendar_id TEXT NOT NULL, clock TEXT NOT NULL, payload TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS paper_lots (
        id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
        buy_date TEXT NOT NULL, calendar_id TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>=0),
        cost_cents INTEGER NOT NULL CHECK(cost_cents>=0), fill_id INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS paper_quotes (
        quote_key TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
        digest TEXT NOT NULL, payload TEXT NOT NULL, used_buy INTEGER NOT NULL DEFAULT 0,
        used_sell INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS paper_marks (
        mode TEXT NOT NULL, symbol TEXT NOT NULL, price TEXT NOT NULL, observed_at TEXT NOT NULL,
        exchange TEXT NOT NULL, quote_key TEXT NOT NULL, PRIMARY KEY(mode,symbol))""",
        "CREATE INDEX IF NOT EXISTS paper_pending ON paper_decisions(mode,status,submitted_at)",
        "CREATE INDEX IF NOT EXISTS paper_quote_history ON paper_quotes(mode,symbol,observed_at)",
    ]
    with _atomic(conn):
        for statement in statements + paper_exits.SCHEMA:
            conn.execute(statement)
        for mode in sorted(MODES):
            conn.execute("INSERT OR IGNORE INTO paper_accounts(mode,initial_cents,cash_cents,config) VALUES(?,?,?,?)",
                         (mode, INITIAL_CENTS, INITIAL_CENTS, _json(DEFAULTS)))


def _account(conn, mode):
    _mode(mode)
    row = conn.execute("SELECT * FROM paper_accounts WHERE mode=?", (mode,)).fetchone()
    if row is None:
        raise ValueError("call init_paper(conn) first")
    return dict(row)


def configure_paper(conn, mode, changes):
    """Configure illustrative execution assumptions; never loosen 10% capital cap."""
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("unknown paper configuration field")
    with _atomic(conn):
        config = json.loads(_account(conn, mode)["config"])
        for key, value in changes.items():
            number = _num(value, key)
            if key in {"max_quote_age_seconds", "order_ttl_seconds"}:
                _integer(value, key, 1)
                if value > (300 if key == "max_quote_age_seconds" else 86400):
                    raise ValueError(f"{key} exceeds safe maximum")
                config[key] = value
            else:
                if key == "single_security_cap" and not (0 < number <= Decimal("0.10")):
                    raise ValueError("single_security_cap must be >0 and <=0.10")
                if key in {"commission_rate", "sell_stamp_rate", "transfer_rate"} and number > 1:
                    raise ValueError("fee rate must be <=1")
                if key == "slippage_bps" and number > 1000:
                    raise ValueError("slippage_bps must be <=1000")
                config[key] = float(number)
        conn.execute("UPDATE paper_accounts SET config=? WHERE mode=?", (_json(config), mode))
    return config


def import_calendar(conn, data):
    """Validate and persist caller-attested calendar, never fetch or infer sessions."""
    if not isinstance(data, dict):
        raise ValueError("calendar must be an object")
    cal = dict(data)
    ident = _text(cal.get("calendar_id"), "calendar_id", 120)
    mode = _mode(cal.get("mode"))
    if cal.get("exchange") not in EXCHANGES or cal.get("timezone") != "Asia/Shanghai":
        raise ValueError("calendar must identify supported exchange and Asia/Shanghai timezone")
    if cal.get("provenance") != ("synthetic" if mode == "demo" else "verified"):
        raise ValueError("calendar provenance incompatible with mode")
    _text(cal.get("source"), "source", 500)
    if cal.get("complete") is not True:
        raise ValueError("calendar must attest complete session coverage")
    if mode == "imported":
        _url(cal.get("source_url"))
        _text(cal.get("verified_by"), "verified_by", 500)
        _text(cal.get("verification_note"), "verification_note")
    try:
        start, end = date.fromisoformat(cal["coverage_start"]), date.fromisoformat(cal["coverage_end"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("calendar requires ISO coverage_start/coverage_end dates") from exc
    if end < start:
        raise ValueError("calendar coverage reversed")
    if not isinstance(cal.get("sessions"), list) or not cal["sessions"]:
        raise ValueError("calendar must contain explicit sessions")
    seen = set()
    for entry in cal["sessions"]:
        try:
            day = date.fromisoformat(entry["date"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("invalid session date") from exc
        if not start <= day <= end or day.isoformat() in seen:
            raise ValueError("duplicate/out-of-coverage calendar session")
        seen.add(day.isoformat())
        intervals = entry.get("intervals")
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("session requires nonempty explicit trading intervals")
        last_close = None
        for interval in intervals:
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("interval must be [open ISO, close ISO]")
            opened, closed = map(_time, interval)
            if opened >= closed or opened.astimezone(TZ).date() != day or closed.astimezone(TZ).date() != day:
                raise ValueError("invalid or cross-date session interval")
            if last_close is not None and opened < last_close:
                raise ValueError("overlapping/unsorted session intervals")
            last_close = closed
    cal["calendar_id"] = ident
    cal["sessions"] = sorted(cal["sessions"], key=lambda item: item["date"])
    digest = _hash(cal)
    with _atomic(conn):
        _account(conn, mode)
        prior = conn.execute("SELECT digest FROM paper_calendars WHERE calendar_id=?", (ident,)).fetchone()
        if prior is not None and prior["digest"] != digest:
            raise ValueError("calendar_id already exists with different content; use a new version ID")
        conn.execute("INSERT OR IGNORE INTO paper_calendars VALUES(?,?,?,?)", (ident, mode, digest, _json(cal)))
    return {"calendar_id": ident, "mode": mode, "sessions": len(seen), "provenance": cal["provenance"],
            "independently_verified": False}


def _decision(row):
    result = json.loads(row["payload"])
    result.update({"id": row["id"], "decision_id": row["decision_id"], "status": row["status"],
                   "block_reason": row["block_reason"], "created_at": row["created_at"]})
    return result


def submit_decision(conn, data, *, _exit_context=None):
    if not isinstance(data, dict):
        raise ValueError("decision must be an object")
    mode = _mode(data.get("mode"))
    symbol = _symbol(data.get("symbol"), mode)
    side = data.get("side")
    if side not in {"buy", "sell", "hold"}:
        raise ValueError("side must be buy, sell or hold")
    quantity = _integer(data.get("quantity", 0 if side == "hold" else None), "quantity")
    if (side == "hold" and quantity != 0) or (side != "hold" and quantity == 0):
        raise ValueError("hold quantity must be zero; buy/sell quantity must be positive")
    if side == "buy" and quantity % 100:
        raise ValueError("buy quantities must be multiples of 100")
    submitted, evidence = _time(data.get("submitted_at")), _time(data.get("evidence_at"))
    if evidence > submitted:
        raise ValueError("evidence_at cannot be later than submitted_at")
    if submitted > datetime.now(timezone.utc):
        raise ValueError("decision cannot be submitted in the future")
    source = data.get("source")
    if source not in {"assistant", "rules", "user"}:
        raise ValueError("decision source must be assistant, rules or user")
    body = {"mode": mode, "symbol": symbol, "side": side, "quantity": quantity,
            "submitted_at": _stamp(submitted), "evidence_at": _stamp(evidence), "source": source,
            "reason": _text(data.get("reason"), "reason")}
    for key in ("thesis", "risks", "invalidation"):
        if key in data:
            body[key] = _text(data[key], key)
    if _exit_context is not None:
        body["exit_context"] = _exit_context
    digest = _hash(body)
    decision_id = _text(data.get("decision_id", digest), "decision_id", 160)
    with _atomic(conn):
        account = _account(conn, mode)
        prior = conn.execute("SELECT * FROM paper_decisions WHERE mode=? AND decision_id=?", (mode, decision_id)).fetchone()
        if prior is not None:
            if prior["digest"] != digest:
                raise ValueError("decision_id reused with different content")
            return _decision(prior)
        if side != "hold" and _exit_context is None and conn.execute(
                "SELECT 1 FROM paper_exit_plans WHERE mode=? AND symbol=? AND status='active'", (mode, symbol)).fetchone():
            raise ValueError("active_exit_plan_requires_cancel_before_manual_trade")
        if account["watermark"] and submitted < _time(account["watermark"]):
            raise ValueError("decision predates the processed simulation clock; hindsight insertion rejected")
        cursor = conn.execute("""INSERT INTO paper_decisions(decision_id,mode,symbol,side,quantity,
            submitted_at,evidence_at,source,reason,status,digest,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, mode, symbol, side, quantity, _stamp(submitted), _stamp(evidence), source,
             body["reason"], "hold" if side == "hold" else "pending", digest, _json(body), _stamp()))
        row = conn.execute("SELECT * FROM paper_decisions WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _decision(row)


def _calendar_session(cal, when):
    day = when.astimezone(TZ).date().isoformat()
    if not cal["coverage_start"] <= day <= cal["coverage_end"]:
        raise ValueError("outside_calendar_coverage")
    for entry in cal["sessions"]:
        if entry["date"] == day:
            if any(_time(start) <= when < _time(end) for start, end in entry["intervals"]):
                return day
            raise ValueError("outside_trading_interval")
    raise ValueError("not_exchange_trading_session")


def _session(conn, mode, session, account):
    if not isinstance(session, dict) or not session.get("calendar_id"):
        raise ValueError("missing_verified_exchange_calendar")
    row = conn.execute("SELECT * FROM paper_calendars WHERE calendar_id=? AND mode=?",
                       (session["calendar_id"], mode)).fetchone()
    if row is None:
        raise ValueError("missing_verified_exchange_calendar")
    cal = json.loads(row["payload"])
    as_of = _time(session.get("as_of"))
    clock = session.get("clock")
    if clock not in {"realtime", "replay"}:
        raise ValueError("explicit_realtime_or_replay_clock_required")
    now = datetime.now(timezone.utc)
    if as_of > now:
        raise ValueError("future_simulation_clock")
    if clock == "realtime" and abs((now - as_of).total_seconds()) > 5:
        raise ValueError("realtime_clock_must_match_wall_clock")
    if account["clock"] and account["clock"] != clock:
        raise ValueError("cannot_mix_replay_and_realtime_track_records")
    if account["watermark"] and as_of < _time(account["watermark"]):
        raise ValueError("simulation_clock_cannot_move_backwards")
    day = _calendar_session(cal, as_of)
    return cal, as_of, clock, day


def _quote(data, mode, cal, as_of, config):
    q = dict(data)
    if q.get("mode") != mode:
        raise ValueError("quote_mode_mismatch")
    _symbol(q.get("symbol"), mode)
    _text(q.get("quote_id"), "quote_id", 160)
    _text(q.get("source"), "source", 500)
    expected = "synthetic" if mode == "demo" else "verified"
    if q.get("provenance") != expected:
        raise ValueError("quote_provenance_incompatible_with_mode")
    if mode == "imported":
        _url(q.get("source_url"))
        if q.get("source_verified") is not True:
            raise ValueError("unverified_quote_source")
    if q.get("exchange") != cal["exchange"]:
        raise ValueError("quote_exchange_calendar_mismatch")
    if q.get("currency") != "CNY" or q.get("instrument_type") != "A_SHARE":
        raise ValueError("unsupported_currency_or_instrument")
    observed, received = _time(q.get("observed_at")), _time(q.get("received_at"))
    if not observed <= received <= as_of:
        raise ValueError("quote_future_or_invalid_receipt_time")
    if (as_of - observed).total_seconds() > config["max_quote_age_seconds"]:
        raise ValueError("stale_quote")
    day = _calendar_session(cal, observed)
    if day != as_of.astimezone(TZ).date().isoformat():
        raise ValueError("quote_from_other_session")
    if q.get("market_status") != "open" or q.get("suspended") is not False:
        raise ValueError("market_closed_suspended_or_unknown")
    if q.get("limit_state") != "normal":
        raise ValueError("limit_state_blocked_or_unknown")
    for name in ("price", "bid", "ask", "lower_limit", "upper_limit"):
        q[name] = _num(q.get(name), name, "0.00000001")
    if not q["lower_limit"] < q["bid"] <= q["price"] <= q["ask"] < q["upper_limit"]:
        raise ValueError("inconsistent_price_spread_or_price_limit")
    q["bid_size"] = _integer(q.get("bid_size"), "bid_size", 0)
    q["ask_size"] = _integer(q.get("ask_size"), "ask_size", 0)
    q["observed"] = observed
    q["day"] = day
    q["key"] = _hash([mode, q["source"], q["symbol"], q["quote_id"]])
    return q


def _fees(gross_cents, side, config):
    gross = Decimal(gross_cents) / 100
    commission = max(_num(config["minimum_commission"], "minimum_commission"),
                     gross * _num(config["commission_rate"], "commission_rate"))
    parts = {"commission": _cents(commission),
             "stamp": _cents(gross * _num(config["sell_stamp_rate"], "sell_stamp_rate")) if side == "sell" else 0,
             "transfer": _cents(gross * _num(config["transfer_rate"], "transfer_rate"))}
    return parts, sum(parts.values())


def _lots(conn, mode, symbol=None):
    query, args = "SELECT * FROM paper_lots WHERE mode=? AND quantity>0", [mode]
    if symbol is not None:
        query += " AND symbol=?"
        args.append(symbol)
    return [dict(row) for row in conn.execute(query + " ORDER BY buy_date,id", args).fetchall()]


def _quote_history_is_unambiguous(conn, mode, symbol, observed_at):
    return conn.execute("SELECT COUNT(*) AS n FROM paper_quotes WHERE mode=? AND symbol=? AND observed_at=?",
                        (mode, symbol, observed_at)).fetchone()["n"] == 1


def _available_holding_mark(conn, mode, symbol, exchange, at, max_age):
    """Use only historically received, unambiguous evidence available at fill time.

    paper_marks is a presentation cache; its newest packet may arrive later than
    a proposed historical fill. Search durable validated quotes instead.
    """
    rows = conn.execute("SELECT observed_at,payload FROM paper_quotes WHERE mode=? AND symbol=? AND observed_at BETWEEN ? AND ? ORDER BY observed_at DESC,quote_key",
                        (mode, symbol, _stamp(at-timedelta(seconds=max_age)), _stamp(at))).fetchall()
    for row in rows:
        observed = _time(row["observed_at"])
        age = (at - observed).total_seconds()
        if age < 0 or age > max_age:
            continue
        raw = json.loads(row["payload"])
        if raw.get("exchange") != exchange or _time(raw["received_at"]) > at:
            continue
        # A late conflict must never revive an older, more favorable valuation.
        if not _quote_history_is_unambiguous(conn, mode, symbol, row["observed_at"]):
            raise ValueError("fresh_marks_required_for_all_holdings")
        return _num(raw["bid"], "bid", "0.00000001")
    raise ValueError("fresh_marks_required_for_all_holdings")


def _equity_for_buy(conn, account, q, config):
    equity = account["cash_cents"]
    exposure = 0
    for lot in _lots(conn, account["mode"]):
        price = _available_holding_mark(conn, account["mode"], lot["symbol"], lot["exchange"],
                                        q["observed"], config["max_quote_age_seconds"])
        marked = _cents(price * lot["quantity"], ROUND_FLOOR)
        equity += marked
        if lot["symbol"] == q["symbol"]:
            exposure += marked
    return equity, exposure


def _execute(conn, decision, q, cal, clock, config):
    side, quantity, mode = decision["side"], decision["quantity"], decision["mode"]
    if q["observed"] <= _time(decision["submitted_at"]):
        raise ValueError("await_strictly_later_quote")
    if (q["observed"] - _time(decision["submitted_at"])).total_seconds() > config["order_ttl_seconds"]:
        raise ValueError("order_expired")
    liquidity = conn.execute("SELECT used_buy,used_sell FROM paper_quotes WHERE quote_key=?", (q["key"],)).fetchone()
    available = q["ask_size"] - liquidity["used_buy"] if side == "buy" else q["bid_size"] - liquidity["used_sell"]
    if quantity > available:
        raise ValueError("insufficient_displayed_liquidity")
    slip = _num(config["slippage_bps"], "slippage_bps") / 10000
    raw = q["ask"] * (1 + slip) if side == "buy" else q["bid"] * (1 - slip)
    price = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR)
    if not q["lower_limit"] < price < q["upper_limit"]:
        raise ValueError("slippage_reaches_price_limit")
    gross = _cents(price * quantity)
    parts, fees = _fees(gross, side, config)
    account = _account(conn, mode)
    allocated = []
    realized = 0
    if side == "buy":
        if gross + fees > account["cash_cents"]:
            raise ValueError("insufficient_cash_no_leverage")
        equity, exposure = _equity_for_buy(conn, account, q, config)
        # Mark the new shares at bid; spreads/slippage/fees reduce equity immediately.
        new_mark = _cents(q["bid"] * quantity, ROUND_FLOOR)
        post_equity = equity - gross - fees + new_mark
        cap = _num(config["single_security_cap"], "single_security_cap")
        # Use paid value for the new order as an additional conservative bound.
        if exposure + max(gross, new_mark) > Decimal(min(account["initial_cents"], post_equity)) * cap:
            raise ValueError("single_security_cap_exceeded")
        cash_delta = -gross - fees
    else:
        lots = _lots(conn, mode, q["symbol"])
        if sum(lot["quantity"] for lot in lots) < quantity:
            raise ValueError("insufficient_position_no_shorting")
        valid_dates = {entry["date"] for entry in cal["sessions"]}
        eligible = [lot for lot in lots if lot["exchange"] == q["exchange"] and
                    cal["coverage_start"] <= lot["buy_date"] < q["day"] and lot["buy_date"] in valid_dates]
        if sum(lot["quantity"] for lot in eligible) < quantity:
            raise ValueError("t_plus_one_locked_or_calendar_coverage_missing")
        needed, cost = quantity, 0
        for lot in eligible:
            take = min(needed, lot["quantity"])
            share_cost = lot["cost_cents"] if take == lot["quantity"] else int(
                (Decimal(lot["cost_cents"]) * take / lot["quantity"]).to_integral_value(rounding=ROUND_HALF_UP))
            allocated.append((lot["id"], take, share_cost))
            cost += share_cost
            needed -= take
            if not needed:
                break
        cash_delta = gross - fees
        if account["cash_cents"] + cash_delta < 0:
            raise ValueError("insufficient_cash_for_sell_fees")
        realized = cash_delta - cost
    payload = {"decision_id": decision["decision_id"], "mode": mode, "symbol": q["symbol"],
               "side": side, "quantity": quantity, "price": float(price), "gross": _money(gross),
               "fees": {key: _money(val) for key, val in parts.items()}, "total_fees": _money(fees),
               "cash_delta": _money(cash_delta), "realized_pnl": _money(realized),
               "filled_at": _stamp(q["observed"]), "quote_id": q["quote_id"], "quote_source": q["source"],
               "provenance": q["provenance"], "calendar_id": cal["calendar_id"], "clock": clock,
               "execution_assumptions": config, "simulation_only": True}
    cursor = conn.execute("""INSERT INTO paper_fills(decision_pk,mode,symbol,side,quantity,price,gross_cents,
        fee_cents,realized_cents,filled_at,quote_key,calendar_id,clock,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (decision["id"], mode, q["symbol"], side, quantity, str(price), gross, fees, realized,
         _stamp(q["observed"]), q["key"], cal["calendar_id"], clock, _json(payload)))
    if side == "buy":
        conn.execute("""INSERT INTO paper_lots(mode,symbol,exchange,buy_date,calendar_id,quantity,cost_cents,fill_id)
            VALUES(?,?,?,?,?,?,?,?)""", (mode, q["symbol"], q["exchange"], q["day"], cal["calendar_id"],
                                        quantity, gross + fees, cursor.lastrowid))
    else:
        for lot_id, taken, cost in allocated:
            conn.execute("UPDATE paper_lots SET quantity=quantity-?,cost_cents=cost_cents-? WHERE id=?", (taken, cost, lot_id))
    conn.execute("UPDATE paper_accounts SET cash_cents=cash_cents+?,realized_cents=realized_cents+? WHERE mode=?",
                 (cash_delta, realized, mode))
    column = "used_buy" if side == "buy" else "used_sell"
    conn.execute(f"UPDATE paper_quotes SET {column}={column}+? WHERE quote_key=?", (quantity, q["key"]))
    conn.execute("UPDATE paper_decisions SET status='filled',block_reason='' WHERE id=?", (decision["id"],))
    payload["id"] = cursor.lastrowid
    return payload


def process_pending(conn, mode, quotes, session):
    """Process a quote batch atomically. Missing prerequisites return blocked HOLD."""
    _mode(mode)
    if isinstance(quotes, dict):
        quotes = list(quotes.values())  # latest_snapshots(conn,mode) compatibility
    if not isinstance(quotes, (list, tuple)):
        raise ValueError("quotes must be a list or symbol-to-quote mapping")
    summary = {"mode": mode, "status": "hold", "filled": 0, "rejected": 0, "pending": 0,
               "fills": [], "blocked": [], "simulation_only": True}
    with _atomic(conn):
        account = _account(conn, mode)
        config = json.loads(account["config"])
        before_events = conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM paper_exit_events").fetchone()["n"]
        try:
            cal, as_of, clock, day = _session(conn, mode, session, account)
        except (ValueError, TypeError, KeyError) as exc:
            summary["blocked"].append({"reason": str(exc)})
            summary["pending"] = conn.execute("SELECT COUNT(*) AS n FROM paper_decisions WHERE mode=? AND status='pending'", (mode,)).fetchone()["n"]
            return summary
        parsed = []
        for raw in quotes:
            try:
                q = _quote(raw, mode, cal, as_of, config)
                if any(lot["exchange"] != q["exchange"] for lot in _lots(conn, mode, q["symbol"])):
                    raise ValueError("quote_exchange_position_mismatch")
                if account["watermark"] and q["observed"] < _time(account["watermark"]):
                    raise ValueError("quote_predates_processed_clock")
                parsed.append((q, raw))
            except (ValueError, TypeError, KeyError) as exc:
                summary["blocked"].append({"symbol": raw.get("symbol") if isinstance(raw, dict) else None, "reason": str(exc)})
        unique = {}
        for q, raw in parsed:
            if q["key"] in unique and _hash(unique[q["key"]][1]) != _hash(raw):
                raise ValueError("quote_id reused with changed content")
            unique[q["key"]] = (q, raw)
        parsed = sorted(unique.values(), key=lambda pair: (pair[0]["observed"], pair[0]["symbol"], pair[0]["key"]))
        # Mark every symbol at a given timestamp before decisions at that timestamp;
        # never use any mark from later in the batch.
        for q, raw in parsed:
            digest = _hash(raw)
            prior = conn.execute("SELECT digest FROM paper_quotes WHERE quote_key=?", (q["key"],)).fetchone()
            if prior is not None and prior["digest"] != digest:
                raise ValueError("quote_id reused with changed content")
            conn.execute("INSERT OR IGNORE INTO paper_quotes(quote_key,mode,symbol,observed_at,digest,payload) VALUES(?,?,?,?,?,?)",
                         (q["key"], mode, q["symbol"], _stamp(q["observed"]), digest, _json(raw)))
        paper_exits.quarantine_ambiguous_history(conn, mode, _stamp(as_of))
        for observed in sorted({q["observed"] for q, raw in parsed}):
            group = [(q, raw) for q, raw in parsed if q["observed"] == observed]
            # Conflicting same-time executable quotes do not have a knowable ordering.
            counts = {}
            for q, raw in group:
                counts[q["symbol"]] = conn.execute(
                    "SELECT COUNT(*) AS n FROM paper_quotes WHERE mode=? AND symbol=? AND observed_at=?",
                    (mode, q["symbol"], _stamp(observed))).fetchone()["n"]
            group = [(q, raw) for q, raw in group if counts[q["symbol"]] == 1]
            for symbol, count in counts.items():
                if count > 1:
                    summary["blocked"].append({"symbol": symbol, "reason": "ambiguous_same_timestamp_quotes"})
            for q, raw in group:
                conn.execute("""INSERT INTO paper_marks VALUES(?,?,?,?,?,?) ON CONFLICT(mode,symbol) DO UPDATE SET
                    price=excluded.price,observed_at=excluded.observed_at,exchange=excluded.exchange,quote_key=excluded.quote_key""",
                    (mode, q["symbol"], str(q["bid"]), _stamp(observed), q["exchange"], q["key"]))
            for q, raw in group:
                decisions = conn.execute("""SELECT * FROM paper_decisions WHERE mode=? AND symbol=? AND status='pending'
                    ORDER BY submitted_at,id""", (mode, q["symbol"])).fetchall()
                for decision in decisions:
                    if _time(decision["submitted_at"]) >= observed:
                        continue
                    try:
                        fill = _execute(conn, decision, q, cal, clock, config)
                        summary["fills"].append(fill)
                        summary["filled"] += 1
                    except ValueError as exc:
                        reason = str(exc)
                        terminal = reason in {"order_expired", "insufficient_cash_no_leverage", "single_security_cap_exceeded",
                                              "insufficient_position_no_shorting", "t_plus_one_locked_or_calendar_coverage_missing",
                                              "insufficient_cash_for_sell_fees"}
                        conn.execute("UPDATE paper_decisions SET status=?,block_reason=? WHERE id=?",
                                     ("rejected" if terminal else "pending", reason, decision["id"]))
                        summary["rejected"] += int(terminal)
                        summary["blocked"].append({"decision_id": decision["decision_id"], "reason": reason})
                paper_exits.evaluate(conn, q, cal)
        # Expire even with absent quotes; old decisions never spring into life days later.
        for decision in conn.execute("SELECT * FROM paper_decisions WHERE mode=? AND status='pending'", (mode,)).fetchall():
            if (as_of - _time(decision["submitted_at"])).total_seconds() > config["order_ttl_seconds"]:
                conn.execute("UPDATE paper_decisions SET status='rejected',block_reason='order_expired' WHERE id=?", (decision["id"],))
                summary["rejected"] += 1
        paper_exits.sync_all(conn, mode, _stamp(as_of))
        summary["exit_events"] = paper_exits.events(conn, mode, after=before_events)
        conn.execute("UPDATE paper_accounts SET watermark=?,clock=? WHERE mode=?", (_stamp(as_of), clock, mode))
        summary["pending"] = conn.execute("SELECT COUNT(*) AS n FROM paper_decisions WHERE mode=? AND status='pending'", (mode,)).fetchone()["n"]
        summary["status"] = "filled" if summary["filled"] else "hold"
        summary["as_of"], summary["clock"], summary["session_date"] = _stamp(as_of), clock, day
    return summary


def paper_state(conn, mode):
    account = _account(conn, mode)
    config = json.loads(account["config"])
    positions = {}
    for lot in _lots(conn, mode):
        position = positions.setdefault(lot["symbol"], {"symbol": lot["symbol"], "quantity": 0, "cost_cents": 0, "lots": []})
        position["quantity"] += lot["quantity"]
        position["cost_cents"] += lot["cost_cents"]
        position["lots"].append({"quantity": lot["quantity"], "buy_session": lot["buy_date"], "exchange": lot["exchange"],
                                 "calendar_id": lot["calendar_id"], "cost": _money(lot["cost_cents"])})
    market_value = 0
    all_marked, fresh = True, True
    now = datetime.now(timezone.utc)
    valuation_clock = _time(account["watermark"]) if account["clock"] == "replay" and account["watermark"] else now
    valuation_times = []
    for symbol, position in positions.items():
        mark = conn.execute("SELECT * FROM paper_marks WHERE mode=? AND symbol=?", (mode, symbol)).fetchone()
        if mark and (any(lot["exchange"] != mark["exchange"] for lot in position["lots"]) or
                     not _quote_history_is_unambiguous(conn, mode, symbol, mark["observed_at"])):
            mark = None
        if mark:
            evidence = conn.execute("SELECT payload FROM paper_quotes WHERE quote_key=?", (mark["quote_key"],)).fetchone()
            if evidence is None or _time(json.loads(evidence["payload"])["received_at"]) > valuation_clock:
                mark = None
        position["cost"] = _money(position.pop("cost_cents"))
        position["average_cost"] = round(position["cost"] / position["quantity"], 6)
        position["mark_price"] = float(mark["price"]) if mark else None
        position["marked_at"] = mark["observed_at"] if mark else None
        if mark:
            value = _cents(Decimal(mark["price"]) * position["quantity"], ROUND_FLOOR)
            market_value += value
            position["market_value"] = _money(value)
            age = (valuation_clock - _time(mark["observed_at"])).total_seconds()
            position["mark_fresh"] = 0 <= age <= config["max_quote_age_seconds"]
            fresh = fresh and position["mark_fresh"]
            valuation_times.append(mark["observed_at"])
        else:
            all_marked, fresh = False, False
            position["market_value"], position["mark_fresh"] = None, False
    decisions = [_decision(row) for row in conn.execute("SELECT * FROM paper_decisions WHERE mode=? ORDER BY id DESC LIMIT 200", (mode,)).fetchall()]
    fills = [dict(json.loads(row["payload"]), id=row["id"]) for row in conn.execute("SELECT * FROM paper_fills WHERE mode=? ORDER BY id DESC LIMIT 200", (mode,)).fetchall()]
    fees = conn.execute("SELECT COALESCE(SUM(fee_cents),0) AS n FROM paper_fills WHERE mode=?", (mode,)).fetchone()["n"]
    calendars = [{"calendar_id": row["calendar_id"], "provenance": json.loads(row["payload"])["provenance"],
                  "independently_verified": False} for row in conn.execute("SELECT * FROM paper_calendars WHERE mode=? ORDER BY calendar_id", (mode,)).fetchall()]
    equity = _money(account["cash_cents"] + market_value) if all_marked else None
    conflicts = [dict(row) for row in conn.execute("SELECT mode,symbol,observed_at,reason FROM paper_data_conflicts WHERE mode=? ORDER BY rowid", (mode,))]
    quality = {"data_conflicts": conflicts,
               "data_quality_warning": "历史输入发生时间戳歧义；既有假设成交未追溯重写，受影响计划需取消后复核"} if conflicts else {}
    return {**quality, "mode": mode, "currency": "CNY", "initial_capital": _money(account["initial_cents"]),
            "cash": _money(account["cash_cents"]), "market_value": _money(market_value) if all_marked else None,
            "equity": equity, "equity_is_current": bool(all_marked and fresh),
            "valuation_as_of": min(valuation_times) if valuation_times else None,
            "valuation_basis": "replay_simulation_clock" if account["clock"] == "replay" else "wall_clock",
            "realized_pnl": _money(account["realized_cents"]), "fees_paid": _money(fees),
            "total_pnl": round(equity - _money(account["initial_cents"]), 2) if equity is not None else None,
            "positions": list(positions.values()), "decisions": decisions, "fills": fills,
            "exit_plans": paper_exits.plan_state(conn, mode), "exit_events": paper_exits.events(conn, mode),
            "exit_disclaimer": paper_exits.EXIT_DISCLAIMER,
            "pending_count": conn.execute("SELECT COUNT(*) AS n FROM paper_decisions WHERE mode=? AND status='pending'", (mode,)).fetchone()["n"],
            "clock": account["clock"], "processed_as_of": account["watermark"], "config": config,
            "calendars": calendars, "status": "ready_for_explicit_quotes" if calendars else "hold_missing_calendar",
            "track_record": "synthetic_demo" if mode == "demo" else "imported_hypothetical_paper",
            "simulation_only": True, "ai_api_integrated": False, "broker_connected": False, "disclaimer": DISCLAIMER}
