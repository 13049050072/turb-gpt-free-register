# ChatGPT2API 401 全量队列查活与自动写回

## Goal

修复 Oracle 上 `/chatgpt2api-401` 页面批量查活触发 Cloudflare HTTP 524、单批最多 50 个账号以及查活成功后不会自动修改 ChatGPT2API 远端账号的问题。将同步长请求改为后台队列：支持全选当前所有安全映射候选，固定 5 并发复用现有本地查活流程，并在每个账号获得且持久化新的 AT 后自动精确写回锁定的 ChatGPT2API 远端账号 ID。

## Context

- 当前 `POST /api/chatgpt2api-401/refresh` 在请求线程内串行执行 `ChatGPT2API401Service.refresh()`，每个账号都会运行完整的本地查活；选择 50 个账号时请求时间超过 Cloudflare 上限并返回 524。
- 当前 Web/API/Service 都将批量上限硬编码为 50。
- 当前状态机将查活与远端替换拆成两个人工步骤；查活成功仅进入 `ready`，不会修改 ChatGPT2API。
- 项目已有安全的精确 ID 替换逻辑：写回前后校验远端 ID、邮箱唯一性、旧/新 AT 摘要及 RT 保留摘要；`POST /api/accounts/update` 只发送 `{id, access_token}` 并回读验证。
- 项目已有 `run_account_live_check()` 正常查活流程，负责 DB claim、代理路由、日志、刷新并持久化本地 AT；本功能必须复用它。
- 部署使用单 Gunicorn worker + gthread，可在进程内维护有界后台执行器；重启后需将未完成状态收敛为可重试失败。

## Requirements

### Queue API

- `POST /api/chatgpt2api-401/refresh` 改为入队接口，收到合法请求后立即返回 HTTP 202，不等待任一账号完成。
- 请求必须带 `remote_ids` 非空数组和字面量 `confirm_replace: true`；没有明确确认时不得入队或写回远端。
- 支持全选当前页面全部候选，不再限制 50；仍需设置足以覆盖当前账号池的有界安全上限（至少 2,000，建议 5,000），超限返回 400。
- 固定后台并发为 5；响应和状态中可展示 workers/queued/active/completed/failed 等非敏感进度。
- 同一远端 ID 已入队、执行中或不再可重试时不得重复提交；返回 bounded skipped reason。

### Background processing

- 每个任务必须按检测时锁定的 `remote_id`、邮箱、本地账号 ID 和凭证摘要执行。
- 任务开始前重新验证远端 ID/邮箱唯一映射、本地 ID/邮箱唯一映射、旧 AT 摘要和 RT 保留摘要；变化则标记 `conflict`，不查活、不写回。
- 复用现有 `run_account_live_check(local_account_id)`；不得实现另一套登录/刷新流程。
- 5 个 worker 可并发执行不同账号的查活；远端写回仍必须经过既有精确 ID 串行门禁，避免共享审计状态竞态。
- 查活成功必须验证返回 AT 摘要等于本地持久化 AT 摘要、不同于锁定旧远端 AT、不同于查活前本地 AT。
- 通过后自动调用既有精确替换流程，仅更新锁定的远端 ID；必须验证 exact `updated_ids`、远端 ID/邮箱仍唯一、AT 变为新摘要、RT 保留摘要未变。
- 成功状态直接变为 `replaced`；查活失败为 `failed`（可重试）；快照漂移或写回验证失败为 `conflict`（需重新检测）。
- 远端更新失败时不得声称成功；审计必须保留失败阶段与安全原因，不包含任何 AT、RT、管理密钥或响应体。

### Persistence and restart

- 继续使用 `chatgpt2api_401_audit.json` 原子写入、`0600` 和符号链接安全语义。
- 新增 `queued` 状态（如采用）；服务初始化或重新加载时，遗留 `queued`/`refreshing` 必须转为可重试 `failed`，原因说明进程中断。
- `state()` 必须可被页面轮询，显示每个账号的安全状态和汇总计数；不得输出原始凭证。

### Web UI

- “查活刷新”改为“查活并写回”，确认文案明确说明会自动更新锁定的 ChatGPT2API 远端账号 AT。
- 全选必须选择所有 actionable 候选，不进行 50 条截断。
- 提交后显示“已加入后台队列（并发 5）”，立即恢复页面响应；每 2–5 秒轮询状态，直到 queued/refreshing 为 0。
- 页面关闭不影响后台任务；重新打开页面可继续看到持久化进度。
- 保留手工“确认原位替换”作为历史 `ready` 状态的兼容/恢复路径，或安全地将旧 `ready` 项纳入明确确认的自动写回；不得降低精确 ID 门禁。

### Compatibility

- 不修改普通账号查活页面、注册流程、ChatGPT2API 导入、Sub2API 401、CPA 兼容流程。
- 不修改 ChatGPT2API 服务端代码。
- 继续只更新 AT；RT 作为必须保持不变的证据，不向远端写入 RT。

## Acceptance Criteria

- [ ] 选择超过 50 个、直至全部候选的请求可以成功入队；有界上限与非法输入测试通过。
- [ ] 入队 API 在后台任务完成前返回 202，测试证明不会同步等待查活。
- [ ] 固定 5 worker，并发测试证明最多 5 个查活同时运行且全部候选最终被处理。
- [ ] 每个任务复用 `run_account_live_check()`，并验证返回/持久化/旧 AT 三方摘要。
- [ ] 查活成功后自动更新 exact remote ID，AT 回读为新摘要，RT 摘要不变，状态为 `replaced`。
- [ ] 查活失败、映射漂移、旧 AT 漂移、RT 漂移、update acknowledgement 不匹配和回读失败均不会误报成功。
- [ ] 重复入队被跳过；重启后 queued/refreshing 收敛为可重试失败。
- [ ] 页面支持全选、文案明确自动写回、展示并发 5，并轮询后台进度。
- [ ] 审计/API/日志不包含 AT、RT、管理密钥、OAuth code、callback URL 或完整上游响应。
- [ ] ChatGPT2API focused/API/template、normal liveness、import、Sub2API 回归、`py_compile`、inline JS `node --check`、`git diff --check` 和全量测试完成。
- [ ] 部署 Oracle 后，页面不再产生 524；先用小批量真实账号验证 exact ID 的远端 AT 摘要变化，再允许大批量操作。

## Out of Scope

- 修改 ChatGPT2API 服务端接口或数据库。
- 更新远端 refresh token、password、client_id、id_token 或其他字段。
- 自动处理邮箱/ID 映射不唯一的账号。
- 绕过查活失败或快照冲突强制写入远端。
- 多 Gunicorn worker/多副本分布式队列；当前部署保持单 worker。
