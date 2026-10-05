# 星策 Starfolio · Vue 3 + Element Plus 管理版

中文研究工作台，以 Vue 3 + Element Plus 构建六页管理界面；空的持久化工作区起步。没有复制任何 Python 数据库、持仓、凭证或审计历史。

## 已启用

自选增改移除；人工行情 JSON 导入；同窗基线资金异常扫描；人工独立录入事实阶段、相对预期和价格观察证据；提醒复核与去重；持仓参考 CSV 原子替换；操作审计；日报下载。demo 仅按请求生成明确虚构数据，与 imported 隔离。实时模式失败关闭。

## 明确未启用

没有自动行情/新闻抓取、模型调用、外部通知、调度或全天监控承诺。没有券商或真实交易。云端没有模拟账户执行，/api/paper/* 全部返回 501，界面不提供相应写入控件，也不虚构资金、交易、收益。

独立 Python v2 源码版实现基础纸面账本及分批/移动退出测试；这里没有将其复杂接收时间、后续报价和日历约束简化迁移。

## 安全与存储

原部署使用 Sites 私有访问策略。这个公开源码仓库仅保留通用部署模板，不包含原站点身份或继承其访问策略；新部署必须另行配置可信登录网关和访问范围。不能把可由客户端随意提供的身份头当作公网认证。Worker 全路由身份门控，首页使用官方 ChatGPT 登录助手，API 再次检查身份；状态由 Site-scoped 用户 ID 分区。写入要求精确同源 Origin、JSON、自定义请求头及操作编号。D1 使用生成的 schema-only Drizzle 迁移。一次写入将整个工作区与审计通过 revision CAS 更新，并用 D1 transactional batch 原子保存幂等回执；冲突有限重试。没有 GET 写入状态或运行时建表。

持仓 CSV 在提交前明确提示上传到私有云端且替换已有参考持仓。未预载任何实际持仓。所有用户文字显示前转义。

## 检查

- node --test tests/engine.test.mjs（20 项）
- python3 tests/atomicity.test.py（5 项）
- npm run typecheck（Vue SFC + TypeScript）
- npm run test:ui（8 项 jsdom 实际 Element Plus 组件流程；不等于浏览器视觉验收）
- npm run lint
- 支持的 Sites Worker 构建
- 本地 D1 schema 应用成功

此环境是 portable，不能使用 managed preview。尝试 CLI 本地 HTTP 验证时 Wrangler 遭遇 uv_interface_addresses 系统限制；未完成浏览器视觉或端到端 HTTP 验证，也未完成支持上下文中的 WebMCP 验证。上线以原生 Sites 部署结果及私有访问策略验证为准；没有访问发布 URL 来绕过本机浏览器限制。

## 前端架构与复现

- `ui/ResearchWorkspace.vue`：可编辑的 Vue 单文件管理界面，实际使用 Element Plus 菜单、表格、表单、校验、弹窗、抽屉、上传、标签页、提示及空状态组件
- `ui/entry.ts`：Vue createApp 与 Element Plus（中文语言包）初始化，使用 npm 打包依赖，无运行时 CDN
- `ui/api.ts`、`ui/types.ts`：既有接口适配和数据模型；`ui/workspace.css` 仅布局、主题和响应式样式
- `app/VueWorkspace.tsx`：薄 React 挂载/清理桥；既有 Vinext / Sites 服务端外壳保留，认证、Workers API 和 D1 不变
- `vite.config.ts` 使用官方 `@vitejs/plugin-vue` 编译 SFC；`npm ci && npm run build` 从 clean clone 重新生成全部部署资产。无手工预生成的 public 管理端 bundle
- 旧手写 DOM 模板 `public/app.js`、`lib/dashboard-markup.ts` 已移除。原 Sites 框架依赖保留，避免无关升级或清理

组件用法来源：
- https://element-plus.org/en-US/guide/quickstart
- https://vuejs.org/guide/scaling-up/sfc

本合并仓库的通用运行、迁移和部署边界见 [部署说明](../docs/DEPLOYMENT.md)，本次验证范围见 [验证记录](../docs/VALIDATION.md)。公开源码不包含研究数据、持仓或原站点 project_id。
