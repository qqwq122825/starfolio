# 免费日线研究试接入

## 实际状态（2026-10-05）

Python 已实现 Tushare `daily` HTTPS 适配、严格字段校验、本地独立研究记录和 CLI/API 状态。尚无用户授权配置的 token，**没有取得真实行情，没有真实数据模拟成交，也没有验证已认证账户的接口权限**。

云计算机实际连通性检查：

- 2026-10-05 04:31:25 UTC：`GET https://api.tushare.pro` 返回 HTTP 200
- 2026-10-05 04:33:25 UTC：`POST https://api.tushare.pro`，daily JSON、空 token，返回 HTTP 200 / API 40101（要求 token）；最终 URL 为原 HTTPS 地址
- 这仅证明当前 HTTPS 可达并响应 API 请求，不证明 authenticated daily 成功、数据质量或长期 SLA。官方传统 REST 文档仍写 HTTP；程序坚持 TLS 验证、拒绝重定向，绝不降级 HTTP。后续如 HTTPS 不可用就明确失败
- AKShare 介绍页的 Python HTTPS 请求返回 HTTP 403。未尝试替换身份、代理、绕过或反复请求该端点；文档通过网页阅读工具核对

## 为什么没有默认启用 AKShare / 东方财富

[AKShare 概览](https://akshare.akfamily.xyz/introduction.html)限定接口及相关数据用于学术研究。[东方财富法律声明](https://about.eastmoney.com/home/disclaimer)和[服务协议](https://about.eastmoney.com/home/protocol)对内容复制与行情用途有额外限制。开源接口代码不能替代上游数据授权。本试验不把网页公开可见等同于允许自动复制或再分发，不抓取这些候选行情。

## 免费资格、使用边界与用户下一步

[Tushare 权限频次表](https://tushare.pro/document/1?doc_id=290)列出 120 积分、0 元/年、50 次/分钟、每天 8,000 次，仅未复权日线，其他接口不包含。[积分说明](https://tushare.pro/document/1?doc_id=13)说明注册 100 积分、完善真实个人资料 20 积分。注册需要用户自行完成验证；此交付没有注册、填写资料、购买积分、创建 token 或安装长期凭据。

[服务协议](https://tushare.pro/document/1?doc_id=405)的授权面向个人、不可转让、非商业使用；不能据此承诺公开行情再分发。本站仅准备个人私有研究接入。任何真实数据或衍生研究记录不得随公共源码、公共网页、ZIP 或测试夹具发布。[FAQ](https://tushare.pro/document/1?doc_id=122)要求相关使用标明 Tushare 来源，署名不替代再分发授权。

若用户选择该免费路径，应由用户阅读并接受适用条款、自行取得满足免费资格的账户及 token，在获准的安全设置流程中配置到自己的运行环境。**不要把 token 发到聊天、Git、截图、命令行参数、URL 或日志中。**程序只从当前进程环境 `TUSHARE_TOKEN` 读取已配置 token，不保存 token，不读取或改写 `.env`，不提供网页 token 输入框。配置/传输长期凭据须走用户安全交接，不能由此文档代替批准。

配置后还需要再运行一次真正的、已授权的 HTTPS daily 请求并核验返回，才能说免费行情已连通。遇到权限或配额错误只报失败，不自动充值或改用收费端点。

## 小范围运行

只支持上交所 6 位、6 开头代码加 `.SH`，每次 1–3 个；回看 1–120 个日历日。下面 `600000.SH` 只是语法例子，不是投资推荐，不会自动加入用户自选。

```bash
python3 investment_watch.py --db data/private-research.db daily-fetch --symbols 600000.SH --days 60
python3 investment_watch.py --db data/private-research.db daily-status
```

成功退出 0；源失败退出 1；缺失、过期或未配置等降级退出 2。未配置 token 的 fetch 返回 `missing_token_user_secure_setup_required`、空 bars、退出 1。没有自动回退演示。

这个功能不启动后台轮询。每个标的只请求一次，无自动重试；小宇宙单次最多 3 个 daily 请求。使用者重复运行仍须遵守平台配额，不应循环全市场下载。SQLite 的 `daily_research_runs` 独立保留每次结果，API `/api/state` 下的 `daily_research` 提供状态；尚未在托管 Web 版接入或部署此适配器。

## 数据与日历契约

- [daily 字段文档](https://tushare.pro/document/2?doc_id=27)：未复权 OHLC，价格人民币元/股，`vol` 手乘 100 转为股，`amount` 千元乘 1,000 转为元
- `pct_chg` 保留供应商基于除权昨收计算的百分比值；不从普通前一天 close 重算，不宣称含分红总收益，不凭未复权序列计算投资回报
- 日线仅带交易日期。`period_end_at` 按上海时区当天 15:00 标记统计周期边界，`timestamp_basis` 明确写 inferred，绝非伪造交易所 tick 时间；`fetched_at` 为实际响应接收时间
- `large_order_net:null`，不推算大单资金、停牌、涨跌停、bid/ask、流动性、成交状态
- 非法/重复/越界/假日日期、非有限值、负成交量金额、不一致 OHLC、错标的、错 schema 全部失败
- 最新日线为空标记 missing；旧日期标记 stale。停牌期间可能无数据，但仅凭缺失不能断言停牌
- `execution_eligible:false` 永久固定；数据只进入独立 daily 表，不写 intraday snapshots，不生成资金提醒、买卖判断或纸面成交

免费层不能假设包含 [trade_cal](https://tushare.pro/document/2?doc_id=26)（当前文档要求 2,000 积分）。本试验仅依据[上交所官方 2026 年休市安排](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)建立全年公布交易日表：周末加明确节假日，不从一般工作日推断调休周六开市，不拓展至深交所或 2027 年。

官方 daily 页写入库 15–16 点，而[接口权限概览](https://tushare.pro/document/1?doc_id=108)写 15–17 点，故采用保守 17:00 日线切换点。10 月 1–7 日国庆休市期间期望最新日线为 9 月 30 日；10 月 8 日 17:00 后期望 10 月 8 日。此标签只判断每日研究资料，不改变现有 20 分钟盘中行情期限或模拟交易门槛。日历注明未核验临时额外休市，缺失最新 bar 一律可见降级。超出 2026 年覆盖范围停止判定，须更新官方日历。

## 验证

- 所有新增测试使用人工构造夹具，无真实市场价格
- `python3 -m unittest discover -s tests -p 'test_*.py' -v`
- 覆盖假期/开市/入库时点、时区、单位、复权声明、缺失/过期/日历过期、错误与 token 脱敏、HTML/错误 schema、禁止隐式回退、重复研究调用不产生撮合报价
- 实际 HTTPS smoke 仅验证无凭据端点响应；完整 authenticated 数据验收仍被安全 token 配置阻挡
