# 可选 Jev 上下文排序

Jev 接在原生 Wiki hook 的规则召回之后，对已授权项目的候选段落排序、过滤。
默认关闭，现有检索、来源引用、历史限定和上下文长度限制保持有效。
当前固定调用 TypeSafe `jev-1.13.0`；不替换 Wiki 编译模型、手动 MCP
`get_project_context`、主模型摘要或 subagent 调度。

## 另一台机器同步

1. 按 [README.md](README.md) 从仓库配置的维护分支更新、构建 runtime，
   再由已有安装流程部署。保留本机角色、machineId、队列和配置，先 dry-run。
   不复制另一台机器的运行目录、插件清单、配置、密钥或 `stateDir`。
2. 检查本机状态：

   ```sh
   python3 deployment/jev-control.py status
   ```

   默认读取 `~/.config/llmwiki/knowledge-flow.json`；可用 `--config /absolute/config.json`
   指定其他安装。新设备没有 `jevContext` 设置时仍使用原规则。
3. 在本机 macOS“钥匙串访问”中添加通用密码，服务名 `typesafe.ai`，账户名使用
   本机自选的标识，密码为对应 TypeSafe API key。不要把 key 放到命令参数、
   shell 历史或 JSON 配置里。已有安全运行环境也可以提供 `TYPESAFE_API_KEY`；
   交互终端的环境变量不会自动传入桌面或后台进程。
4. 核实账户余额、到期时间，并分配**本机**额度后显式启用。例如以下变量必须由本机
   的真实分配值填写，不能照抄另一台机器的金额：

   ```sh
   python3 deployment/jev-control.py on \
     --budget-usd "$JEV_MACHINE_ALLOWANCE_USD" \
     --expires-at "$JEV_GRANT_EXPIRES_AT"
   ```

   到期时间是带时区的 ISO 时间，例如 `2026-10-19T00:00:00Z`，但该示例不代表
   当前账户到期时间。单机额度必须大于 0、最多 $5。再次启用可省略这两个参数。

每台机器的本地账本相互独立，不是共享账户的全局预算。同一笔账户额度不要在多台机器
重复分配。部署前先核对账户额度并决定各机预算；不要复制或清空 SQLite 账本来恢复额度。

## 开关与状态

```sh
python3 deployment/jev-control.py status
python3 deployment/jev-control.py off
python3 deployment/jev-control.py on
```

命令只改本机 `jevContext`，不替换 worker、项目规则或机器角色，也不重置已用额度。
关闭后，同一 worker 返回原规则结果；下一次 hook 即生效，无需重启服务。
不含 Jev 的旧 worker 会被拒绝启用，先完成正常 runtime 升级。

如果本机曾使用独立覆盖层，升级正式 runtime 后改用这里的仓库命令，不复制旧覆盖层，
也不通过旧命令回滚新版。已有 `jevContext` 会被常规安装器作为本机设置保留。

## 排序与回退

每个原始候选得到 direct/supporting/irrelevant 概率，按 `2 * direct + supporting`
排序，仅移除 irrelevant >= 0.9 的段落。程序保留原证据对象，不生成替代文本。
过滤掉段落后标记结果未完整展开，并保留原始页面的补查入口。

| 条件 | 处理 |
| --- | --- |
| 关闭、少于两个候选、输入或时间预算不足 | 原规则 |
| 固定模型返回完整有效评分 | Jev 排序与过滤 |
| HTTP 错误、超时、格式或模型不符 | 完整原候选回退；瞬时错误冷却 60 秒 |
| HTTP 402、鉴权失败、本地额度不足 | 停止后续付费调用，持续使用原规则 |
| 已过期 | 原规则，不再调用 API |

Jev 最多占用 1.8 秒；规则检索耗时较长时进一步缩短。原 Python hook 的 worker 截止
时间仍为 4 秒。底层检索或宿主独立失败仍走原有降级提示，不能承诺在所有故障下送达。

## 预算、数据和验证

`${stateDir}/jev-trial.sqlite` 用原子事务记录调用预留和 usage。进程中断或账单未知时
保留保守预留，开关切换不退还该笔额度。每次启用前都要核对服务当前价格和账户状态；
本地账本与预算都不能代替供应商账单。
状态输出是**本地记账，不是账户余额**。额度用于真实调用，没有自动耗费额度的循环。

请求仅含问题和候选语义文本，处理可识别的凭据、链接、路径、邮件和 ID 模式；不发送
来源原文窗口或 page ID。模式脱敏不是完整匿名化保证，语义文本仍可能包含业务名称。
API key 在调用时从钥匙串或环境读取，不写入仓库、配置或日志。

每个会话的 `context-diagnostics` 包含 `diagnostics.contextRanking.mode/reason`。
这是准备好的上下文状态，不是宿主送达、答案采用或准确率提升的证明。
同步后应在那台设备的真实新任务里验证，不以其他机器的历史测试替代。

本地检查：

```sh
npx vitest run test/knowledge-flow-jev.test.ts
python3 -m unittest discover -s deployment -p test_jev_control.py
node extensions/knowledge-flow/build.mjs /absolute/staging/knowledge-flow
```

构建必须保留 `node:` 前缀：`node:sqlite` 不存在无前缀的内置别名。
原生集成还应在实际 Node 24 runtime 上对比 disabled/expired 与规则结果，检查启用
时的 contextRanking，以及 Python hook 的四秒截止时间。
