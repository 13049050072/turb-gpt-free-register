# Sub2API 重复凭证自动收敛

## Goal

修复 Sub2API 401 轮换在生成替代账号后仍保留旧 `account_id` 的问题。未来补跑完成并验证唯一健康替代账号后，自动删除检测时锁定的旧 ID；同时识别并安全收敛当前遗留的同邮箱双凭证。页面默认补跑并发改为 3。

## Observed Production Evidence

- 2026-10-13 Oracle 上 `/sub2api-401` 显示旧 ID `25` 为 `cleanup_ready`，替代 ID `40` 已验证，但旧 ID 未自动删除。
- Sub2API 账号列表中同邮箱 `corsage.scalene0j@icloud.com` 同时存在 `#25`（`error`）和 `#40`（`active`）。
- 审计文件还显示四组历史双凭证：`16/21`、`17/20`、`18/24`、`19/22`；两条均可刷新，但后者均有唯一更晚的 `created_at`。
- 当前模板并发下拉框默认选中第一个值 1；用户要求默认 3。

## Requirements

### Preserve exact-ID safety

- 禁止按邮箱批量删除。
- 所有删除目标必须是服务状态中锁定的精确旧 `account_id`。
- 删除前必须重新列出并刷新候选替代 ID，确认替代账号仍存在、同邮箱且健康。
- 删除后必须重新列出，确认旧 ID 消失；失败时保留可重试审计证据。
- 不记录、返回或持久化管理密钥、OAuth token/code、callback URL 或完整凭证。

### Account metadata

- Sub2API 列表适配器保留经过限制和校验的非敏感 `created_at` 字段，用于同邮箱重复账号的先后关系判定。
- `created_at` 缺失、无效或出现并列最新时间时，不得自动判定旧账号。
- 现有 `platform=openai`、`type=oauth`、分页边界、整数 wire ID 与脱敏约束保持不变。

### Future retry auto-cleanup

- `POST /api/sub2api-401/retry` 仍由用户显式选择旧 ID 并确认启动补跑。
- 补跑完成后，继续用旧 ID 快照、同邮箱唯一新 ID 和 batch-refresh 健康结果验证替代关系。
- 一旦选中的旧 ID 进入 `cleanup_ready`，后台流程立即调用精确 ID cleanup，不再要求用户第二次点击清理按钮。
- 自动删除只处理本次 batch 选中的旧 ID；不得处理其他同邮箱账号。
- 自动 cleanup 失败时保持 `cleanup_ready`，页面继续提供手动重试清理，并展示受控原因。
- 成功时状态直接进入 `cleaned`，并确认替代 ID 保持健康。

### Historical duplicate convergence

检测同邮箱多个 OpenAI OAuth account ID 时：

1. 若恰好一个账号健康、其余账号 unauthorized/error，则健康账号为 keeper，其余精确 ID 可进入 `cleanup_ready`。
2. 若多个账号均健康，只有在存在唯一最新且合法的 `created_at` 时，最新 ID 为 keeper，所有更早 ID 分别进入 `cleanup_ready`。
3. 若 keeper 不唯一、时间缺失/并列、邮箱歧义、账号快照变化或刷新结果不完整，则保持 `ambiguous`，禁止删除。
4. 检测本身不执行删除；历史重复项由页面现有 cleanup 操作显式确认。此次部署后允许运维对已确认的五组遗留重复项执行一次精确 ID 清理。

### UI and defaults

- Sub2API 401 页面并发下拉框默认选中 3。
- `/api/sub2api-401/retry` 未传 `workers` 时默认 3，仍限制在 `1..8`。
- 补跑确认文案明确说明：唯一替代 ID 验证成功后会自动删除锁定旧 ID。
- cleanup 按钮文案明确其用途是历史重复项或自动清理失败重试。
- 页面继续展示旧 ID、补跑前/后 ID、唯一替代 ID、锁定清理 ID 与状态原因。

## Acceptance Criteria

- [ ] 适配器 `created_at` 解析/限制测试通过，秘密字段不泄漏。
- [ ] 未来补跑：唯一健康新 ID 出现后，自动删除精确旧 ID并验证消失，最终状态 `cleaned`。
- [ ] 自动删除失败时保留 `cleanup_ready` 与可重试证据。
- [ ] 自动清理不会删除未属于本次 batch 的其他账号。
- [ ] 历史重复：唯一健康 keeper、唯一最新 keeper、时间缺失和时间并列均有测试。
- [ ] 同邮箱两个健康账号且唯一最新时，旧 ID 不再显示为 `ambiguous`，而是可精确清理。
- [ ] 默认并发为 3（模板选择、前端 fallback、API fallback）。
- [ ] 相关 adapter/service/API/template 测试、现有 Sub2API/Codex 回归、`py_compile`、JS syntax、`git diff --check` 通过。
- [ ] 部署 Oracle 后清理当前 5 个遗留旧 ID，复查每个邮箱只剩唯一 keeper ID。

## Out of Scope

- 修改 Sub2API 服务端。
- 按邮箱直接删除账号。
- 自动处理无法唯一确认 keeper 的重复组。
- 修改普通 Codex 注册、ChatGPT2API 401、Agent Token 上传或 CPA 兼容流程。
