# v2 纸面退出计划与消息研究契约

## 边界

本模块是确定性的规则/账本测试器，不是已有收益优势的策略、自动 AI 投资服务或真实交易系统。未接入行情/新闻 API，未部署常驻程序。只有调用 paper-process 并提供合格数据时才推进模拟。所有比例必须由调用方显式填写；test_only 必须为 true。示例阈值没有验证或推荐含义。

## 计划 JSON

examples/paper-v2-exit-plan.json 是完整虚构示例。API 为 paper.configure_exit_plan(conn,data)，HTTP 为 POST /api/paper/exit-plan，CLI 为 paper-exit-plan FILE。

- plan_id：同模式唯一且不可变；原内容重复导入返回原计划，修改内容须新 ID
- mode：demo / imported；彼此隔离，禁止 live
- symbol、quantity：已在该模拟账户持有的证券及计划股数，不可大于现有仓位
- submitted_at：含时区；不能晚于实际现在或早于已经推进的模拟时钟
- source：user / assistant / rules，只描述计划来源；程序派生的卖单始终标记 rules
- reason：真实填写计划依据；不伪造 AI 解释
- reference_price：人工指定的正数参考价，阈值以此计算；不自动等同于含费平均成本，因此涨幅阈值不是实际净利润率
- stages：按 gain_pct 严格递增的最多 20 级列表，每项 gain_pct 为正小数、上限 10（1000%），quantity 为正整数。0.02 表示 2%
- trailing：可缺省/null；启用时必须给 activation_gain_pct（0–10）及 distance_pct（0 到 1 之间，不含端点）。0.03 表示从高水位回撤 3%
- 单独分批退出时各级股数总和必须等于计划股数；同时启用 trailing 时各级总股数必须小于计划股数，保留正数余量
- 至少启用 stages 或 trailing；未知字段、NaN、Infinity、负数、布尔股数等拒绝

可以只覆盖现有仓位的一部分。为防止隐含重叠，同模式/证券仅一个活动计划；计划生效时拒绝额外手动 buy/sell，hold 仍可记录。创建前需先完成已有 pending 买卖决策。取消后才可改变仓位或创建新计划。卖出仍遵循原型的整数股/FIFO 模型，未实现所有板块特殊申报规则。

## 触发、成交、剩余量

高水位从创建计划后第一条合格 bid 开始，永不降低；低质量/过期/停牌/涨跌停/相同或更早观察时间不会更新它。bid 达到 reference_price × (1 + activation_gain_pct) 后，trailing_active 保持开启。之后 bid ≤ high_water_mark × (1 − distance_pct) 时，触发退出该计划剩余股数。移动退出优先于尚未创建卖单的分批意图。

分批按最低尚未完成层级检查；每次只创建一张卖单。跨过多级也不会把各级假设为已成交。新卖单在下一条合格报价按 bid 加不利滑点撮合，没有“到达目标价即可保证按目标卖出”。卖单触发后是一张等待后续报价的模拟卖单，后续价格跌回阈值以下也不取消它。

剩余股数只在成交写入后递减；拒绝、过期、流动性不足、T+1 锁定不会改变数量。T+1 锁定的触发意图保留，跨日解锁后需新报价创建决策，再等待更晚报价成交。过期子订单保留为 rejected，新的合格报价可生成不同 ID 的重试单。意图不会因价格反弹自动消失；若不再想退出，必须取消计划。

## 时间与重启

- 所有日历、来源、状态、新鲜度、T+1、盘口量、费用、仓位上限和不做空约束沿用 v1
- 计划信号不会在自身观察时间成交；派生决策的提交/证据时间不得早于所有相关触发及高水位报价的最大 received_at
- 同一批按观察时间处理，但延迟收到的高点不会让后续回撤在该高点尚未收到时提前成交
- 同模式/证券/观察时刻出现不同报价 ID 时，保守视为排序歧义并隔离；仅重放其中一条或重启不能解除隔离。之后新出现的矛盾数据不会重写已经发生的模拟成交
- 高水位、激活状态、意图、剩余股数、阶段完成状态、子订单与事件都存入 SQLite；原子事务保证中断时不会只写一半
- 没有新合格数据时不推进。历史回放仍可能有人工事后选择偏差，不能声称为真实实时业绩

## 取消和审计

paper.cancel_exit_plan(conn, mode, plan_id, reason)，HTTP POST /api/paper/exit-cancel，CLI paper-exit-cancel FILE。取消活动计划会取消其未成交子订单，不撤销已成交部分，不自动卖出剩余仓位。重复取消安全。已完成计划保留原完成状态。

paper-state 包含 exit_plans、exit_events、exit_disclaimer。事件包含计划创建、激活、触发、子订单创建、受阻、拒绝、成交和取消。决策包含 exit_context，可追溯 plan_id、触发价格与时刻、高水位、信息可用时间及尝试次数。价格字段使用十进制字符串以保留审计精度。

## 复现虚构分批和移动退出

使用全新独立路径，不能把此演示导入真实研究账本：

```bash
python3 investment_watch.py --db data/v2-demo.db paper-calendar examples/paper-demo-calendar.json
python3 investment_watch.py --db data/v2-demo.db paper-decision examples/paper-v2-entry.json
python3 investment_watch.py --db data/v2-demo.db paper-process examples/paper-demo-process.json
python3 investment_watch.py --db data/v2-demo.db paper-exit-plan examples/paper-v2-exit-plan.json
python3 investment_watch.py --db data/v2-demo.db paper-process examples/paper-v2-exit-process.json
python3 investment_watch.py --db data/v2-demo.db paper-state --mode demo
```

结果：虚构买入 600 股，分批卖出 100、200 股，移动退出余下 300 股。高水位 10.70，回撤线 10.379，最后一笔因跳空在后续 bid 9.80 基础上不利滑点后成交 9.79，绝不在回撤线虚构卖出。重复执行最后的 process 不会重复成交。

## 消息的事实、预期、价格三个层次

原有 stage 仅记录 unknown / application / pilot / order / revenue / other，并附原文证据。人工 reviewed 要求核验人和原文，并不代表程序独立验证了网页。

v2 独立字段：
- expectation_status：unknown / above / in_line / below / priced_in，必须附 expectation_evidence；应记录预期基准、其形成时间、来源及比较内容
- price_reaction：unknown / up / flat / down，必须附 price_reaction_evidence；应记录观察窗口、价格来源、涨跌和可能的其他解释

这些是人工研究输入。系统不依据价格反推订单真实性，也不把“消息后下跌”自动判定为利好兑现或卖点。priced_in 是分析者解释，不是可直接测量或已验证事实。缺失字段/未知证据产生 insufficient_evidence + HOLD；资料齐全也只为 manual_review_only + HOLD。新闻记录不会自动生成纸面交易。旧记录增加 unknown 默认值，不改写原始事实。

## 信息时点、交易所和迟到冲突修正

持仓估值与新买单的风险检查不得使用尚未收到的行情。风险检查按候选成交报价的 observed_at，选择该时刻已收到、交易所匹配、仍在新鲜度窗内、且同标的同时间不冲突的历史 bid；较新的迟到包不会覆盖本来可用的较旧数据。缺少合格依据则保持待定，不推测价格。

与现有同代码持仓交易所不符的报价在落库前拒绝。展示估值同样核对交易所、原始行情证据和接收时间；迟到的同时间冲突会令受影响估值失效，不能继续显示为当前权益。已经完成的模拟成交不会被静默改写；冲突属于需人工复核的审计问题。

分批退出子单等待流动性期间，若已激活的移动止盈线被穿越，系统持久记录首次穿越的 deferred_trailing 意图。价格之后反弹也不会抹去该事件；原分批子单结束后按真实剩余股数创建移动止盈子单，任何时刻仍只有一张待成交子单。新子单的提交时间不得早于此前触发信息、已完成子单的成交报价和当前报价的接收时间，仍须之后的严格更晚报价才能成交。

如果已经被退出计划使用的同标的同时间报价后来出现冲突，系统在处理下一次成交前将该计划隔离：取消未成交子单、保留活动计划和剩余股数，显示 ambiguous_prior_quote_requires_cancel，要求明确取消并人工复核；后续好报价不会自动解除隔离。已有成交不回滚。新计划保存实际使用的报价键，旧版本计划缺少这一记录时对其已观察时间范围采取保守隔离。

曾用于估值、成交或活动退出计划的历史输入出现迟到冲突时，账户另外持久记录 data_conflicts 和 data_quality_warning。即使以后收到正常新报价或取消退出计划，这条历史数据质量警告也不会被清掉，以免把存在争议的模拟历史当作干净的策略业绩。
