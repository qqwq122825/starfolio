# 部署配置与安全边界

本文件是部署准备说明，不会创建云资源、支付服务、接入真实数据或安装后台任务。

## Python

详细的本机运行、可信认证代理、持久磁盘和 systemd 审阅样例见 [python/docs/deployment.md](../python/docs/deployment.md)。原型 HTTP 服务绑定 `127.0.0.1`，没有公网登录系统。不要直接暴露服务或取消 Host / 同源校验。

## Web

Web 管理界面采用 Vue 3 + Element Plus，源码位于 `web/ui/`，由 Vite Vue 插件在正常构建流程中编译。既有 React/vinext 仅承载认证页面、薄层生命周期桥与服务端路由。Web 源码面向 Sites 托管的 Cloudflare Worker 与 D1 运行时，不是静态 HTML 托管包。它依赖平台在已认证请求中注入身份；应用自身只校验这些身份头是否存在及写入是否同源。生产环境必须由可信网关验证并覆盖身份头，不能允许外部客户端自填头部绕过登录。

仓库内 `web/.openai/hosting.json` 是通用模板：

```json
{
  "d1": "DB",
  "r2": null
}
```

没有原部署的 `project_id`、Cloudflare 账户 ID、真实数据库 ID、域名、邮箱、授权令牌或用户列表。`vite.config.ts` 中的全零风格数据库 ID 仅为本地构建占位符，不是远程资源标识。

需要部署时，在自己的隔离工作副本中通过受支持的托管流程配置新项目及访问策略；不要将生成的真实部署配置、凭证或生产数据提交到 Git。仅克隆源码不继承原站点的私有访问策略。发布源代码不等于允许将研究内容或持仓公开。

### 本地开发

```bash
cd web
npm ci
npm run build
# 仅修改当前工作副本的本地模拟 D1，不连接远程数据库
node --import ./scripts/sites-env.mjs ./node_modules/wrangler/bin/wrangler.js \
  d1 execute DB --local --config dist/server/wrangler.json \
  --persist-to .wrangler/state --file drizzle/0000_lyrical_morlocks.sql
npm start
```

命令依赖操作系统允许 Wrangler / Miniflare 创建本地服务。该迁移属于首次空数据库初始化；应用过的数据库不要重复执行同一个迁移。后续迁移应通过正式迁移流程与备份策略管理。

`npm start` 使用构建产物；开发时可用 `npm run dev`。开发模拟登录只用于 localhost / 环回地址，不是生产认证。开发身份为固定测试身份，不包含任何真实用户标识。

### 生产上线前

1. 确认可信登录网关、站点访问范围及 D1 `DB` 绑定
2. 使用受支持的部署流程应用 `drizzle/` 的 schema-only 迁移，不要上传本地数据库文件
3. 建立数据备份、恢复、保留期限和运行健康检查
4. 用真实允许访问的账号验证未登录跳转、身份隔离、重复提交、冲突写入、CSV 上传确认及失败提示
5. 不启用实时、模拟或 24 小时承诺，除非相应实现、数据授权和验收另外完成

现有提交不包含部署命令、自动发布工作流或远程凭证。构建产物不应未经访问控制验证就直接公开。
