# 调度与可靠部署（说明，不自动安装）

本原型当前只有按需运行和单次命令。以下步骤供经过授权的常驻服务器使用，**未在当前开发环境执行**。

## 上线前阻断条件

1. 选择可持续在线、有持久存储/备份和可信时钟的主机
2. 确认行情、历史、日历、停牌/涨跌停、新闻源的使用权限与费用；无数据不启用模拟成交
3. 在可信身份认证代理与 HTTPS 后使用本机应用；需正式改造代理 Host 策略与登录，不可绕过当前 localhost 限制公开端口
4. 用户明确选择通知目的地与可发送内容；本版本不含发送功能或自动调用聊天接口
5. 建立数据质量、交易日历及 paper 约束验收、告警投递确认、备份恢复演练

## 单次任务

```bash
cd /opt/investment-watch
python3 investment_watch.py --db /var/lib/investment-watch/watch.db monitor --mode imported --once
python3 investment_watch.py --db /var/lib/investment-watch/watch.db digest --mode imported --out /var/lib/investment-watch/digest.txt
```

需要先由已授权数据适配器持续导入新数据；调度已有历史快照不会产生实时覆盖。退出码2应触发运行健康提醒，不能当成功无异常。

下面是服务器管理员可审阅后安装的 systemd 样例。路径/用户须根据实际主机调整；不包含数据采集器或消息发送器。

```ini
# investment-watch-scan.service
[Unit]
Description=Investment Watch single scan
[Service]
Type=oneshot
User=investment-watch
WorkingDirectory=/opt/investment-watch
ExecStart=/usr/bin/python3 /opt/investment-watch/investment_watch.py --db /var/lib/investment-watch/watch.db monitor --mode imported --once
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/investment-watch

# investment-watch-scan.timer
[Unit]
Description=Investment Watch scan schedule
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
```

24小时健康检查与交易时段扫描是两件事。基于完整交易日历安排实时源调用；休市时仍可监测公告，不能制造成交报价或把星期一视为交易日。当前 2026-10-05 的节假日状态未加载到执行日历，模拟模块默认阻断。

## 可靠性

- SQLite WAL 与事务；持久目录权限应仅运行用户可读写。定期用 SQLite backup API 备份，不要只复制活跃 WAL 数据库主文件
- 供应商失败最多三次指数退避；真实部署应加入供应商限流、抖动、熔断和延迟队列
- 每事件持久去重；状态和规则版本写入数据库；同一模拟决策/报价不可重复成交
- 进程重启不会丢账本，但当前 v1 未实现退出中断 run 标记恢复、审计数据留存裁剪、自动备份或健康推送
- 页面“本机运行”只表示浏览器正在访问本机服务，不是全天可用保证
- 不要把 API 凭证放入聊天、配置样例、URL、日志、截图或 ZIP。未来凭证配置必须走单独授权与安全注入
