export interface Snapshot {symbol:string;mode:string;price:number;change_pct:number;turnover:number;observed_at:string;freshness:{state:string;label:string}}
export interface WatchItem {symbol:string;name:string;enabled:number;snapshot:Snapshot|null}
export interface Assessment {action:string;status:string;reason:string;missing_fields?:string[]}
export interface News {id:string;symbol:string;title:string;mode:string;stage:string;verification:string;published_at:string;source:string;source_url:string;evidence_excerpt:string;expectation_status:string;expectation_evidence:string;price_reaction:string;price_reaction_evidence:string;reviewed_by:string;note:string;amount:string;research_assessment:Assessment}
export interface Alert {id:number;symbol:string;kind:string;title:string;score:number;status:string;evidence_stale:boolean;payload:{evidence:string[];counter_evidence:string[];invalidation:string[];hypothesis:string;question:string;source:string;source_url:string;snapshot_at:string;rule_version:string;disclaimer:string;research_assessment?:Assessment}}
export interface Portfolio {symbol:string;name:string;shares:number;cost:number;imported_at:string}
export interface Run {id:number;mode:string;started_at:string;status:string;detail:string}
export interface Audit {id:number;at:string;action:string;target:string;detail:string}
export interface Workspace {mode:string;server_time:string;csrf_token:string;watchlist:WatchItem[];news:News[];alerts:Alert[];portfolio:Portfolio[];runs:Run[];audit:Audit[];digest:string;rule_version:string;hosted_paper_disabled:boolean;monitor:{freshness_minutes:number}}
export type Page='overview'|'watchlist'|'news'|'portfolio'|'alerts'|'settings';
export const navigation:{key:Page;label:string}[]=[{key:'overview',label:'研究总览'},{key:'watchlist',label:'自选与主题'},{key:'news',label:'事件核验'},{key:'portfolio',label:'持仓与模拟'},{key:'alerts',label:'提醒与审计'},{key:'settings',label:'数据与设置'}];
export const stages:Record<string,string>={unknown:'未知 / 证据不足',application:'应用 / 框架',pilot:'验证 / 试点',order:'明确订单',revenue:'已确认收入',other:'其他 / 未明确'};
export const expectations:Record<string,string>={unknown:'未知 / 不作推断',above:'高于预期',in_line:'符合预期',below:'低于预期',priced_in:'可能已计价'};
export const reactions:Record<string,string>={unknown:'未知 / 未观察',up:'观察窗口上涨',flat:'观察窗口持平',down:'观察窗口下跌'};
export const verifications:Record<string,string>={unverified:'待核验',linked:'已有链接 · 未核验',reviewed:'用户已核验',demo:'虚构样本'};
export const statuses:Record<string,string>={new:'待复核',reviewed:'已复核',dismissed:'已忽略'};
export function formatDate(value?:string){return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—'}
export function number(value?:number|null){return value==null?'—':value.toLocaleString('zh-CN',{maximumFractionDigits:2})}
