# Sub2API 401 补跑状态及时更新

## Goal

修复 Sub2API-401 页面在后台补跑期间不会自动显示状态变化的问题。补跑任务已经由后端异步执行并持久化状态，但页面只在初始加载或手动刷新时读取 `/api/sub2api-401/state`，导致用户误以为任务没有进展。

## Requirements

- 页面使用现有 `GET /api/sub2api-401/state`，每 3000 ms 轮询一次。
- 只有存在 `retrying` 项时轮询；页面重新打开时，如果持久化状态仍为 `retrying`，自动恢复轮询。
- 同一时间最多一个状态请求；成功读取到没有 `retrying` 项时停止定时器。
- 轮询只读取 `state`，不得调用昂贵的 `/detect`。
- 后端状态成功返回后，刷新页面行、计数和原因，不需要手工刷新。
- 轮询错误只显示有界错误文本，并在仍有 `retrying` 项时稍后重试。
- 不改变补跑、清理、选择和 API 的安全门禁。

## Acceptance

- 模板测试覆盖 3000 ms 定时器、`retrying` 守卫、初始加载恢复、终态停止、单飞请求和不调用 `/detect`。
- Sub2API 相关测试、Python 编译、内联 JavaScript `node --check`、`git diff --check` 通过。
- 全量测试中的非相关失败必须在 clean HEAD 原样复现。
