# 可插拔 Agent 接入

## 用法

```sh
python3 deployment/install_agents.py install --host claude --profile ~/.claude --dry-run
python3 deployment/install_agents.py install --host claude --profile ~/.claude
python3 deployment/install_agents.py install --host claude --profile ~/.claude/profiles/claudex
python3 deployment/install_agents.py install --host pi --dry-run
python3 deployment/install_agents.py install --host pi
python3 deployment/install_agents.py status
python3 deployment/install_agents.py disable --host claude --profile ~/.claude
python3 deployment/install_agents.py disable --host pi
```

默认主配置是 `~/.config/llmwiki/knowledge-flow.json`。安装器将精确宿主注册写入该文件的 `agentPlugins` 字段；策略、路由、Wiki 与运行时仍来自同一份配置。宿主配置只登记稳定 launcher 或稳定扩展路径。每次事件都会重新读取 `worker` 路径，因此换运行时不必改写各宿主注册。安装器也会把已存在的共享 `llmwiki-local serve` 注册到 Claude 当前 profile 的 MCP 文件及 Pi 的 `mcp.json`；如果同名 server 已由其他配置占用，安装失败并保留原内容。

`--home` 与 `--config` 可用于 dry-run 和隔离文件测试。Claude hook 把同一主配置路径作为入口参数，因此支持显式 `--config`。Pi 扩展通过宿主进程环境读取 `LLMWIKI_CONFIG`；为避免安装成功但 Pi 实际读另一份配置，Pi 的正式安装只接受该 profile/home 默认主配置。隔离 Pi 会话可在临时宿主进程中设置 `LLMWIKI_CONFIG`、`LLMWIKI_AGENT_LAUNCHER` 与 `PI_CODING_AGENT_DIR` 指向临时配置、launcher 和 profile。Claude profile 与 `--pi-settings` 可显式指定。

status 分别报告 adapter `enabled/configured`、共享 MCP 是否已登记，以及运行时投递；后者一律明确标记为 `unverified`，直到宿主内真实会话验收。MCP 注册复用同一个 launcher，不复制 Wiki 策略配置。

## Given / When / Then 验收

- **共享配置**：Given Claude 与 Pi 都注册到同一 `knowledge-flow.json`，When 各自提交一条匹配项目的提示，Then 两边都从该文件当前的 `worker` 读取并走同一 `hooks.handle` 路由和上下文逻辑。
- **运行时指针更新**：Given 主配置的 `worker` 从版本 A 改为版本 B，When 下一次宿主事件启动新 bridge 进程，Then 它调用 B，无需修改 Claude 或 Pi 配置。
- **Claude 去重与隔离**：Given 默认 profile 和 claudex profile 各自可能有旧版 llmwiki hook，When 注册其中一个 profile，Then 只替换同一 config + profile 的旧 llmwiki Prompt/Stop 项；用户其他 hooks 及另一 profile 保持不变。
- **Pi 当前分支取证**：Given Pi 活动分支中含本回合用户消息、最终助手文字、工具返回、隐藏思考、旧分支和 Wiki 注入消息，When 一次成功 run 到达 `agent_settled`，Then 只提交本回合可见 user/assistant 文本，并以原生 entry locator 与内容 hash 标识；工具、思考、旧分支和注入消息不进入证据。
- **Pi 首轮和重复提示**：Given `before_agent_start` 触发时活动分支为空，或已有完全相同的旧提示，When 当前提示随后写入活动分支并完成，Then 用 hook 前的 branch leaf/entry ID 快照只绑定新消息；空基线可成功采集，旧提示不能冒充当前轮。
- **失败关闭**：Given transcript/branch 不完整、身份或工作目录不符、没有完整最终答复、Claude 最终 stop reason 不是 `end_turn`、用户块含图像、Pi run 中止或报错，When Stop/settlement 到达，Then 不提交候选。
- **Claude 失败记录**：Given 上述任一 Claude 证据校验失败，When 该回合在提示阶段未路由到项目，Then 与共享 Stop 入口一致记为 filtered，不产生采集异常；When 该回合已路由到项目，Then 记录 `ClaudeEvidenceUnavailable` 及固定原因代码（如 `foreign-workspace`），不含对话内容。
- **幂等和关闭**：Given Stop 重复触发，When bridge 再次处理相同原生 turn，Then 共用队列最多保留一个任务。Given 关闭 Pi 或一个 Claude profile，When 配置变更完成，Then 只移除同 config 的精确 owned registration，其他宿主、profile 与用户配置保留。
- **dry-run 与备份**：Given 任意宿主配置，When 使用 `--dry-run`，Then 仅输出将变更的路径且文件、目录和链接均不变化。When 正常修改既有文件，Then 先建立权限为私有的备份，再原子替换；发生失败时恢复已触及的注册文件。
- **包完整性**：Given 一个已安装 immutable bundle，When 升级 current 指针，Then 先校验目标和旧 current 的 manifest；若文件被改动或路径不在 bundle 根目录，操作失败并保留现场。
