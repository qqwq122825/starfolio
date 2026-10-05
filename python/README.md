# 星策 Starfolio · Python v2

原实现名称：Investment Watch。当前目录为合并仓库的 `python/`。

A 股中文研究管理工作台：先看证据，再讨论行动。Python 3.11+ 标准库后端、SQLite、无构建步骤的静态前端。

## 当前交付状态（v2）

**已实现**：六页管理界面、自选编辑、半导体/存储研究分类、行情 JSON 导入、资金异常规则、事件阶段人工核验、CSV 持仓参考、提醒状态与审计、日报文本、单次监控命令、失败重试与去重、隔离模拟账户及虚拟买卖撮合、可选分批退出/移动止盈、重启安全的剩余股数与退出审计。

**尚未接入**：真实行情/新闻 API、L2 逐笔、券商只读持仓、AI API、常驻 24 小时主机、外部通知、互联网访问认证。程序不会在这些能力缺失时伪造结果。

默认展示 DEMO01–DEMO04 虚构公司和虚构行情。它们不对应真实证券。真实自选清单最初为空。没有当前热门股票排名、真实买卖建议或真实策略业绩。

## 快速运行

```bash
cd python
python3 investment_watch.py serve
```

在运行此程序的**同一台电脑**打开 http://127.0.0.1:8765。第一次启动自动建立纯演示数据；浏览器“数据与设置”可切换人工导入模式。不是公网地址，不会在你的另一台电脑自动打开。

可选：空行情启动和独立数据库。

```bash
python3 investment_watch.py --db data/my-watch.db serve --empty --port 8765
```

服务只监听 127.0.0.1，拒绝外部 Host；写入要求同源令牌。没有账号体系，不适合直接公开。生产部署须置于认证代理、HTTPS、持久磁盘与监控后；本交付没有做公网部署。

## 操作路径

1. 在“自选与主题”新增六位证券代码和名称，再进入“人工导入”工作区
2. 在“数据与设置”导入来源已核对的行情 JSON
3. 点击“执行一次扫描”；不能把“扫描成功”理解成已抓取实时行情
4. 在“事件核验”添加公告链接、发布时间、原文依据，区分应用/框架、试点、订单、收入
5. 在“提醒与审计”查看证据、另一种解释、失效条件，并记录复核状态
6. 在“持仓与模拟”导入本机参考持仓 CSV；模拟决策必须保留真实判断来源
7. “导出日报”下载当前模式的研究摘要。页面每 30 秒只刷新本机状态，不拉取市场数据

## 行情与规则

见 [数据契约](docs/adapters.md)。examples/market-snapshot-template.json 是**固定历史日期的虚构格式样本**，故正常会被判定过期；不得通过改时间把历史数据冒充实时数据。

```bash
python3 investment_watch.py import-market examples/market-snapshot-template.json
python3 investment_watch.py monitor --mode imported --once
python3 investment_watch.py digest --mode imported --out data/digest.txt
```

要分析某个导入证券，先加入自选。命令退出码：0=扫描正常，1=源失败，2=降级（缺失/过期/基线不足）。服务未配置真实实时源时，live 模式明确失败，不回退到演示。

资金异常规则 evidence-v1：
- 至少 20 个可比历史时间窗的成交额样本，基线均值与标准差均大于 0
- 成交额/基线均值 ≥1.8 且 Z≥2.5：40 分
- 供应商大单净额绝对值/成交额 ≥8%：30 分
- 价格方向与供应商大单净额方向背离：15 分
- ≥55 分进入人工关注；同模式/标的/上海交易日期/规则版本去重

这是透明的研究筛选示例，阈值未经收益回测或经济有效性验证。没有概率含义。基线可比性仍需数据提供者证明；系统不会把缺少的大单数据估算出来。大单是供应商的统计分类，不能证明真实机构身份、主力吸筹或洗盘。

行情时间超过 20 分钟或在未来，禁止新资金提醒；每条记录保留观察时间、抓取时间与来源。失败不能等同于“市场无异常”。非交易时段的旧报价仍按过期处理，不会为了日报而重标为新报价。交易日历未加载时，程序不根据星期推断开市。

## 消息的真实性与实质性

- 仅放入 HTTPS 来源链接，状态最多只能是“已有链接”，不会自动升级为核验
- 人工标记“已核验”必须填写核验人和原文依据；系统不因此声称独立验证事实
- 72 小时内、人工核验、订单或收入阶段可进入事件关注；框架和试点不会自动成为订单
- 订单不是收入，收入不是利润或回款；需核对金额、附加条件、取消风险及相对公司体量
- v2 分开记录事实阶段、相对预期（超预期/符合/低于/已计价）和价格反应；后两者必须附人工研究依据，不从涨跌反推事实
- 证据未知/不足保持 HOLD；资料齐全也只进入人工研究，不自动从新闻创建买卖决策
- 没有接入自动新闻收集、全文抽取或模型事实判断

## 模拟账户（只有纸面交易）

demo 与 imported 各自独立的初始虚拟现金 100,000 元。默认无杠杆、无做空，单证券 ≤初始资金 10% 且 ≤扣费后权益 10%。这不是实盘券商订单。imported 表示导入的假设模拟，不意味着实时策略业绩。

- 支持 buy / sell / hold；判断来源必须如实标记 assistant / rules / user
- 助理可按已获授权的研究证据生成判断文件；本程序没有接入 AI API，也不会自动编造“AI 理由”
- 缺少日历/新鲜报价/交易状态时保持 HOLD，不成交
- 模拟成交使用**决策之后**的合格报价，bid/ask 加不利滑点，不用决策当时已知的旧价格
- T+1 用明确交易日历、100 股买入、停牌/涨跌停保守阻断、现金约束、FIFO、费用、重复执行安全
- source_verified、日历 verified 只是导入者声明，本程序并未独立核验
- 历史首次导入无法证明研究者没有事后信息；所有历史回放必须标注假设性，禁止当真实历史实时业绩
- 部分成交、完整盘口队列、公司行动、分红、融资、完整交收和所有板块特殊最小申报规则尚未模拟

费用为**可配置示例**，不是已核验最新券商收费：佣金 0.03%、最低 5 元；卖出印花税 0.05%；双边过户 0.001%；不利滑点 5bps。实际行情与收费接入后需核对。

```bash
# 状态：没有日历时明确 hold_missing_calendar
python3 investment_watch.py paper-state --mode imported
# 按顺序导入你已核对的资料
python3 investment_watch.py paper-calendar calendar.json
python3 investment_watch.py paper-decision decision.json
python3 investment_watch.py paper-process quote-batch.json
# {"mode":"imported","config":{"commission_rate":0.0003,...}}
python3 investment_watch.py paper-config fees.json
```

完整输入字段、时钟/不回溯约束与失败行为见 paper.py 文件头文档，演示 JSON 在 examples 中。先用独立 demo 数据库跑样例，不能与真实数据账本混合。

## v2 分批退出与移动止盈（可选，仅规则测试）

必须显式创建 test_only=true 的退出计划；默认不启用。它只管理当前已持有的模拟股票，不自动建仓。

- 分批卖出按参考价的不同涨幅阈值触发固定股数；成交后才减少剩余股数
- 移动止盈先达到激活阈值，再跟踪合格 bid 的最高值；高水位只升不降，回撤触发卖出计划剩余股数
- 每次只保留一张退出卖单；触发报价不能同时用于成交，生成决策不得早于所有相关报价的接收时间
- T+1 锁定、订单过期或流动性不足不会假装成交；已触发意图会保留，待新的合格数据重试；如研究判断改变，须取消计划
- 跳空按之后实际合格 bid 和不利滑点模拟，绝不按“止盈线”保证成交
- 同标的一次只有一个活动计划；活动期间需先取消计划才能手动增减仓，避免重复管理同一股数
- 所有比例只是测试参数，未做收益/样本外/经济有效性验证，不构成推荐参数或止损保证

```bash
python3 investment_watch.py paper-exit-plan exit-plan.json
python3 investment_watch.py paper-exit-cancel cancel-plan.json
```

中文界面可导入/取消计划并查看高水位、剩余股数、生成判断和事件日志。完整契约、示例复现命令与延迟语义见 [退出规则说明](docs/paper-exits.md)。

## 只读持仓

```csv
symbol,name,shares,cost
600000,示例名称,100,10
```

CSV 导入原子替换本机参考持仓，禁止附带账户、身份证或登录信息。不会上传第三方。未来只读持仓接口契约见 docs/adapters.md，没有实现 broker、buy、sell 或真实下单端点。

## 24 小时运行边界

目前是可运行原型，不是已承诺 24/7 SLA 的生产服务。当前云开发进程可能结束。可按 docs/deployment.md 配置常驻主机和调度，但本交付**没有安装 daemon、cron、systemd 或自动通知**。

监控命令是单次、安全可重入的任务。主机调度不应改变数据源授权、超额调用或绕过交易日历。行情抓取、日报、消息轮询可分别调度。先通过数据质量与模拟约束验收，再考虑外部提示。

真实数据选型与采购前问题见 [API 方案对照](docs/data-api-options.md)。没有购买服务、创建密钥、配置持久访问或连接券商。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check static/app.js  # Node 仅用于可选语法检查，应用运行不需要 Node
```

测试覆盖失败/过期、字段校验、重试、去重、CSV 原子性、公告核验、HTTP/CSRF/Host 限制，以及模拟账户费用、T+1、限价/停牌、资金上限、前视偏差与重复执行。v2 新增高水位单调性、分批股数守恒、延迟接收无前视、跳空、退出取消/过期重试、重启去重、持久化同时间歧义隔离与新闻未知证据 HOLD 测试。

**可视化 QA 限制**：当前环境的本机 Chromium 无法启动（socket 权限受限），云浏览器拒绝 localhost 页面。未获得可验证的界面截图或浏览器交互通过结果。已完成 Python/HTTP 测试、JS 语法检查及前端源码检查；这不等同于视觉验收。tests/ui_check.py 是供允许本机 Chromium 的环境运行的待执行 UI 检查脚本。

## 文件

- investment_watch.py：SQLite、规则、API、CLI、本机 HTTP 服务
- paper.py：隔离、保守的模拟账户与撮合模块
- paper_exits.py：可选、持久化、仅测试的分批退出与移动止盈
- static/：六页中文管理工作台
- examples/：明确标注演示的数据格式
- docs/：数据接入、API 选型、调度部署
- tests/：stdlib 单元/HTTP 测试与可选 UI 检查脚本
- ../docs/VALIDATION.md：本次源码整理的验证记录（不含运行日志）

data/ 运行时创建；交付包不包含数据库、真实持仓、凭证、缓存或虚拟环境。

### 复现一笔纯演示成交

以下使用独立 demo 数据库，仅验证模拟账本，绝非真实股票交易或策略推荐：

```bash
python3 investment_watch.py --db data/demo-replay.db paper-calendar examples/paper-demo-calendar.json
python3 investment_watch.py --db data/demo-replay.db paper-decision examples/paper-demo-decision.json
python3 investment_watch.py --db data/demo-replay.db paper-process examples/paper-demo-process.json
python3 investment_watch.py --db data/demo-replay.db paper-state --mode demo
```

预期虚构 DEMO01 买入 100 股，模拟价 10.02 元、示例费用 5.01 元、现金 98,992.99 元。再次执行同一 paper-process 不会重复成交。此样例的 source=assistant 只表示编写了演示判断，不表示已有自动模型接入。
