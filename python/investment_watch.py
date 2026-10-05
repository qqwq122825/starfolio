#!/usr/bin/env python3
"""Investment Watch: local, read-only decision-support prototype (stdlib only)."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import paper

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data' / 'watch.db'
TZ = ZoneInfo('Asia/Shanghai')
RULE_VERSION = 'evidence-v1'
MAX_AGE_MINUTES = 20
MODES = {'demo', 'imported', 'live'}
STATUSES = {'new', 'reviewed', 'dismissed'}
STAGES = {'unknown', 'application', 'pilot', 'order', 'revenue', 'other'}
STAGE_LABELS = {'unknown':'未知/证据不足', 'application':'应用/框架', 'pilot':'验证/试点', 'order':'明确订单', 'revenue':'已确认收入', 'other':'其他/未明确'}
EXPECTATION_STATUSES = {'unknown', 'above', 'in_line', 'below', 'priced_in'}
PRICE_REACTIONS = {'unknown', 'up', 'flat', 'down'}
NEWS_ANALYSIS_DISCLAIMER = ('仅记录研究者手工输入；事实阶段、相对预期与价格反应互不证明。'
    '价格上涨不能证明订单或收入；已有订单/收入也不能证明超预期或尚未计价。'
    '本程序不独立核实原文、不自动判断预期、不生成买卖指令。')
DEMO_WATCH = [('DEMO01','示例芯片'),('DEMO02','示例电网'),('DEMO03','示例制造'),('DEMO04','示例消费')]

def utcnow():
    return datetime.now(timezone.utc)

def stamp(dt=None):
    return (dt or utcnow()).isoformat(timespec='seconds')

def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('时间必须是带时区的 ISO 8601 字符串')
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间缺少时区')
    return dt.astimezone(timezone.utc)

def finite_number(value, field, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value):
        raise ValueError(f'{field} 必须是有限数值')
    if minimum is not None and value < minimum:
        raise ValueError(f'{field} 不可小于 {minimum}')
    return float(value)

def safe_url(value, optional=False):
    if not value and optional:
        return ''
    p = urlparse(str(value))
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('来源链接必须是无凭据的 HTTPS 网址')
    return str(value)[:1500]

def symbol_valid(symbol, demo=False):
    return bool(re.fullmatch(r'\d{6}', symbol) or (demo and re.fullmatch(r'DEMO0[1-4]', symbol)))

def connect(db=DEFAULT_DB):
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER DEFAULT 1, added_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, mode TEXT NOT NULL, observed_at TEXT NOT NULL, fetched_at TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(symbol,mode,observed_at,source));
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, score INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL, stage TEXT NOT NULL, verification TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS portfolio(symbol TEXT PRIMARY KEY, name TEXT NOT NULL, shares REAL NOT NULL, cost REAL NOT NULL, imported_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(mode,symbol,observed_at);
    ''')
    if not conn.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
        for symbol, name in DEMO_WATCH:
            conn.execute('INSERT OR IGNORE INTO watchlist VALUES(?,?,1,?)',(symbol,name,stamp()))
        conn.executemany('INSERT OR IGNORE INTO settings VALUES(?,?)',[('initialized','true'),('mode','demo')])
        conn.commit()
    paper.init_paper(conn)
    migrate_news(conn)
    conn.commit()
    return conn

def audit(conn, action, target, detail):
    conn.execute('INSERT INTO audit(at,action,target,detail) VALUES(?,?,?,?)',(stamp(),action,target,json.dumps(detail,ensure_ascii=False)))

def setting(conn,key,default=''):
    row=conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return row['value'] if row else default

def set_setting(conn,key,value):
    conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

def freshness(snapshot, now=None):
    now=now or utcnow()
    age=(now-parse_time(snapshot['observed_at'])).total_seconds()/60
    if age < -2:
        return {'state':'invalid','age_minutes':round(age,1),'label':'数据时间在未来 · 禁止触发'}
    if snapshot['mode']=='demo':
        return {'state':'demo','age_minutes':max(0,round(age,1)),'label':'模拟样本 · 非实时行情'}
    return {'state':'fresh' if age<=MAX_AGE_MINUTES else 'stale','age_minutes':max(0,round(age,1)),'label':'时间窗内' if age<=MAX_AGE_MINUTES else '数据已过期 · 禁止触发'}

def validate_snapshot(data, mode='imported'):
    if mode not in MODES:
        raise ValueError('未知模式')
    s = dict(data)
    s['mode']=mode
    if not symbol_valid(str(s.get('symbol','')),mode=='demo'):
        raise ValueError('证券代码必须为六位数字')
    s['observed_at']=stamp(parse_time(s.get('observed_at')))
    if parse_time(s['observed_at']) > utcnow()+timedelta(minutes=2):
        raise ValueError('不接受未来时间的行情')
    s['source']=str(s.get('source','')).strip()[:150]
    if not s['source']:
        raise ValueError('每条行情必须标明来源')
    s['source_url']=safe_url(s.get('source_url'),optional=mode=='demo')
    for field in ('price','turnover','volume'):
        s[field]=finite_number(s.get(field),field,0)
    s['change_pct']=finite_number(s.get('change_pct'), 'change_pct')
    if s.get('large_order_net') is not None:
        s['large_order_net']=finite_number(s['large_order_net'],'large_order_net')
        if not s.get('flow_definition'):
            raise ValueError('资金流字段必须提供供应商统计口径 flow_definition')
        if abs(s['large_order_net']) > s['turnover']:
            raise ValueError('大单净额绝对值不可超过成交额，请核对单位')
    # A baseline must use the same elapsed trading window and units as this snapshot.
    history=s.get('baseline',[])
    if not isinstance(history,list) or len(history)>120:
        raise ValueError('baseline 必须是最多 120 项数组')
    s['baseline']=[finite_number(v,'baseline',0) for v in history]
    if history and not s.get('baseline_basis'):
        raise ValueError('有基线时必须说明 baseline_basis（同交易时段/复权/单位）')
    s['fetched_at']=stamp()
    s['series']=[finite_number(v,'series',0) for v in s.get('series',[])[:60]]
    return s

def anomaly(s, now=None):
    fresh=freshness(s,now)
    if fresh['state'] not in {'fresh','demo'}:
        return None
    baseline=s.get('baseline',[])
    if len(baseline)<20 or s['turnover']<=0:
        return None
    mean=sum(baseline)/len(baseline)
    variance=sum((x-mean)**2 for x in baseline)/len(baseline)
    std=math.sqrt(variance)
    if mean<=0 or std<=0:
        return None
    ratio=s['turnover']/mean
    z=(s['turnover']-mean)/std
    flow=s.get('large_order_net')
    flow_ratio=flow/s['turnover'] if flow is not None else None
    score=0
    evidence=[]
    if ratio>=1.8 and z>=2.5:
        score+=40
        evidence.append(f'同时间窗成交额为 {len(baseline)} 个历史样本均值的 {ratio:.2f} 倍；Z={z:.2f}')
    if flow_ratio is not None and abs(flow_ratio)>=.08:
        score+=30
        evidence.append(f'供应商大单净额/成交额为 {flow_ratio:+.1%}（{s["flow_definition"]}）')
    if flow_ratio is not None and ((flow_ratio>0 and s['change_pct']<0) or (flow_ratio<0 and s['change_pct']>0)):
        score+=15
        evidence.append(f'价格涨跌 {s["change_pct"]:+.2f}% 与大单统计方向相反')
    if score<55:
        return None
    direction='流入' if flow_ratio is not None and flow_ratio>0 else '流出'
    return {'score':score,'title':f'量能放大与大单{direction}同时出现','evidence':evidence,
        'volume_ratio':round(ratio,2),'z_score':round(z,2),'flow_ratio':round(flow_ratio,4) if flow_ratio is not None else None,
        'snapshot_at':s['observed_at'],'source':s['source'],'source_url':s['source_url'],
        'rule_version':RULE_VERSION,'hypothesis':'可观察到交易活跃度与大单统计异常；无法据此识别真实机构身份、吸筹或洗盘意图。',
        'counter_evidence':['指数或板块整体波动可能造成共同异动','大宗交易、调仓、开收盘集中成交或数据分类差异可产生相似信号'],
        'invalidation':['后续同时间窗成交额比值降到 1.2 以下','大单净额方向反转或供应商修订统计','数据超过 20 分钟、基线不可比或来源不可用'],
        'question':'是否加入人工复核清单，并在核对公告与自身风险约束后，再考虑是否调整仓位？',
        'disclaimer':'规则关注分，不是上涨概率、收益预测或买卖指令'}

def save_alert(conn,symbol,mode,kind,payload,event_key):
    fp=alert_fingerprint(symbol,mode,kind,event_key)
    now=stamp()
    existing=conn.execute('SELECT * FROM alerts WHERE fingerprint=?',(fp,)).fetchone()
    if existing:
        # Preserve reviewed/dismissed state; do not send another alert for the same event.
        old=json.loads(existing['payload'])
        if old!=payload:
            conn.execute('UPDATE alerts SET payload=?,score=?,title=?,updated_at=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),payload['score'],payload['title'],now,existing['id']))
        return False
    conn.execute('INSERT INTO alerts(fingerprint,symbol,mode,kind,title,score,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?)',
        (fp,symbol,mode,kind,payload['title'],payload['score'],now,now,json.dumps(payload,ensure_ascii=False)))
    audit(conn,'alert_created',symbol,{'mode':mode,'kind':kind,'event':event_key})
    return True

def alert_fingerprint(symbol,mode,kind,event_key):
    return hashlib.sha256(f'{mode}|{symbol}|{kind}|{event_key}|{RULE_VERSION}'.encode()).hexdigest()

def store_snapshot(conn,s):
    conn.execute('INSERT INTO snapshots(symbol,mode,observed_at,fetched_at,source,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,mode,observed_at,source) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload',
        (s['symbol'],s['mode'],s['observed_at'],s['fetched_at'],s['source'],json.dumps(s,ensure_ascii=False)))

def demo_data(now=None):
    now=now or utcnow()
    out=[]
    for i,(symbol,name) in enumerate(DEMO_WATCH):
        baseline=[(1+(j%7-3)*.047)*1e8 for j in range(20)]
        turnover=[2.43e8,2.05e8,1.16e8,.94e8][i]
        out.append(validate_snapshot({'symbol':symbol,'price':[38.62,21.35,15.08,46.70][i], 'change_pct':[-1.32,2.14,.46,-.73][i],
            'volume':turnover/[38.62,21.35,15.08,46.70][i],'turnover':turnover,'large_order_net':[3.41e7,-2.25e7,4e6,-3e6][i],
            'observed_at':stamp(now),'source':'内置演示样本（虚构）','source_url':'','flow_definition':'演示：按单笔成交金额分组的买卖方向净额，非机构资金',
            'baseline':baseline,'baseline_basis':'虚构的前 20 个交易日相同累计时段；人民币元',
            'series':[100+math.sin(j*.7+i)*.45+j*(.075 if i==1 else -.03) for j in range(24)]},'demo'))
    return out

def seed_demo_news(conn):
    samples=[{'id':'demo-framework','symbol':'DEMO01','title':'示例芯片签署应用合作框架，未披露订单金额','stage':'application','verification':'demo','note':'仅说明应用方向；没有采购承诺、订单金额或收入确认依据。','amount':None},
        {'id':'demo-order','symbol':'DEMO02','title':'示例电网披露采购合同，交付与回款仍有条件','stage':'order','verification':'demo','note':'虚构合同样本：金额与履约条件较明确，但订单不等于已确认收入。','amount':'虚构金额 1.2 亿元'},
        {'id':'demo-rumor','symbol':'DEMO03','title':'网传示例制造进入新业务，原始出处尚缺失','stage':'other','verification':'demo','note':'缺少公告、原文和可核实出处，不能升级为实质利好。','amount':None}]
    for n in samples:
        n.update({'mode':'demo','published_at':stamp(),'source':'虚构新闻样本','source_url':'','reviewed_by':'','evidence_excerpt':'仅供展示，不代表任何真实公司或事件'})
        conn.execute('INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?,?)',(n['id'],n['symbol'],'demo',n['title'],n['published_at'],n['stage'],n['verification'],json.dumps(n,ensure_ascii=False)))

def latest_snapshots(conn,mode):
    rows=conn.execute('SELECT payload FROM snapshots WHERE mode=? ORDER BY observed_at DESC,id DESC',(mode,)).fetchall()
    latest={}
    for row in rows:
        s=json.loads(row['payload'])
        latest.setdefault(s['symbol'],s)
    return latest

def run_monitor(conn, mode, provider=None, attempts=3, backoff=.3, now=None):
    if mode not in MODES:
        raise ValueError('未知模式')
    now=now or utcnow()
    started=stamp()
    run=conn.execute('INSERT INTO runs(mode,started_at,status,detail) VALUES(?,?,?,?)',(mode,started,'running','运行中')).lastrowid
    conn.commit()
    new_count=0
    try:
        if provider:
            last=None
            for attempt in range(attempts):
                try:
                    snapshots=provider()
                    last=None
                    break
                except Exception as exc:
                    last=exc
                    if attempt+1<attempts:
                        time.sleep(backoff*(2**attempt))
            if last:
                raise RuntimeError(f'数据源 {attempts} 次尝试失败：{last}')
            snapshots=[validate_snapshot(s,mode) for s in snapshots]
        elif mode=='demo':
            snapshots=demo_data(now)
            seed_demo_news(conn)
        elif mode=='imported':
            snapshots=list(latest_snapshots(conn,mode).values())
        else:
            raise RuntimeError('实时行情适配器尚未配置；未自动回退到模拟数据')
        enabled={r['symbol'] for r in conn.execute('SELECT symbol FROM watchlist WHERE enabled=1')}
        stale=0
        incomplete=0
        scanned=0
        for s in snapshots:
            if s['symbol'] not in enabled:
                continue
            scanned+=1
            store_snapshot(conn,s)
            if freshness(s,now)['state'] in {'stale','invalid'}:
                stale+=1
                continue
            result=anomaly(s,now)
            if result:
                day=parse_time(s['observed_at']).astimezone(TZ).date().isoformat()
                new_count+=save_alert(conn,s['symbol'],mode,'flow',result,day)
            elif len(s.get('baseline',[]))<20:
                incomplete+=1
        status='degraded' if stale or incomplete or not scanned else 'ok'
        detail=f'检查 {scanned} 个标的；新增 {new_count} 条异常；{stale} 条过期；{incomplete} 条基线不足'
        if not scanned:
            detail+='；无可分析数据'
        conn.execute('UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?',(stamp(),status,detail,run))
        set_setting(conn,'mode',mode)
        audit(conn,'monitor_run',str(run),{'mode':mode,'status':status,'new_alerts':new_count})
        conn.commit()
        return {'status':status,'detail':detail,'new_alerts':new_count,'run_id':run}
    except Exception as exc:
        conn.rollback()
        message=str(exc)[:500]
        conn.execute('UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?',(stamp(),'failed',message,run))
        set_setting(conn,'mode',mode)
        audit(conn,'monitor_failed',str(run),{'mode':mode,'error':message})
        conn.commit()
        return {'status':'failed','detail':message,'new_alerts':0,'run_id':run}

def import_market(conn,items):
    if not isinstance(items,list) or not items or len(items)>100:
        raise ValueError('行情文件必须是包含 1–100 项的 JSON 数组')
    items=[validate_snapshot(s,'imported') for s in items]
    for s in items:
        store_snapshot(conn,s)
    audit(conn,'market_import',str(len(items)),{'source_count':len({s['source'] for s in items})})
    conn.commit()
    return len(items)

def import_portfolio(conn,text):
    rows=list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
    if not rows or len(rows)>200:
        raise ValueError('CSV 必须有 1–200 行，表头为 symbol,name,shares,cost')
    validated=[]
    seen=set()
    for r in rows:
        if set(r)!={'symbol','name','shares','cost'}:
            raise ValueError('CSV 表头必须恰好为 symbol,name,shares,cost；不要包含账户或身份信息')
        symbol=r['symbol'].strip()
        if not symbol_valid(symbol) or symbol in seen:
            raise ValueError('证券代码须为六位数字且不可重复')
        seen.add(symbol)
        name=r['name'].strip()[:50]
        try:
            shares=finite_number(float(r['shares']),'shares',0)
            cost=finite_number(float(r['cost']),'cost',0)
        except (TypeError,ValueError):
            raise ValueError('shares 和 cost 必须是非负有限数字')
        validated.append((symbol,name,shares,cost,stamp()))
    # Validate the complete batch before the atomic replacement.
    with conn:
        conn.execute('DELETE FROM portfolio')
        conn.executemany('INSERT INTO portfolio VALUES(?,?,?,?,?)',validated)
        audit(conn,'portfolio_import','local_csv',{'count':len(validated)})
    return len(validated)

def news_research_assessment(n):
    """A conservative research gate, never a paper-account decision or signal."""
    missing=[]
    if n.get('stage', 'unknown') in {'unknown', 'other'} or not n.get('evidence_excerpt'):
        missing.append('factual_stage_evidence')
    if n.get('verification')!='reviewed' or not n.get('reviewed_by'):
        missing.append('human_source_review')
    if n.get('expectation_status', 'unknown')=='unknown' or not n.get('expectation_evidence'):
        missing.append('expectation_evidence')
    if n.get('price_reaction', 'unknown')=='unknown' or not n.get('price_reaction_evidence'):
        missing.append('price_reaction_evidence')
    return {'action':'HOLD','status':'insufficient_evidence' if missing else 'manual_review_only',
        'missing_fields':missing,
        'reason':'证据或分类未知，保持 HOLD，等待人工研究；不会据价格反推事实。' if missing else
            '人工证据已记录，仍保持 HOLD；不自动推导买卖或已验证收益优势。',
        'disclaimer':NEWS_ANALYSIS_DISCLAIMER}

def news_with_analysis(data):
    """Default legacy records to unknown; never derive classifications from prices."""
    n=dict(data)
    for field, default in [('expectation_status','unknown'),('expectation_evidence',''),
                           ('price_reaction','unknown'),('price_reaction_evidence','')]:
        n.setdefault(field,default)
    for field, choices in [('expectation_status',EXPECTATION_STATUSES),('price_reaction',PRICE_REACTIONS)]:
        if not isinstance(n[field],str) or n[field] not in choices:
            n[field]='unknown'
    for field in ('expectation_evidence','price_reaction_evidence'):
        if not isinstance(n[field],str):
            n[field]=''
    n['analysis_schema_version']=2
    n['research_assessment']=news_research_assessment(n)
    return n

def migrate_news(conn):
    # JSON extension only: original factual fields and original provenance stay intact.
    if setting(conn,'news_analysis_schema')=='2':
        return
    for row in conn.execute('SELECT id,payload FROM news').fetchall():
        n=news_with_analysis(json.loads(row['payload']))
        conn.execute('UPDATE news SET payload=? WHERE id=?',(json.dumps(n,ensure_ascii=False),row['id']))
        alert=conn.execute('SELECT id,payload FROM alerts WHERE fingerprint=?',
            (alert_fingerprint(n['symbol'],n['mode'],'news',row['id']),)).fetchone()
        if alert:
            payload=json.loads(alert['payload'])
            payload['research_assessment']=n['research_assessment']
            conn.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),alert['id']))
    set_setting(conn,'news_analysis_schema','2')

def add_news(conn,data):
    if not isinstance(data,dict):
        raise ValueError('消息记录必须是对象')
    n=dict(data)
    symbol=str(n.get('symbol',''))
    if not symbol_valid(symbol):
        raise ValueError('新闻证券代码必须为六位数字')
    title=str(n.get('title','')).strip()[:200]
    if not title:
        raise ValueError('标题不能为空')
    stage=n.get('stage','unknown')
    if not isinstance(stage,str) or stage not in STAGES:
        raise ValueError('无效事件阶段')
    url=safe_url(n.get('source_url'))
    published=stamp(parse_time(n.get('published_at',stamp())))
    if parse_time(published)>utcnow()+timedelta(minutes=2):
        raise ValueError('新闻发布时间不可在未来')
    verification=n.get('verification','unverified')
    if not isinstance(verification,str) or verification not in {'unverified','linked','reviewed'}:
        raise ValueError('无效核验状态')
    def evidence_text(field):
        value=n.get(field,'')
        if not isinstance(value,str) or len(value)>3000:
            raise ValueError(f'{field} 必须是最多 3000 字符的证据文本')
        return value.strip()
    excerpt=evidence_text('evidence_excerpt')
    reviewer=str(n.get('reviewed_by','')).strip()[:60]
    if stage not in {'unknown','other'} and not excerpt:
        raise ValueError('标记事实阶段必须提供原文依据；价格反应不能作为事实证明')
    if verification=='reviewed' and (not excerpt or not reviewer):
        raise ValueError('人工核验须填写核验人及原文依据；链接本身不等于核验')
    expectation=n.get('expectation_status','unknown')
    reaction=n.get('price_reaction','unknown')
    if not isinstance(expectation,str) or expectation not in EXPECTATION_STATUSES or not isinstance(reaction,str) or reaction not in PRICE_REACTIONS:
        raise ValueError('无效预期或价格反应分类；未知时请选择 unknown')
    expectation_evidence=evidence_text('expectation_evidence')
    reaction_evidence=evidence_text('price_reaction_evidence')
    if expectation!='unknown' and not expectation_evidence:
        raise ValueError('非未知预期分类必须填写比较基准、时间与证据；不能仅凭涨跌推断')
    if reaction!='unknown' and not reaction_evidence:
        raise ValueError('非未知价格反应必须填写观察窗口、来源与证据')
    # Explicit field allowlist: a submitted assessment/proof cannot override the gate.
    n=news_with_analysis({'id':hashlib.sha256(f'{symbol}|{url}|{title}'.encode()).hexdigest()[:24], 'symbol':symbol,'title':title,'mode':'imported','stage':stage,'source_url':url,'source':str(n.get('source','手动录入'))[:150], 'published_at':published,'verification':verification,'evidence_excerpt':excerpt,'reviewed_by':reviewer,'note':str(n.get('note',''))[:1000], 'amount':str(n.get('amount',''))[:80],
        'expectation_status':expectation,'expectation_evidence':expectation_evidence,
        'price_reaction':reaction,'price_reaction_evidence':reaction_evidence})
    conn.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET published_at=excluded.published_at,stage=excluded.stage,verification=excluded.verification,payload=excluded.payload', (n['id'],symbol,'imported',title,published,stage,verification,json.dumps(n,ensure_ascii=False)))
    new=False
    eligible=False
    # Only an explicit human review can promote a material-event alert. It is still not an assertion of truth.
    if verification=='reviewed' and stage in {'order','revenue'}:
        age=(utcnow()-parse_time(published)).total_seconds()/3600
        if 0<=age<=72:
            eligible=True
            payload={'title':title,'score':70 if stage=='order' else 85,'evidence':[f'用户标记为人工核验：{reviewer}',excerpt,f'阶段：{STAGE_LABELS[stage]}；金额：{n["amount"] or "未填写"}'], 'source':n['source'],'source_url':url,'snapshot_at':published,'rule_version':RULE_VERSION,'hypothesis':'由人工核验记录触发；系统未独立验证原始文件或预测利润影响。','counter_evidence':['订单不等于收入；收入不等于利润或回款','核对附加条件、取消条款、关联交易与相对体量','事实阶段不代表超预期、尚未计价或未来上涨'], 'invalidation':['公告更正、合同取消、交付或回款条件不满足','原文证据不支持已标记阶段'], 'question':'是否复核该事件对现有投资逻辑的影响，再决定是否考虑调整仓位？','disclaimer':'事件关注分，不是收益概率或交易指令','research_assessment':n['research_assessment']}
            payload['event_currently_eligible']=True
            new=save_alert(conn,symbol,'imported','news',payload,n['id'])
    if not eligible:
        prior=conn.execute('SELECT payload FROM alerts WHERE fingerprint=?',
            (alert_fingerprint(symbol,'imported','news',n['id']),)).fetchone()
        if prior:
            # Preserve history/review status, but never leave withdrawn evidence as current.
            payload=json.loads(prior['payload'])
            payload.update({'score':0,'snapshot_at':published,'research_assessment':n['research_assessment'],
                'event_currently_eligible':False,
                'evidence':[f'当前核验状态：{verification}；事实阶段：{STAGE_LABELS[stage]}',excerpt or '当前缺少事实原文依据'],
                'hypothesis':'记录已更新，不再满足近期人工核验的订单/收入提醒条件；保留旧提醒供审计。',
                'question':'保持 HOLD，补充或重新核验独立证据后再进行人工研究。'})
            save_alert(conn,symbol,'imported','news',payload,n['id'])
    audit(conn,'news_saved',n['id'],{'verification':verification,'stage':stage,'expectation_status':expectation,'price_reaction':reaction,'research_action':n['research_assessment']['action'],'new_alert':new})
    conn.commit()
    return n

def digest(conn,mode):
    today=utcnow().astimezone(TZ).date().isoformat()
    alerts=[]
    for r in conn.execute('SELECT * FROM alerts WHERE mode=? ORDER BY score DESC',(mode,)):
        if parse_time(r['updated_at']).astimezone(TZ).date().isoformat()==today:
            alerts.append(r)
    run=conn.execute('SELECT * FROM runs WHERE mode=? ORDER BY id DESC LIMIT 1',(mode,)).fetchone()
    lines=[f'投研哨兵 · {today} 日报',f'模式：{mode}（demo 为完全虚构演示；imported 为人工导入，不代表实时接入）',f'生成时间：{stamp()}','',f'今日更新的关注事件：{len(alerts)}']
    for a in alerts:
        p=json.loads(a['payload'])
        lines.extend([f'• {a["symbol"]}：{a["title"]}｜关注分 {a["score"]}｜状态 {a["status"]}',f'  证据时间：{p.get("snapshot_at","")}；来源：{p.get("source","")}'])
        if p.get('research_assessment'):
            lines.append(f'  研究状态：{p["research_assessment"]["action"]}；{p["research_assessment"]["reason"]}')
    lines.extend(['',f'最近扫描：{run["status"]} / {run["detail"]}' if run else '尚无扫描记录','数据过期、缺失或失败均不等于“市场无异常”。','待决策：是否人工复核上述证据与反证，再考虑是否调整仓位？','本系统不连接券商、不下单；大单分类无法识别真实机构或洗盘意图。'])
    return '\n'.join(lines)

def state(conn):
    mode=setting(conn,'mode','demo')
    snapshots=latest_snapshots(conn,mode)
    watch=[]
    for r in conn.execute('SELECT * FROM watchlist ORDER BY added_at,symbol'):
        item=dict(r)
        item['snapshot']=snapshots.get(r['symbol'])
        if item['snapshot']:
            item['snapshot']['freshness']=freshness(item['snapshot'])
        watch.append(item)
    alerts=[]
    for r in conn.execute('SELECT * FROM alerts WHERE mode=? ORDER BY score DESC,updated_at DESC LIMIT 100',(mode,)):
        a=dict(r); a['payload']=json.loads(a['payload'])
        a['evidence_stale']= mode!='demo' and (utcnow()-parse_time(a['payload']['snapshot_at'])).total_seconds()> (72*3600 if a['kind']=='news' else MAX_AGE_MINUTES*60)
        alerts.append(a)
    news=[news_with_analysis(json.loads(r['payload'])) for r in conn.execute('SELECT payload FROM news WHERE mode=? ORDER BY published_at DESC LIMIT 100',(mode,))]
    runs=[dict(r) for r in conn.execute('SELECT * FROM runs WHERE mode=? ORDER BY id DESC LIMIT 12',(mode,))]
    events=[dict(r) for r in conn.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 20')]
    return {'paper':paper.paper_state(conn,mode) if mode in paper.MODES else None,'mode':mode,'server_time':stamp(),'timezone':'Asia/Shanghai','watchlist':watch,'alerts':alerts,'news':news,'runs':runs,'audit':events,'portfolio':[dict(r) for r in conn.execute('SELECT * FROM portfolio')], 'digest':digest(conn,mode),'monitor':{'persistent':False,'description':'按需扫描；尚未部署常驻服务或外部通知通道','live_adapter':False,'broker_api':False,'freshness_minutes':MAX_AGE_MINUTES},'rule_version':RULE_VERSION}

class Server(ThreadingHTTPServer):
    def __init__(self,address,db):
        self.db=db
        self.token=secrets.token_urlsafe(32)
        super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    server_version='InvestmentWatch/1.0'
    def log_message(self,fmt,*args):
        sys.stderr.write('%s %s\n'%(self.log_date_time_string(),fmt%args))
    def respond(self,status,data,content_type='application/json; charset=utf-8'):
        raw=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,(dict,list)) else data.encode() if isinstance(data,str) else data
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)
    def host_ok(self):
        host=self.headers.get('Host','').split(':')[0]
        return host in {'localhost','127.0.0.1'}
    def do_GET(self):
        if not self.host_ok():
            return self.respond(403,{'error':'仅允许本机访问'})
        path=urlparse(self.path).path
        if path=='/api/state':
            with connect(self.server.db) as conn:
                result=state(conn)
            result['csrf_token']=self.server.token
            return self.respond(200,result)
        if path=='/api/digest':
            with connect(self.server.db) as conn:
                output=digest(conn,setting(conn,'mode','demo'))
            return self.respond(200,output,'text/plain; charset=utf-8')
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path in files:
            content=(ROOT/'static'/files[path]).read_bytes()
            mime={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path]
            return self.respond(200,content,mime)
        return self.respond(404,{'error':'接口不存在；本服务没有交易接口'})
    def do_POST(self):
        if not self.host_ok():
            return self.respond(403,{'error':'仅允许本机访问'})
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://{self.headers.get("Host")}':
            return self.respond(403,{'error':'禁止跨站请求'})
        if self.headers.get('X-Watch-Token')!=self.server.token:
            return self.respond(403,{'error':'缺少本机请求令牌，请刷新页面'})
        if not self.headers.get('Content-Type','').startswith('application/json'):
            return self.respond(415,{'error':'仅接受 JSON'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=524288:
                raise ValueError('请求大小必须在 1–524288 字节内')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError('请求必须是对象')
            with connect(self.server.db) as conn:
                path=urlparse(self.path).path
                if path=='/api/run':
                    result=run_monitor(conn,data.get('mode',setting(conn,'mode','demo')))
                elif path=='/api/mode':
                    mode=data.get('mode')
                    if mode not in MODES: raise ValueError('未知模式')
                    set_setting(conn,'mode',mode); audit(conn,'mode_changed',mode,{})
                    result={'mode':mode}
                elif path=='/api/watchlist':
                    symbol=str(data.get('symbol','')).strip()
                    name=str(data.get('name','')).strip()[:50]
                    if not symbol_valid(symbol,True) or not name: raise ValueError('请输入六位证券代码和名称')
                    conn.execute('INSERT INTO watchlist VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,enabled=excluded.enabled',(symbol,name,int(bool(data.get('enabled',True))),stamp()))
                    audit(conn,'watchlist_saved',symbol,{'name':name});result={'saved':True}
                elif path=='/api/watchlist/remove':
                    conn.execute('DELETE FROM watchlist WHERE symbol=?',(str(data.get('symbol','')),))
                    audit(conn,'watchlist_removed',str(data.get('symbol','')),{})
                    result={'removed':True}
                elif path=='/api/alerts/status':
                    status=data.get('status')
                    if status not in STATUSES: raise ValueError('无效提醒状态')
                    row=conn.execute('SELECT id FROM alerts WHERE id=?',(data.get('id'),)).fetchone()
                    if not row: raise ValueError('提醒不存在')
                    conn.execute('UPDATE alerts SET status=?,updated_at=? WHERE id=?',(status,stamp(),data['id']))
                    audit(conn,'alert_status',str(data['id']),{'status':status,'note':str(data.get('note',''))[:500]})
                    result={'saved':True}
                elif path=='/api/import/portfolio': result={'count':import_portfolio(conn,str(data.get('csv','')))}
                elif path=='/api/import/market': result={'count':import_market(conn,data.get('snapshots'))}
                elif path=='/api/news': result={'news':add_news(conn,data)}
                elif path=='/api/paper/decision':
                    result=paper.submit_decision(conn,data)
                    audit(conn,'paper_decision',str(result.get('decision_id','')),{'mode':data.get('mode'),'side':data.get('side'),'source':data.get('source')})
                elif path=='/api/paper/calendar':
                    result=paper.import_calendar(conn,data)
                    audit(conn,'paper_calendar',data.get('calendar_id',''),{})
                elif path=='/api/paper/process':
                    result=paper.process_pending(conn,data.get('mode'),data.get('quotes',[]),data.get('session',{}))
                    audit(conn,'paper_process',data.get('mode',''),result)
                elif path=='/api/paper/config':
                    result=paper.configure_paper(conn,data.get('mode'),data.get('config',{}))
                    audit(conn,'paper_config',data.get('mode',''),data.get('config',{}))
                elif path=='/api/paper/exit-plan':
                    result=paper.configure_exit_plan(conn,data)
                    audit(conn,'paper_exit_plan',data.get('plan_id',''),{'mode':data.get('mode'),'source':data.get('source'),'test_only':True})
                elif path=='/api/paper/exit-cancel':
                    result=paper.cancel_exit_plan(conn,data.get('mode'),data.get('plan_id'),data.get('reason'))
                    audit(conn,'paper_exit_cancel',data.get('plan_id',''),{'mode':data.get('mode'),'reason':data.get('reason')})
                else: return self.respond(404,{'error':'接口不存在；本服务没有交易接口'})
                conn.commit()
            return self.respond(200,result)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
            return self.respond(400,{'error':str(exc)[:500]})
        except Exception:
            return self.respond(500,{'error':'内部错误，请检查本机服务日志；未进行交易操作'})

def main():
    p=argparse.ArgumentParser(description='投研哨兵：本机只读研究工具，无交易能力')
    p.add_argument('--db',default=str(DEFAULT_DB))
    sub=p.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--port',type=int,default=8765);serve.add_argument('--empty',action='store_true')
    monitor=sub.add_parser('monitor');monitor.add_argument('--mode',choices=sorted(MODES),default='imported');monitor.add_argument('--once',action='store_true',help='显式单次运行；不安装后台服务')
    im=sub.add_parser('import-market');im.add_argument('file')
    ip=sub.add_parser('import-portfolio');ip.add_argument('file')
    dg=sub.add_parser('digest');dg.add_argument('--mode',choices=sorted(MODES),default='imported');dg.add_argument('--out')
    for command in ('paper-decision','paper-calendar','paper-process','paper-config','paper-exit-plan','paper-exit-cancel'):
        parser=sub.add_parser(command);parser.add_argument('file')
    ps=sub.add_parser('paper-state');ps.add_argument('--mode',choices=sorted(paper.MODES),default='imported')
    args=p.parse_args()
    conn=connect(args.db)
    if args.command=='serve':
        if not args.empty and not conn.execute('SELECT 1 FROM snapshots LIMIT 1').fetchone():
            run_monitor(conn,'demo')
        conn.close()
        print(f'投研哨兵：http://127.0.0.1:{args.port}（仅本机；无常驻监控保障）',flush=True)
        Server(('127.0.0.1',args.port),args.db).serve_forever()
    elif args.command=='monitor':
        result=run_monitor(conn,args.mode)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result['status']=='failed' else 2 if result['status']=='degraded' else 0
    elif args.command=='import-market':
        print(f'导入 {import_market(conn,json.loads(Path(args.file).read_text()))} 条行情')
    elif args.command=='import-portfolio':
        print(f'导入 {import_portfolio(conn,Path(args.file).read_text())} 个持仓')
    elif args.command.startswith('paper-'):
        if args.command=='paper-state': result=paper.paper_state(conn,args.mode)
        else:
            data=json.loads(Path(args.file).read_text())
            if args.command=='paper-decision': result=paper.submit_decision(conn,data)
            elif args.command=='paper-calendar': result=paper.import_calendar(conn,data)
            elif args.command=='paper-process': result=paper.process_pending(conn,data.get('mode'),data.get('quotes',[]),data.get('session',{}))
            elif args.command=='paper-exit-plan': result=paper.configure_exit_plan(conn,data)
            elif args.command=='paper-exit-cancel': result=paper.cancel_exit_plan(conn,data.get('mode'),data.get('plan_id'),data.get('reason'))
            else: result=paper.configure_paper(conn,data.get('mode'),data.get('config',{}))
            audit(conn,args.command,data.get('mode',''),{'file_name':Path(args.file).name})
            conn.commit()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='digest':
        output=digest(conn,args.mode)
        if args.out: Path(args.out).write_text(output,encoding='utf-8')
        else: print(output)
    conn.close()
    return 0

if __name__=='__main__':
    raise SystemExit(main())
