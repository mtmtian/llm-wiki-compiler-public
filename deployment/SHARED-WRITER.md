# 自动共享写入与一次性接管

## 用户结果与边界

共享 Obsidian 页面只有一个已配置的 materializer。以下流程用虚构身份 `peer-a` 和
`peer-b` 举例；每个部署的实际身份、角色和路径都来自仓库外的私有配置。默认机自动刷新不依赖
对话。用户明确要求另一台更新 Wiki 时，可请求一次性写入，完成后自动交回默认机。

接管使用持久化让出回执，不使用心跳超时抢占。原写入机在本机写锁内结束已有事务并持久记录让出，
才发布带文件 hash 和知识输入清单的回执。接收方必须收到完整回执、来源和页面后才能写入。
机器离线、回执延迟或 worker 空闲都不能被当作已让出。采集、审核和独立 publication 不受接管等待影响。
所有参与机都需先升级；旧 runtime 不认识协调协议，不能混用。

## 行为验收场景

- Given 两台已启用协调模式但尚未交接旧所有权，When 后台同步，Then 两台均不抢写共享页面。
- Given 原写入机已完成本机事务且两机版本就绪，When 显式初始化并指定 `peer-a` 默认写入，Then `peer-a` 接收校验清单后自动刷新，原机器保持不写。
- Given 非默认机发出一次性请求，When 默认机尚未收到请求或尚未发回让出回执，Then 请求机只等待。
- Given 默认机已经让出，When iCloud 尚未传输回执或机器重启，Then 默认机不会因旧共享快照、超时或没有对话而恢复写入。
- Given 请求机收到完整回执和输入，When 正常 worker 唤醒，Then 完成一次更新、交回文件清单，默认机承接后继续自动刷新。
- Given 回执比页面或 publication 先到，When 接收方检查，Then 保留已有页面并等待完整数据，不用旧知识覆盖新内容。
- Given 人工修改、回执分叉、回放旧授权或输入漂移，When 尝试接管，Then 拒绝写入并报告差异。
- Given 在让出回执发布前、页面更新中或交回时中断，When 重试，Then 从私有恢复状态继续，不重新授予同一个请求。

接管协议与恢复日志是操作状态，不进入 Wiki 历史页或默认知识检索。

## 命令和实际状态

授权是用户明确提出本次更新后，由 agent 调用下列显式入口；普通采集 hook 不产生接管请求。
请求 ID 使用一个稳定的新标识（例如本次操作的 UUID），重试沿用同一 ID。已完成的 ID 不会重新执行。

```sh
# 只读查看默认机、当前持有者和待处理请求；不申请权限、不刷新页面。
llmwiki-maintain --shared-writer-status

# 用户明确授权本次更新后，在请求机执行。默认机收到回执前，请求机不会写。
llmwiki-maintain --request-shared-write <request-id>
```

如果调用者就是默认写入机，入口执行一次正常同步；否则先返回 `requested` 与
`permissionGranted: false`。两台后台 worker 监听 requests/releases 目录，并保留每 5 分钟的检查。
默认机的下一次同步结束已有页面事务后发回 `handed-off`；借用机收到回执后自动执行一次同步，
返回 `returned-to-default`，默认机收到返回清单后继续自动刷新。无需人工再次批准已经发出的同一请求。

`not-materializer` 表示本机目前不持有共享页面写入权，本机 publication 和私有知识同步继续。
`handoffWaiting` 表示另一台版本/配置尚未就绪：默认机继续自动更新，仅暂不授予接管。
不存在超时自动接管或超时自动回收；机器休眠、关闭对话窗口或 worker 显示 `not running` 都不是让出回执。

## 切换默认 materializer

1. 保留两机路径、预算、项目映射与采集/发布角色。按各机已有部署方式构建同一个已合并完整 commit，
   核验 manifest；安装 dry-run，暂停旧 worker 并确认旧进程结束，再分别安装。双方的额外参数相同：

   ```sh
   new_materializer='peer-a'
   old_materializer='peer-b'
   python3 deployment/install.py --runtime <本机已核验runtime> --writer --event-driven \
     --materializer-machine-id "$new_materializer" \
     --shared-writer-bootstrap-machine "$old_materializer" --dry-run
   # 检查结果后去掉 --dry-run 实际安装；保留各机原有必要参数。
   ```

2. 在各机 Codex 原生 hooks 界面审阅并信任新命令，执行 `llmwiki-maintain --check --announce`。
   新模式在没有 bootstrap 时报告 `awaiting-handoff-bootstrap`，不会根据新的默认身份抢写。
   每台只申报自己的实际 runtime 与协调配置；双方可以恢复新版 worker，采集和私有同步继续。
3. 原写入机 `peer-b` 等到所有参与机实际申报同一完整 runtime commit 与相同协调配置后执行：

   ```sh
   llmwiki-maintain --bootstrap-shared-writer
   llmwiki-maintain --bootstrap-shared-writer --apply --announce
   ```

   初次 dry-run 核验其已有 ownership manifest 与共享页面。实际激活在原写入机本地写锁内恢复/完成
   原事务、生成最终清单、持久记录让出，才发布 bootstrap；新 materializer 不能代替原机器作此声明。
   dry-run 不写共享记录或页面；正常本机副本检查可以刷新私有缓存和同步诊断。
4. `peer-a` 收到完整 bootstrap、来源、页面后，正常后台同步接收所有权并刷新。双方核对完整引用与
   默认上下文，重复同步不重复写入。测试一次真实授权接管和归还，确认新 materializer 恢复自动更新。

既有模板保持兼容模式，避免旧部署在常规升级时被无意切换。显式启用后的 `sharedWriter` 和
`materializerMachineId` 会在后续安装中保留，不被模板重置。配置中的 `bootstrapMachineId` 记录初始
旧所有者，后续一次性请求无需再次初始化。

## 故障与恢复

- `.knowledge-exchange/v2/shared-writer/` 保存不可变 bootstrap、每机请求与释放链；
  私有 `stateDir/shared-writer-state.json` 保存已看见的最新回执和待重发的本机让出回执。
  共享 hash 检测完整性，不是签名；沿用已受信任参与机的文件夹权限边界。
- 让出方先持久停止写入再发回执，重启仍保持让出；接收方只承认完整、无分叉的连续回执链。
  回执先到而内容尚未下载时，保留页面并报告 checkpoint 不匹配，文件到齐后正常同步重试。
- 页面 hash 不符也可能是真实人工修改，不能强行覆盖或把它当成单纯同步延迟。核对差异后再决定如何保留。
- 部分页面更新使用原有 pending 事务恢复；成功前不交回权限。发布回执时中断，从私有 outbox 重发同一记录。
- 收不到对方回执时保留当前权限状态。不能通过删除私有状态、清空历史、改 machineId 或恢复旧版本来解除等待。
  原持有机失联且无法恢复时，需要另外审核实际页面和机器停写事实的恢复迁移；本入口不提供强制抢占。
- 激活后不得让不支持本协议的旧 runtime 恢复运行。新版本会拒绝失去协调配置的回退路径，
  但不能控制另一台被手动重新启动的旧程序。升级窗口中的停旧、实际版本申报和两机验收仍不可省略。

验收需区分：代码测试通过、已构建、已安装、hooks 已信任、旧所有者已让出、新 materializer 已实际写入，以及真实双机
接管/归还是否完成。隔离目录测试不证明另一台实体设备已收到 iCloud 文件。
