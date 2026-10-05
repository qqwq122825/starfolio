"""Private daily-bar research trial. Never produces paper/execution quotes.

Tushare daily's documented units: price CNY, vol lots of 100 shares,
amount thousands of CNY; unadjusted OHLC. No external dependencies.
"""
import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
SOURCE = 'https://tushare.pro/document/2?doc_id=27'
CALENDAR_SOURCE = 'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml'
ENDPOINT = 'https://api.tushare.pro'
FIELDS = 'ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount'
BLOCKERS = ['daily_bars_have_no_bid_ask_sizes_or_trading_status']
# Official SSE published schedule, including weekend closures. Never extrapolated.
HOLIDAYS = [('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),
            ('2026-04-04','2026-04-06'),('2026-05-01','2026-05-05'),
            ('2026-06-19','2026-06-21'),('2026-09-25','2026-09-27'),
            ('2026-10-01','2026-10-07')]


class SourceError(ValueError):
    """Sanitized error code only; never includes upstream bodies or tokens."""


def aware_now(now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise SourceError('timezone_required')
    return now.astimezone(TZ)


def is_session(day):
    if day.year != 2026:
        raise SourceError('calendar_out_of_coverage')
    return day.weekday() < 5 and not any(date.fromisoformat(a) <= day <= date.fromisoformat(b) for a,b in HOLIDAYS)


def expected_session(now=None):
    now = aware_now(now)
    day = now.date()
    if day.year != 2026:
        raise SourceError('calendar_out_of_coverage')
    # Daily docs say 15–16h; permission overview says 15–17h. Use 17h conservatively.
    if now.time() < time(17):
        day -= timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day


def universe(symbols):
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 3:
        raise SourceError('choose_one_to_three_shanghai_symbols')
    if any(not isinstance(s,str) or not re.fullmatch(r'6\d{5}\.SH',s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise SourceError('trial_requires_unique_shanghai_a_share_codes')
    return symbols


def numeric(v, positive=False):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or (v <= 0 if positive else v < 0):
        raise SourceError('invalid_bar_numeric_value')
    return float(v)


def normalize(rows, symbol, start, end, fetched_at):
    if not isinstance(rows,list) or len(rows) > 121:
        raise SourceError('invalid_bar_count')
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row,dict) or row.get('ts_code') != symbol:
            raise SourceError('unexpected_symbol')
        try:
            raw_date = row['trade_date']
            if not isinstance(raw_date,str) or not re.fullmatch(r'\d{8}',raw_date):
                raise ValueError()
            day = datetime.strptime(raw_date,'%Y%m%d').date()
            prices = {k:numeric(row[k],True) for k in ('open','high','low','close','pre_close')}
            volume, amount = numeric(row['vol']), numeric(row['amount'])
            change = row['pct_chg']
            if isinstance(change,bool) or not isinstance(change,(int,float)) or not math.isfinite(change):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise SourceError('malformed_daily_bar') from None
        if not start <= day <= end or day in seen or not is_session(day):
            raise SourceError('invalid_or_duplicate_trade_date')
        if prices['low'] > min(prices['open'],prices['close']) or prices['high'] < max(prices['open'],prices['close']) or prices['low'] > prices['high']:
            raise SourceError('inconsistent_ohlc')
        if not math.isfinite(volume*100) or not math.isfinite(amount*1000):
            raise SourceError('unit_conversion_overflow')
        seen.add(day)
        result.append(dict(prices, symbol=symbol,trade_date=day.isoformat(),
            period_end_at=datetime.combine(day,time(15),TZ).isoformat(),
            timestamp_basis='inferred_exchange_daily_period_end_not_source_tick',
            fetched_at=fetched_at,adjustment='none',volume_shares=volume*100,
            turnover_cny=amount*1000,change_pct=float(change),
            change_pct_basis='provider_uses_ex_rights_previous_close',
            source='Tushare daily',source_url=SOURCE,large_order_net=None,
            execution_eligible=False))
    return sorted(result,key=lambda b:b['trade_date'])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError('https_redirect_rejected')


class TushareDaily:
    """Requires user-configured token; HTTPS-only, no retries or fallback hosts."""
    def __init__(self, token=None, opener=None):
        self._token = token if token is not None else os.environ.get('TUSHARE_TOKEN','')
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def fetch(self, symbol, start, end):
        if not self._token:
            raise SourceError('missing_token_user_secure_setup_required')
        request = urllib.request.Request(ENDPOINT, method='POST',
            headers={'Content-Type':'application/json','User-Agent':'Starfolio-private-research/1'},
            data=json.dumps({'api_name':'daily','token':self._token,
                             'params':{'ts_code':symbol,'start_date':start.strftime('%Y%m%d'),'end_date':end.strftime('%Y%m%d')},
                             'fields':FIELDS}).encode())
        try:
            with self._opener.open(request,timeout=15) as response:
                if response.geturl() != ENDPOINT:
                    raise SourceError('https_redirect_rejected')
                raw = response.read(1_000_001)
            if len(raw)>1_000_000:
                raise SourceError('provider_response_too_large')
            payload = json.loads(raw)
        except SourceError:
            raise
        except urllib.error.HTTPError as exc:
            raise SourceError('https_http_'+str(exc.code)) from None
        except Exception:
            raise SourceError('https_transport_or_json_failed') from None
        if not isinstance(payload,dict) or type(payload.get('code')) is not int or payload.get('code') != 0:
            raise SourceError('provider_permission_quota_or_service_error')
        data = payload.get('data')
        if not isinstance(data,dict) or not isinstance(data.get('fields'),list) or not isinstance(data.get('items'),list):
            raise SourceError('invalid_provider_schema')
        fields = data['fields']
        if any(not isinstance(field,str) for field in fields) or len(fields)!=len(set(fields)) or not set(FIELDS.split(',')).issubset(fields):
            raise SourceError('missing_or_duplicate_provider_fields')
        if len(data['items']) > 121 or any(not isinstance(row,list) or len(row)!=len(fields) for row in data['items']):
            raise SourceError('invalid_provider_rows')
        return [dict(zip(fields,row)) for row in data['items']]


def init(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS daily_research_runs(id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)')


def collect(conn, symbols, days=60, provider=None, now=None):
    init(conn)
    current = aware_now(now)
    result = {'status':'failed','provider':'Tushare daily','checked_at':current.isoformat(),
        'execution_eligible':False,'execution_blockers':BLOCKERS.copy(),
        'calendar':{'exchange':'SSE','source_url':CALENDAR_SOURCE,'coverage':'2026',
            'basis':'published_schedule_weekends_and_explicit_holidays','unscheduled_closures_verified':False},
        'license_scope':'private_personal_research_no_redistribution',
        'symbols':{}}
    try:
        universe(symbols)
        if isinstance(days,bool) or not isinstance(days,int) or not 1 <= days <= 120:
            raise SourceError('lookback_must_be_1_to_120_calendar_days')
        end = expected_session(current)
        start = max(date(2026,1,1),end-timedelta(days=days-1))
        result['expected_bar_date']=end.isoformat()
        provider = provider or TushareDaily()
        for symbol in symbols:
            try:
                rows = provider.fetch(symbol,start,end)
                fetched = aware_now(now).isoformat()  # actual response receipt in production
                bars = normalize(rows,symbol,start,end,fetched)
                latest = bars[-1]['trade_date'] if bars else None
                result['symbols'][symbol]={'status':'current_daily' if latest==end.isoformat() else 'missing' if latest is None else 'stale',
                    'latest_bar_date':latest,'expected_bar_date':end.isoformat(),'bars':bars,
                    'note':'Missing sessions may reflect suspension or source gaps; no suspension inference'}
            except SourceError as exc:
                result['symbols'][symbol]={'status':'failed','error':str(exc),'bars':[]}
            except Exception:
                result['symbols'][symbol]={'status':'failed','error':'provider_unexpected_failure','bars':[]}
        statuses = [v['status'] for v in result['symbols'].values()]
        result['status']='ok' if all(s=='current_daily' for s in statuses) else 'failed' if all(s=='failed' for s in statuses) else 'degraded'
    except SourceError as exc:
        result['error']=str(exc)
    conn.execute('INSERT INTO daily_research_runs(checked_at,payload) VALUES(?,?)',(result['checked_at'],json.dumps(result)))
    conn.commit()
    return result


def state(conn, now=None):
    init(conn)
    row = conn.execute('SELECT payload FROM daily_research_runs ORDER BY id DESC LIMIT 1').fetchone()
    if row is None:
        return {'status':'not_configured','execution_eligible':False,'execution_blockers':BLOCKERS.copy(),'symbols':{}}
    result = json.loads(row[0])
    result['evaluated_at']=aware_now(now).isoformat()
    try:
        expected=expected_session(now).isoformat()
        for item in result['symbols'].values():
            item['expected_bar_date']=expected
            if item['status'] in ('current_daily','stale'):
                item['status']='current_daily' if item['latest_bar_date']==expected else 'stale'
                item['expected_bar_date']=expected
        if result['status']=='ok' and any(v['status']!='current_daily' for v in result['symbols'].values()):
            result['status']='degraded'
        result['expected_bar_date']=expected
    except SourceError:
        result['status']='degraded'
        result['error']='calendar_out_of_coverage'
        for item in result['symbols'].values():
            if item['status']=='current_daily': item['status']='calendar_unknown'
    return result
