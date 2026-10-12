# ChatGPT2API 401 AT 安全轮换

## Goal

为 Oracle 上的 ChatGPT2API Web 账号池增加独立的 401/AT 过期检测页面：仅对能够与本地账号唯一映射的失效远端账号执行现有查活流程，在本地成功获取并持久化不同的新 AT 后，经用户显式确认，按锁定的远端账号 ID 更新 AT，并验证其他凭证字段未变化。

## Context

- 本项目已将注册成功的 ChatGPT Web 账号同步到 Oracle 上的 ChatGPT2API。
- ChatGPT2API 中同步的是 access token；AT 会过期，远端账号会显示 invalid/401。
- 本地已有账号查活流程，可刷新 AT 并写回本地账号记录。
- ChatGPT2API 提供已脱敏的账号列表、独立凭证读取和按稳定账号 ID 更新接口：
  - `GET /api/accounts` 仅返回映射与状态信息，不返回任何凭证值。
  - `GET /api/accounts/{id}/access-token` 提供 AT。
  - `GET /api/accounts/{id}/refresh-token` 提供 RT；HTTP 404 表示明确不存在 RT，HTTP 200 但没有非空 `refresh_token` 表示冲突。
  - `POST /api/accounts/update` 的精确请求体为 `{id, access_token}`，响应必须由 `updated_ids` 确认同一个精确 ID。
- 功能逻辑参考 Sub2API 401 页面，但本功能是同一远端记录的 AT 字段安全替换，不删除账号，也不创建替代远端 ID。

## Requirements

### Adapter and detection

- 复用 `config/chatgpt2api.py` 的 `CHATGPT2API_BASE_URL`、`CHATGPT2API_MANAGEMENT_KEY`、`CHATGPT2API_REQUEST_TIMEOUT`，不新增重复密钥。
- 有界分页读取 ChatGPT2API 账号；支持直接响应和常见 envelope。
- 仅将明确包含 `invalid`、`expired`、`unauthorized`、HTTP 401 或等价 AT 失效信号的账号纳入候选。
- 使用 ChatGPT2API 稳定远端 ID 锁定目标；邮箱只用于将一个远端 ID 唯一关联到一个本地账号。
- 远端 ID 缺失/重复、邮箱缺失/冲突、同邮箱多远端账号、同邮箱多本地账号时均标记为不可操作。
- 检测先通过脱敏列表验证稳定远端 ID 与邮箱映射唯一，再从独立端点读取凭证证据。
- 快照仅保存旧 AT 的 SHA-256，以及 RT 的存在性和值摘要；`_PRESERVED_FIELDS` 仅包含 `refresh_token`。
- `password`、`client_id`、`last_refresh` 和 `id_token` 不得猜测，也不得宣称已保留；不得持久化或返回任何明文凭证。

### Liveness refresh

- 复用普通账号查活链路及其 DB claim、网络路由、代理回退、日志和持久化逻辑。
- 查活前复核锁定远端 ID、脱敏列表中的唯一邮箱映射、旧 AT 摘要和 RT 存在性/值摘要未变化。
- 查活成功必须同时满足：响应返回可验证 AT、本地已持久化相同 AT、新 AT 不同于锁定远端旧 AT、且不同于查活前本地 AT。
- 未满足任一条件时不得进入可替换状态。

### Confirmed replacement

- 替换请求必须带字面量布尔值 `confirm_replace=true`；否则仅返回预览。
- 更新目标必须是检测时锁定的精确远端 ID，禁止按邮箱批量更新。
- 更新前再次复核远端与本地映射、旧 AT、RT 存在性/值摘要和新 AT 摘要。
- 仅调用 `POST /api/accounts/update`，精确请求体为 `{id, access_token}`；响应必须在 `updated_ids` 中确认锁定的精确远端 ID。
- 更新确认后必须再次调用独立 AT/RT 端点并做哈希验证；同时重新读取脱敏列表，确认远端 ID 与邮箱映射仍唯一、AT 摘要等于新值且不同于旧值、RT 存在性/值摘要未变化。
- 不得根据未提供的字段推断 `password`、`client_id`、`last_refresh` 或 `id_token`，也不得作 ID-token 保留声明。
- 任一复核失败时保留冲突证据，不自动重试覆盖。

### Persistence and security

- 审计文件原子写入，平台支持时权限为 `0600`；符号链接路径必须在解析后的目标目录创建临时文件并保留符号链接。
- 页面、API、日志和审计仅展示远端 ID、邮箱、本地账号 ID、状态、受控原因、时间戳和短摘要。
- 管理密钥、AT/RT、密码、client secret、OAuth code、callback URL、Authorization header 和完整远端响应不得记录或返回。
- 同一审计路径的检测、查活和替换操作必须串行化，并在每次写操作前重载审计状态，拒绝过期实例。

### Web UI and compatibility

- 新增 `/chatgpt2api-401` 页面及 `/api/chatgpt2api-401/state|detect|refresh|replace`。
- 现代与 legacy 首页均增加独立导航入口。
- 保留检测、选择失效账号、查活、替换预览、显式确认、状态计数和错误展示结构。
- 不修改 Sub2API 401、普通注册、普通查活、ChatGPT2API 自动导入或 CPA OAuth 兼容行为。

## Acceptance Criteria

- [ ] ChatGPT2API 列表和精确 ID 更新适配器有单元测试，且不会输出秘密字段。
- [ ] invalid/expired/unauthorized/401 分类与健康账号排除测试通过。
- [ ] 缺失或冲突的远端 ID/邮箱/本地映射无法查活或替换。
- [ ] 查活复用现有正常链路，并验证返回 AT 与本地持久化 AT 一致且确实为新值。
- [ ] 替换只更新检测时锁定的远端 ID，且必须显式确认。
- [ ] 更新前后验证旧/新 AT 摘要、稳定 ID、邮箱唯一性和 RT 存在性/值摘要；不作不受支持的 ID-token 保留声明。
- [ ] 审计原子写入、`0600`、符号链接、重启恢复和并发/过期状态有回归测试。
- [ ] 页面与 API 不显示或记录管理密钥、AT/RT、密码、OAuth code、callback 或完整凭证。
- [ ] ChatGPT2API、Sub2API、普通查活和模板相关回归测试通过。

## Out of Scope

- 修改 ChatGPT2API 服务端代码。
- 自动删除 ChatGPT2API 账号。
- 按邮箱批量覆盖多个远端账号。
- 在未获得新 AT 或未完成前后快照验证时自动替换。
- 调整账号池调度、并发、优先级、分组或配额策略。
