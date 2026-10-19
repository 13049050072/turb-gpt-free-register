## 2026-10-19 - Task: 支付方式检测补齐建单风控头并修正结账前置判定

### What was done

- 支付检测的 `/backend-api/payments/checkout` 建单请求接入 Sentinel 风控头：使用现有 runner 以 `chatgpt_checkout` 流程签发 `openai-sentinel-token` / `openai-sentinel-so-token`，并带上与活动一致的 `Referer`。令牌生成失败只降级为无头请求，不阻断结账检测。
- 建单请求对 provider activity restriction、交互式挑战、5xx 增加一次带新令牌的重试；其余错误仍立即按 best-effort 记录。
- 收紧结账前置条件：offer 无法与 Plus campaign 一一对应时不再猜测首个 offer；纯语言码（如 `ja`）不再被当成账单国家；账单国家必须有确定币种，避免国家/币种不匹配导致的 400。
- 补齐代理出口常见国家的币种映射（含 VN/NG/PK 等），并在 `accounts/check` 解析中对 metadata/discount/duration/entitlement 做类型保护，畸形字段不再打断套餐解析。

### Testing

- `./.venv/bin/python -m unittest tests.test_payment_method_detection`：38 项通过（新增风控头接线、重试、双 offer 跳过、locale 国家判定用例）。
- `./.venv/bin/python -m unittest tests.test_chatgpt_plan_retry tests.test_plan_promo_details tests.test_account_plan_filter tests.test_account_export_twofa`：12 项通过。
- `./.venv/bin/python -m unittest discover -s tests -p "test_*.py"`：799 项，2 项失败（`test_chatgpt_auth_context`、`test_remail_client`），已在干净 HEAD worktree 复现，属既有问题，与本次改动无关。
- `./.venv/bin/python -m py_compile core/chatgpt_plan.py core/openai_auth.py`、`compileall -q core webui main.py`、`git diff --check`：全部通过。
- 未完成：真实账号建单链路未在本机执行。本机无可用代理出口，直连 `sentinel.openai.com` 返回 403 挑战，无法签发令牌。

### Notes

- `core/chatgpt_plan.py`：新增风控头签发与建单重试；收紧 offer/国家/币种判定；`_common_headers` 支持自定义 referer；`parse_accounts_check` 增加类型保护。
- `core/openai_auth.py`：`build_sentinel_header` 增加可选 `page_url`，供 chatgpt.com 页面的 checkout 流程使用。
- `tests/test_payment_method_detection.py`：新增风控头、重试、歧义 offer、locale 国家用例；单元测试内屏蔽真实令牌签发。
- `.trellis/tasks/10-19-payment-method-detection/notes.md`：补充建单风控头与重试约定。
- 回滚：`git checkout -- core/chatgpt_plan.py core/openai_auth.py tests/test_payment_method_detection.py`，并删除本文件。

## 2026-10-19 - Task: fb8b85c 部署到 Oracle 并做真实建单验证

### What was done

- 提交 `fb8b85c`（支付检测建单风控头 + 建单前置收紧），构建镜像 `turb-gpt-free-register:fb8b85c` 并替换 Oracle 上的容器。
- 容器重建后 healthy、重启 0 次、镜像 revision 与 `fb8b85c` 一致，本机登录页返回 302。
- 对 #918 执行真实套餐+建单探测：套餐 200、Sentinel frame 200、Sentinel req 200、`openai-sentinel-token` 与 `openai-sentinel-so-token` 均已附加，建单仍返回 400 `provider_activity_restricted`。

### Testing

- 镜像构建：`docker compose build web` 成功，`docker image inspect` 显示 `revision=fb8b85c`。
- 部署：`compose.env` 备份为 `compose.env.before-fb8b85c-20261019T142826Z`，容器 `state=running health=healthy restarts=0`。
- 部署内探测（`.tmp/payment-flow-transaction/deployed_probe_fb8b85c.py`）：`plan 200` → `sentinel frame 200` → `sentinel req 200` → `checkout 400`，结果 `payment_method_error=checkout: provider_activity_restricted`。
- 假设逐项排除（`.tmp/payment-flow-transaction/probe_context.py`）：页面预热 200 且同步到 build、`checkout_pricing_config` 200、账单国/币种随出口正确（IN/INR、SG/SGD）、稳定设备指纹、attestation（页面未暴露），全部仍为 400 `unusual activity`。
- 出口 IP 采样（`probe_ip_suffix.py`）：同一会话内 3 次请求 IP 一致；不同 `-sid-` 后缀会换到不同 IP。
- 结论：代码侧请求已与浏览器一致；剩余 400 是上游支付风控按 IP/账号做出的决定，不是请求契约或解析缺陷。

### Notes

- `progress.md`：追加本轮部署与真实探测记录。
- Oracle 当前线上镜像：`turb-gpt-free-register:fb8b85c`；回滚方式：把 `/home/ubuntu/turb-gpt-free-register-compose/compose.env` 里的 `TURB_IMAGE` 改回 `turb-gpt-free-register:a731abd`，再 `docker compose up -d --no-deps web`，备份文件为 `compose.env.before-fb8b85c-20261019T142826Z`。

## 2026-10-19 - Task: 批量刷新套餐并验证支付检测（含 #737 全链路成功）

### What was done

- 关闭支付检测批量刷新套餐：最近 40 个账号 + 历史 14 个曾带试用账号，共 54 个账号重新查询；其中 40 个账号 `accounts/check` 返回 200。
- 只有两个账号当前仍满足“一个月免费”闸门：#737 与 #918（100% 折扣、1 期、month、有活动与 offer）。#849 是 50% 折扣、3 期，闸门按预期跳过。
- #737 走标准套餐队列跑完整链路：建单 200 → Stripe init 200 → 落库 `checked`，支付方式 `card / link / upi`，金额 0，币种 INR。
- #918 同一链路仍为 400 `provider_activity_restricted`，确认是该账号自身被支付风控标记，不是代码问题。

### Testing

- 批量套餐刷新（`.tmp/payment-flow-transaction/stage_a_plan_refresh.py`）：40 个最近账号，59 个 free、17 个 401/超时；仅 918 带活动。
- 历史 14 个账号（`stage_a_ids.py`）：737、849、918 返回 200，其余 11 个 401。
- 全链路探测（`stage_b_checkout.py`）：737 `plan 200 → sentinel 200 → checkout 200 → stripe_init 200`，结果 `checked`；849 `plan 200` 后直接 `not_applicable`，无建单请求；918 仍 `provider_activity_restricted`。
- 持久化校验（`stage_c_persist.py`）：737 落库字段 `payment_method_check_status=checked`、`payment_method_types=["card","link","upi"]`、`payment_currency=INR`、`payment_amount=0`、`payment_session_type=subscription`、`payment_method_checked_at=2026-10-19T22:54:15`。

### Notes

- 本轮只新增探测脚本与日志，未再改业务代码；线上镜像仍是 `turb-gpt-free-register:fb8b85c`。
- 结论：支付方式检测在真实环境可用；结果取决于账号是否被上游支付风控标记。#737 为成功样本，#918 为风控样本，#849 为闸门样本。
