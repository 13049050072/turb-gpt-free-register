# Sub2API 401 凭证轮换

## Goal

将现有 CPA 401 检测与安全轮换功能替换为 Sub2API 版本：检测 Sub2API 中 OpenAI OAuth 账号的刷新结果，优先补跑 unauthorized/401 账号，并在确认 Sub2API 已生成替代账号后才允许删除旧账号。

## Context

- 用户明确停止 CPA 方案，改为 Sub2API，功能逻辑保持一致。
- 项目已有 Sub2API 配置与 Codex OAuth 驱动：`SUB2API_API_BASE`、`SUB2API_API_KEY`/`SUB2API_API_TOKEN`、可配置鉴权头、`SUB2API_API_TIMEOUT`，以及 `CODEX_AUTH_URL_SOURCE="sub2"` 流程。
- Sub2API 管理 API：
  - `GET /api/v1/admin/accounts`：分页列出账号，可按 `platform=openai&type=oauth` 筛选。
  - `POST /api/v1/admin/accounts/batch-refresh`：请求 `{"account_ids":[...]}`；响应包含 `total`、`success`、`failed`、`errors[{account_id,error}]` 和 `warnings`。
  - `DELETE /api/v1/admin/accounts/:id`：删除指定旧账号。
- 项目现有 CPA 页面、服务与测试可作为状态机、补跑调度、审计持久化和模板交互的行为参考，但不得继续依赖 CPA 管理 API。

## Requirements

### Sub2API adapter

- 复用 `config/sub2api.py` 现有基址、管理密钥、鉴权头和超时配置，不新增重复密钥配置。
- 管理请求不得记录或返回管理密钥、Token、OAuth code、callback URL 或完整凭证。
- 支持 Sub2API 标准响应 envelope，并对分页账号列表做有界遍历。
- 仅选择 `platform=openai` 且 `type=oauth` 的账号；其他账号不参与检测和轮换。

### Detection

- 先读取目标账号，再调用 Sub2API `batch-refresh`。
- 成功刷新的账号归类为 healthy。
- 刷新错误包含 HTTP 401、unauthorized、invalid_grant 或 invalid_token 时归类为 unauthorized，并进入可补跑状态。
- timeout 归类为 timeout；其他失败归类为 unknown。
- 使用 Sub2API `account_id` 作为远端账号稳定身份，邮箱/账号名称仅用于关联本地账号；无法唯一提取邮箱或匹配冲突时标记 ambiguous，不进入自动删除。
- 检测结果原子持久化到项目数据目录，权限保持 `0600`（平台支持时），并兼容符号链接审计路径。

### Retry and verification

- 继续复用现有 `_run_codex_retry_worker`，由当前 `CODEX_AUTH_URL_SOURCE="sub2"` 流程向 Sub2API 创建新账号。
- 补跑前记录旧 `account_id` 集合；补跑完成后重新读取 Sub2API 账号。
- 只有同一邮箱出现不在旧快照中的新 `account_id`，且旧账号仍存在时，才进入 cleanup-ready。
- 没有新账号、出现多个无法判定的候选、旧账号已变化或本地补跑失败时，不允许删除。

### Cleanup

- 清理请求必须带显式确认。
- 删除目标必须是检测时锁定且验证时仍存在的旧 `account_id`；禁止按邮箱批量删除。
- 删除后重新读取账号列表确认旧 ID 消失；失败时保留审计状态和可重试证据。

### Web UI and compatibility

- 将 `/cpa-401` 页面和 `/api/cpa-401/*` 路由替换为 Sub2API 命名（`/sub2api-401`、`/api/sub2api-401/*`）。
- 导航和页面文案全部改为 Sub2API；不保留 CPA 401 双轨入口。
- 保留检测、选择 unauthorized、补跑、确认清理、状态计数和错误展示的交互结构。
- 不修改普通 Codex 注册、Sub2API Agent Token 上传、本地导出、CPA OAuth 兼容代码或其他账号流程。

## Acceptance Criteria

- [ ] Sub2API 管理 API 的列表、批量刷新和删除适配器有单元测试，且不会输出秘密字段。
- [ ] unauthorized/401、timeout、unknown、healthy 分类测试通过。
- [ ] 页面仅展示非敏感账号标识、邮箱、状态和受控原因。
- [ ] 补跑前后使用 account ID 快照验证替代账号；未验证时不能清理。
- [ ] 清理只删除锁定的旧 account ID，且需要显式确认。
- [ ] 审计文件原子写入、`0600` 权限和符号链接目标目录行为有回归测试。
- [ ] 原 CPA 401 页面、路由、模板导航和对应测试被 Sub2API 版本替换。
- [ ] 现有 Codex/Sub2API 相关测试和模板测试通过。
- [ ] 不记录或输出凭证、Token、管理密钥、OAuth code 或 callback 内容。

## Out of Scope

- 修改 Sub2API 服务端代码。
- 调整 Sub2API 账号调度、并发、优先级、分组或配额策略。
- 自动删除未通过替代账号验证的旧账号。
- 同时维护 CPA 与 Sub2API 两套 401 页面。
- 在线写入 Oracle 的 Sub2API 配置或密钥；部署与真实检测在本地实现验证通过后单独执行。
