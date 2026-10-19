## 当前发布状态：cd5517b

- 已提交并推送 `cd5517b`，Oracle 运行 `turb-gpt-free-register:cd5517b`。
- 已确认两个缺陷：opaque offer ID 被写入 `plan_name`；checkout 元数据未继续请求 OAICS state / Stripe init。
- `plan_name` 固定为 `chatgptplusplan`；仅 Plus campaign 选择 promotion。
- 本地相关测试 71 项通过。基线、修改、回滚、补丁重建已记录。
- Oracle 容器 healthy，重启 0，容器内登录页 HTTP 200，源码哈希一致。
- 部署后使用模拟响应验证实际 /app 代码，三个场景均 checked。
- **真实端到端仍未通过**：候选版单账号 #918 首次 plan 200、checkout 400；修正产品字段后两次复测均在前置 accounts/check 返回 403，未到 checkout。
- 未覆盖数据库里的旧支付结果；未声称线上返回了真实支付方式。

## 下一个未完成验证

上游套餐查询恢复后，通过标准套餐队列对 #918 执行一次检查、写入新结果。只输出阶段、状态码和允许保存的支付字段。

```sh
ssh -o BatchMode=yes oracle 'sudo -n docker exec -i turb-gpt-free-register python - /app/core/chatgpt_plan.py --persist' < .tmp/payment-flow-transaction/live_probe.py
```

若仍为 403，记录阻塞并停止，不能把模拟通过当作真实结果。

## 四个固定交付角色

- MODIFIED_FILE: `/Users/jiangdk/code/personal/turb-gpt-free-register/.tmp/payment-flow-transaction/modified/core/chatgpt_plan.py`
- DIFF_FILE: `/Users/jiangdk/code/personal/turb-gpt-free-register/.tmp/payment-flow-transaction/DIFF_FILE.patch`
- VERIFICATION: `/Users/jiangdk/code/personal/turb-gpt-free-register/.tmp/payment-flow-transaction/VERIFICATION.txt`
- ROLLBACK: `/Users/jiangdk/code/personal/turb-gpt-free-register/.tmp/payment-flow-transaction/ROLLBACK.sh`

部署环境备份：`/home/ubuntu/turb-gpt-free-register-compose/compose.env.before-cd5517b-20261019T115605Z`。
