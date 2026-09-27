维护本机项目 Wiki 自动知识流程。事件 worker 已由 `--event-driven` 显式准备并由专门命令启用后，唤醒时先运行 `~/.local/bin/llmwiki-wake`（默认执行一次完整 reconcile、drain 和 report）；手动诊断仍可运行 `~/.local/bin/llmwiki-maintain --drain --check --announce`。wake 会先做纯 Python reconcile；没有新增队列、v2 publication、baseline 或 v1 migration 信号时直接报告 idle，不调用模型。v2 writer/publisher 各自发布独立不可变记录，contributor 只提交提案；reader 的 event worker 只同步 replica。检查 exchange.peers、publication 持久化和 replica generation，区分记录已写入与本机检索已生效，不把 iCloud 本机文件存在当作双机同步完成。

只处理该流程既有队列和事件记录，不重新扫描全部历史会话，不把维护会话采集到 Wiki。正常候选由已配置的 Luna 提炼和独立审核自动处理；不要绕过冲突门禁、扩大项目范围或自行开启写入。检查配置 `enabled`、`intakeEnabled` 和 `eventDriven.enabled`；reader 的 v2 worker 可以做无模型同步，暂停配置不会排队、drain 或调用模型。

启用 `exchange.sharedWriter` 后，用 `llmwiki-maintain --shared-writer-status` 查看默认机和当前持有者。
只有用户明确授权本次共享页面更新时，才调用 `llmwiki-maintain --request-shared-write <本次稳定请求ID>`；
普通后台维护不创建接管请求。`requested` 不代表已获写入权，等待原持有机的持久让出回执后由正常
worker 更新并归还。无对话、休眠、超时或 `not running` 都不是停写回执。默认 materializer 自动更新、
另一台自动采集和发布仍然继续；升级与恢复按部署文档 SHARED-WRITER.md 执行。

检查失败时依据 last-error.json 和 failed 目录诊断可恢复的运行问题，避免无证据重试或重复发布。对 review 目录中尚待决定的内容，汇总明确冲突、缺失的关键证据及需要用户判断的问题，附现有记录和原始证据引用；无价值或重复内容不需要用户审核。

首次接入 v2 或 baseline 尚不存在时，先运行一次（安装器和 wake 不会自动创建 baseline）：

`~/.local/bin/llmwiki-maintain --initialize-baseline --announce`

输出紧凑的处理数量、检查结果和必要决策项，并核对 `exchange.count`、`exchange.conflicts`、
`exchange.errors` 与当前 replica generation。由显式维护会话（而非 wake 本身）在每周一额外
抽查最近发布的最多五条记录，核对原文支持、日期、项目归属和重复性；区分自动新发布与已有
历史导入。发现质量问题时给出有证据的修正建议，不自动改写全局规则或合并上游代码。质量
抽查记录写在本机 state/quality-audits/，不写入知识正文。
