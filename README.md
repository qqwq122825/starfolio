# 星策 Starfolio

中文投研管理工作台：把自选、行情来源、公告证据、提醒和研究记录放在一起，先核对证据，再讨论行动。

这个仓库包含两个独立运行的实现。它们不会自动同步数据库，也不具备完全相同的功能。

## 先了解边界

- **web/**：基于 **Vue 3 + Element Plus** 成熟组件库的私有云端管理版源码。支持自选、人工行情 JSON、事件三维证据、规则扫描、提醒复核、参考持仓 CSV、审计和日报；**所有 `/api/paper/*` 返回 501，云端模拟交易尚未启用**
- **python/**：Python v2 本机版，包含独立的纸面模拟账本、保守撮合、分批退出和移动止盈测试功能。不会向券商提交订单
- 两个版本均**没有接入实时行情、自动新闻、AI API、券商、自动通知或常驻 24 小时监控**。按一次“扫描”只分析已导入或明确标注的演示数据
- 演示数据和模拟成交不代表真实业绩；资金规则及退出参数未经过收益或样本外有效性验证，不构成投资建议

## 目录

```text
starfolio/
├── python/                  # Python 3.11+、SQLite、静态中文前端
│   ├── investment_watch.py  # 本机服务、API、CLI、研究规则
│   ├── paper.py             # 纸面模拟与保守撮合
│   ├── paper_exits.py       # 分批退出 / 移动止盈测试
│   ├── static/
│   ├── tests/
│   ├── examples/            # 明确标注的虚构格式样本
│   └── docs/
├── web/                     # Vue 3 + Element Plus / vinext / Workers / D1
│   ├── ui/                  # Vue 单文件组件、Element Plus、API 客户端
│   ├── app/                 # 认证页面、API 及薄层 React/Vue 生命周期桥
│   ├── lib/                 # 规则、身份校验、持久化
│   ├── db/                  # Drizzle schema
│   ├── drizzle/             # schema-only 数据库迁移及元数据
│   ├── tests/
│   ├── build/               # Sites 构建适配源码，非生成产物
│   ├── .openai/hosting.json # 无真实项目 ID 的通用配置模板
│   └── package-lock.json    # 锁定 Web 依赖
├── docs/                    # 功能对照、部署边界、验证记录
└── scripts/                 # 本机验收与暂存区隐私检查
```

## 运行 Python 本机版

运行依赖全部来自 Python 标准库，无需 pip 安装，也没有第三方运行依赖锁文件。

```bash
cd python
python3 investment_watch.py serve
```

在同一台电脑打开 http://127.0.0.1:8765。首次运行建立演示数据库；需要空工作区时使用：

```bash
python3 investment_watch.py --db data/my-watch.db serve --empty
```

该服务只面向本机，不带互联网账号认证。不要直接公开端口，也不要通过改写 Host 限制绕过保护。完整操作与纸面成交示例见 [Python 说明](python/README.md)。

## 运行 Web 开发版

需要 Node.js 22.13+、npm，以及允许本地 Worker/D1 模拟器运行的环境。

```bash
cd web
npm ci
npm run build
npm run typecheck
npm test
npm run dev
```

管理界面使用 Element Plus 的布局、菜单、表格、表单、对话框和反馈组件；`ui/ResearchWorkspace.vue` 是可编辑界面源码。React 仅保留为既有 vinext 服务端运行时的薄层承载，界面不是手工仿制 Element UI。依赖随 lockfile 安装并打包，不从第三方 CDN 动态加载。

Web 依赖可信 Sites 身份入口与 D1 绑定。仓库的 `hosting.json` 只声明 `DB` 绑定，不携带任何现有站点项目 ID、所有者信息或凭证。持久化读写前需要在自己的本地 D1 环境应用 `drizzle/` 中的迁移，见 [部署说明](docs/DEPLOYMENT.md)。

本地开发模拟登录只用于环回地址，不是真实用户认证。不要将开发服务直接暴露到互联网，也不要把请求头身份检查当作独立的公网认证方案。

## 验证

```bash
# 先在 web/ 完成 npm ci，再从仓库根目录运行
bash scripts/verify.sh

# 在提交前检查将要发布的暂存文件；结果不会打印秘密原文
git add .
python3 scripts/check-staged-safety.py
```

可复跑的验证包含 Python 113 项、Python 前端源码级交互检查、Web 20 项规则/安全检查、5 项 SQLite 原子性检查、8 项实际 Element Plus 组件流程检查（jsdom）、构建、Vue SFC / TypeScript 类型检查及 lint。具体结果与未覆盖范围见 [验证记录](docs/VALIDATION.md)。这些检查不等于浏览器视觉或生产端到端验收。

## 数据与发布安全

- 数据库、真实持仓、运行日志、凭证、依赖目录、构建缓存和原站点部署身份均不在本仓库中
- `python/examples/portfolio-template.csv` 是虚构格式样本；请勿把实际账户、身份证、登录信息或真实资产清单提交到 Git
- Web 持仓导入会上传到所部署站点的云端存储；Python 持仓导入保存在运行它的电脑。请根据需要选择版本
- 发布源码不会把 Python 模拟引擎部署到网站，也不会自动开通任何付费服务、API 或调度
- 仓库未另行授予整体开源许可；保留的第三方组件和构建适配器许可见相应文件。若要公开分发或商用，请先确认所需授权

更多：[功能对照](docs/PARITY.md) · [部署边界](docs/DEPLOYMENT.md) · [来源与整理说明](docs/SOURCE_PROVENANCE.md)
