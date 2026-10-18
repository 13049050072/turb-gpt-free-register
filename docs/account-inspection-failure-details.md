# 巡检查活失败详情

## 数据边界

`account_inspection_failures` 以 `(inspection_id, account_id)` 为主键独立保存失败明细，按任务分页，不把账号 ID 清单放进统计 JSON。明细保存当时的账号邮箱、状态、时间和有界原因；账号后续查活、删除或服务重启不会改写历史记录。

失败原因在持久化边界先经过低层公共 `redact_error()` 脱敏并限制为 500 字符，API 返回前再次脱敏。覆盖 `password`、access/refresh/id token、`Authorization`、Bearer/Basic 值、OAuth code/callback URL、`Proxy-Authorization`、有无 scheme 的代理凭据（含 HTTP/SOCKS）、代理用户/密码、client secret、cookie/session/JWT、private key 和 token-shaped 值；普通状态文本（如 `token expired`）保持可读。不会保存完整登录结果、AT、Cookie 或 BrowserSession 对象。

## API 合同

`GET /api/accounts/inspections/{inspection_id}/failures?page=1` 沿用 WebUI 鉴权。`inspection_id` 必须是 `1..9223372036854775807` 的 SQLite signed 64-bit 整数；超出范围在数据库访问前返回 `400`。非法页码返回 `400`，任务不存在返回 `404`。

成功、400 和 404 均返回：

- `Cache-Control: no-store, max-age=0`
- `Pragma: no-cache`
- `X-Content-Type-Options: nosniff`

## UI 行为

现代和 Legacy 页面每页显示 20 条，切换任务时隔离旧响应并转义邮箱、原因和未知状态。列表刷新保持单个 in-flight 请求；重叠调用合并为下一次刷新，连续轮询不会永久阻止已完成响应渲染。明确由删除/归档触发的失效刷新会跳过旧快照，避免已删除账号回显。

## 可重跑验证

从仓库根目录执行：

```sh
.venv/bin/python -m unittest tests.test_account_inspection
node tests/test_account_list_reload_ui.cjs
node tests/test_account_inspection_ui.cjs
.venv/bin/python -m py_compile core/error_redaction.py core/db.py core/account_inspection_service.py core/credential_health_scheduler.py webui/app.py
git diff --check
```

本次 pre-merge 观察结果：Python 专项 **82 项 OK**；两个 Node UI 回归均通过；Python 编译与 diff 检查通过。未声明全量测试通过：已知 clean-HEAD 仍有两个失败，属于本次范围之外。未执行真实账号网络、OAuth、代理或 Oracle 验证。
