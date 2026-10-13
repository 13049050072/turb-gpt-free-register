# Sub2API 与 ChatGPT2API 自动巡检

## Goal

为现有 Sub2API-401 与 ChatGPT2API-401 页面增加可开启、可调整周期的后台自动巡检。开启后，服务端每隔配置的周期（用户提出 30 分钟或 1 小时，并希望可调整）检查对应账号状态；发现 401/unauthorized 时，Sub2API 自动进入现有 Codex OAuth 补跑与精确旧 ID 清理流程，ChatGPT2API 自动进入现有查活（liveness）与精确远端 ID AT 写回流程。页面能够查看启用状态、周期、最近一次运行、当前进度和错误，但关闭页面不应停止后台任务。

## What I already know

* 用户要求同时覆盖 Sub2API401 和 ChatGPT2API401，逻辑复用刚完成的手动检测/补跑/查活流程，而不是再造一套凭证处理逻辑。
* 当前项目已经有 `core/sub2api_401_service.py`、`core/chatgpt2api_401_service.py` 两套持久化审计与后台队列；两个页面分别是 `webui/templates/sub2api_401.html` 与 `webui/templates/chatgpt2api_401.html`。
* `webui/app.py:create_app(auth_code: str | None = None) -> Flask` 当前构造两个 401 service，并注册各自的 state/detect/retry/refresh/cleanup/replace API。
* Sub2API 手动路径：`detect()` 调用 Sub2API batch-refresh；选中的 unauthorized ID 进入现有 Codex OAuth 补跑；成功后验证同邮箱唯一健康替代 ID，再对精确旧 ID 做自动清理。
* ChatGPT2API 手动路径：`detect()` 定位明确失效 AT；`refresh()` 以 HTTP 202 将选中远端 ID 加入固定并发 5 的后台查活/写回队列；worker 调用 `run_liveness_check()`，验证新 AT 与 RT 证据后仅 PATCH 锁定远端 ID。
* 当前页面状态轮询只服务于已手动启动的任务：两个模板都使用 3000ms state 轮询，不等同于服务端周期巡检。
* `core/live_check_service.py` 已有可复用的并发查活服务；`core/chatgpt2api_import_service.py` 有持久化队列 worker 的实现模式；没有发现现成的通用周期调度器。
* 现有安全边界必须保持：Sub2API 只对精确 account_id 操作且不能按邮箱批量删除；ChatGPT2API 只对锁定的精确远端 ID 写 AT；不把 AT/RT/管理密钥写入日志、审计或 API 响应。

## Assumptions (temporary)

* 自动巡检默认关闭，必须由页面明确开启；保存开启后立即执行一次，服务重启后启用状态与周期从持久化配置恢复。
* Sub2API 与 ChatGPT2API 各自保存 enabled/interval/last-run/progress/error，不共享开关或周期；任一巡检器故障不阻塞另一类。
* 自动巡检运行在单个 Web 进程内，但配置和最近运行状态必须持久化，避免重启后丢失设置；同一巡检器不能并发重入。
* 自动巡检复用两个现有 `detect()` 及后续队列，不直接调用页面 API，不通过浏览器触发，不调用不必要的昂贵路径两次。
* 一次巡检产生的可处理账号应受现有队列上限、精确 ID 快照、幂等状态和并发门禁约束；上一轮未结束时不启动下一轮。

## Open Questions

* （已决定）周期控件提供 30 分钟、1 小时和自定义分钟数；默认 30 分钟，自定义范围 5–1440 分钟。
* （已决定）启用后立即执行一次，之后按配置周期运行。
* （已决定）Sub2API 与 ChatGPT2API 分别独立开启和配置。

## Requirements (evolving)

* [x] 为 Sub2API401 和 ChatGPT2API401 分别提供独立自动巡检开关。
* [x] 两类巡检周期均可独立调整，提供 30 分钟、1 小时和自定义 5–1440 分钟，默认 30 分钟。
* [x] Sub2API 自动巡检发现 unauthorized/401 后，复用现有补跑、替代 ID 验证、精确旧 ID 清理流程。
* [x] ChatGPT2API 自动巡检发现明确失效/401 后，复用现有查活、AT 写回、RT 保留验证流程。
* [x] 巡检运行在服务端后台，关闭页面不影响任务；页面可读取配置、最近运行与进度。
* [x] 自动巡检不可与手动检测/补跑/查活产生重复任务或破坏现有审计状态。
* [x] 失败可重试，记录有界非敏感错误；不得持久化或输出凭证、密钥、授权头。
* [x] 保持现有 API 安全门禁、精确 ID 操作和缓存安全响应头。

## Acceptance Criteria (evolving)

* [x] 模板/API 测试覆盖两个页面的独立开关、独立周期读取/保存、状态展示和自动动作入口。
* [x] 测试证明禁用时不运行，启用后按周期触发，服务重启能恢复配置，上一轮未完成时不会重入。
* [x] 测试证明 Sub2API 自动路径只处理检测到的 unauthorized 项，并最终复用现有补跑/精确清理门禁。
* [x] 测试证明 ChatGPT2API 自动路径只处理检测到的失效项，并最终复用现有查活/精确远端 ID 写回门禁。
* [x] 测试覆盖手动操作与自动操作并发、网络错误、队列满、进程重启和状态持久化。
* [x] Sub2API、ChatGPT2API 相关测试、全量 Python 编译、内联 JavaScript `node --check`、`git diff --check` 通过；全量测试中的非相关失败需在 clean HEAD 原样复现。
* [ ] 生成部署前可复核的修改、验证、回滚证据；部署后验证 Oracle 容器健康与两个巡检 API/页面。

## Definition of Done (team quality bar)

* Tests added/updated for scheduler persistence, cadence, single-flight, both adapters/services, API validation, and templates.
* `py_compile`, focused/related/full tests, inline JavaScript syntax check, and `git diff --check` run with literal results recorded.
* Backend spec updated with public signatures, state machine, persistence, concurrency and security contracts.
* No production secret is copied into source, logs, task artifacts, or HTML.
* Commit pushed and Oracle deployment/smoke verification completed only after local evidence passes.

## Out of Scope (explicit)

* 不改变现有 Sub2API/ChatGPT2API 账号导入、注册、普通查活、CPA 兼容或 quota 策略。
* 不新增按邮箱删除、按邮箱更新或批量无证据替换逻辑。
* 不把自动巡检实现成浏览器端 `setInterval`，不依赖页面保持打开。
* 不在本任务中扩大为分布式调度/多实例 leader election；若线上拓扑变化需另立任务。
* 不自动处理无法唯一映射、状态不明确、证据漂移或队列冲突的账号；这些继续保留为人工复核状态。

## Technical Approach

* Add `core/credential_health_scheduler.py`: one versioned, atomic JSON state file with two independent job records, one coordinator daemon, one two-slot executor, per-job active-future single-flight, event-driven waits, and an injectable `run_due(now)` seam for deterministic tests.
* Add reusable workflow adapters/coordinators so the scheduler calls existing business gates rather than Flask routes or duplicated credential logic. Sub2API uses `detect -> select retryable exact IDs -> existing Codex retry/post-detect/cleanup`; ChatGPT2API uses `detect -> select pending retryable IDs -> existing refresh queue -> terminal observation`.
* Add independent scheduler GET/PUT API endpoints and include a sanitized scheduler projection in each existing 401 state response. Validate literal booleans and integer intervals in `5..1440`; default disabled and 30 minutes; enabling persists `next_run_at=now` for immediate execution.
* Add a scheduler card to both templates with independent toggle/presets/custom interval, recent lifecycle/progress/error, and existing single-flight state polling. The browser never calls periodic mutation/detect endpoints.
* Persist the scheduler JSON under the existing writable data-volume mapping with atomic `fsync`/`os.replace`, mode `0600`, bounded redacted errors, and restart recovery for interrupted runs. Current deployment is one Gunicorn worker; multi-process leadership is explicitly out of scope.

## Decision (ADR-lite)

**Context**: Two existing 401 workflows already implement exact-ID safety, evidence validation, queueing, and redaction. A periodic feature must survive page closure and restart without creating a second mutation path or duplicate work.

**Decision**: Use one process-local persistence-backed scheduler with two independently configurable jobs. Each job runs immediately when enabled, uses completion-relative cadence, and is single-flight. Both automatic paths reuse extracted shared orchestration from the existing manual workflows.

**Consequences**: The implementation has no new scheduler dependency and is deterministic to test, but it remains valid only for the current one-Gunicorn-worker topology. A future multi-process deployment requires a leader lease or external scheduler before enabling this feature.

## Research References

* [`research/scheduler-pattern.md`](research/scheduler-pattern.md) — repository-backed recommendation for event-driven persistence, restart recovery, single-flight, and shared workflow coordinators.

## Technical Notes

* Existing Sub2API contract: `.trellis/spec/backend/sub2api-credential-rotation.md`.
* Existing ChatGPT2API contract: `.trellis/spec/backend/chatgpt2api-credential-rotation.md`.
* Key source boundaries: `webui/app.py`, `core/sub2api_401_service.py`, `core/chatgpt2api_401_service.py`, `core/live_check_service.py`, `core/chatgpt2api_import_service.py`, `webui/templates/sub2api_401.html`, and `webui/templates/chatgpt2api_401.html`.
* The scheduler stores only config, timestamps, phases, counts, and bounded errors; product audit files remain the source of per-account sanitized evidence.
