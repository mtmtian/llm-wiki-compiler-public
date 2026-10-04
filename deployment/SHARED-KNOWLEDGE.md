# 两台机器共同积累项目知识

## 运行约定

新增的[共享写入交接模式](SHARED-WRITER.md)允许显式请求临时接管一次、完成后自动交回。
启用和默认角色变更必须经过原写入机的释放回执。下表使用虚构身份 `peer-a` 与 `peer-b`，
描述未启用交接协议时的兼容部署；它不是任何主机的当前配置，不能直接修改实际身份来抢写。

两机读取同一 iCloud Wiki，并分别提炼、审核本机项目任务。v2 将发布持久化与本机
可检索状态分开：每台机器写入自己的不可变记录，本机 replica、索引和编译态本地重建。
两机可以同时发布各自记录，不共享 `.llmwiki/state.json`。共享 Obsidian 展示层另由一个
明确的 materializer 维护，不改变两机独立发布和私有检索边界：

| 设备标识 | 角色 | 本机采集 | 共享记录 | 共享 Obsidian 页面 | 本机可检索 |
|---|---|---|---|---|---|
| `peer-b` | writer/publisher + materializer（示例） | 开启 | 写入自身目录 | 负责生成 | 重建本机 replica |
| `peer-a` | writer/publisher | 开启 | 写入自身目录 | 不写入 | 重建本机 replica |

任一 writer/publisher 离线时，另一台 writer/publisher 的 publication 仍可写入，待 iCloud
同步后各自重建本机 replica；
materializer 离线时只会延迟共享 Obsidian 展示，两机发布和私有检索继续。iCloud 不能提供
跨机器锁，唯一写入身份由私有配置中的 `exchange.materializerMachineId` 约束；示例将
`peer-b` 设为 materializer。`--reader` 为 `false/false`，
`--contributor` 为 `true/false`，`--writer` 和 `--publisher` 在 v2 都是
`true/true`。v1 仍按 `publisherMachineId` 限制唯一发布端，升级前不能将旧 runtime
与 v2 状态混用；v2 的 `--publisher` 只是明确启用本机发布，不指定唯一机器。

materializer 是共享派生页面的唯一写入角色，不等于 publisher。指定之外的 writer/publisher、
reader 和 contributor 均不得写共享页面；缺少 `materializerMachineId` 时关闭 shared projection。
每台 writer/publisher 仍可独立写自己的 publication 并重建私有 replica；contributor 只提交
提案，reader 只重建私有 replica。

## 共享与私有边界

共享目录为 Wiki 根目录下 `.knowledge-exchange/`（v2 记录位于其 `v2/` 子目录）：

- `v2/publications/<machineId>/<sha256>.json`：任一参与机独立发布的不可变记录。只含已审 claim、
  支持它的精确引文、原始脱敏证据哈希、出处及日期，不包含整段会话。
- `v2/baseline.json`：共享 Wiki 的不可变基线快照；各机据此重建本机 replica。
- v1 迁移期间仍可见 `submissions/<machineId>/` 与 `receipts/`，v2 writer 不再写入；过渡期 contributor 仍可提交 v1 提案。
- `machines/<machineId>.json`：各机维护命令主动报告的版本与角色状态。

配置、模型登录、完整 evidence、队列、审核记录、审计和回滚历史仍在本机。
业务 Wiki 正文和提案不进入 Git。共享文件夹的写权限本身是信任边界；哈希用于
检测完整性，不是防恶意参与机的数字签名。不得把未知机器加入 participants。

旧格式提案只由 `exchange.legacyImporterMachineId` 指定的一台 v2 writer 导入；该字段只应存在于
外置私有配置。未设置则不自动导入旧提案，v2 contributor 必须显式声明该配置。这样即使两机尚未
看到对方的 publication，也不会各自处理同一旧提案。这个迁移角色不限制各机发布原生 v2
记录。更换迁移机前，先停止旧迁移机、处理或移交待处理批次、同步全部机器配置，再启用
新迁移机；不支持旧配置仍运行时直接改名抢占。升级前的 runtime 不认识这个门禁，必须
停止其他旧导入进程，不能把单机配置当成 iCloud 分布式锁。

v2 配置中的 `sharedWikiRoot` 是原始 iCloud 根，`wikiRoot` 是本机
`<stateDir>/replica/current` 视图；`machine.json.wikiRoot` 仍填写共享根。首次
bootstrap 只把共享 `sources/` 与 `wiki/` 冻结为不可变的 `v2/baseline.json`，之后每台机器
都由 baseline 加 publication 生成自己的本机 view、索引和编译态。只有
`exchange.materializerMachineId` 指定的那一台机器，才会把该 view 的受管派生页面和导航
投影到共享 Obsidian 根；这不改变其他机器的私有 replica。安装器不复制 Wiki、创建 replica
链接或初始化 baseline，主会话运行时负责这些动作。持久化发布记录、共享投影和本机可检索
状态是三个独立状态，任一状态尚未同步都不能互相推断。

materializer 的 ownership manifest 位于本机 `stateDir/shared-materialization.json`，记录
每个受管共享文件的 hash；中断时保留 `stateDir/shared-materialization-pending.json` 供下次同步恢复。
替换或撤回文件会在目标同目录留下 `.llmwiki-preserved-*.bak`，这些隐藏备份不是 Markdown，
不会被采集，也不会自动删除。人工编辑或 hash 不符时 fail closed，不覆盖人工内容。
冲突不会回滚或冻结本机 `replica/current`，但会写入
`stateDir/replica-errors/shared-materialization.json`；`--check` 将该状态判为失败。每次
同步都会重试共享投影，包括本机 publication digest 没有变化的同步。

每次成功切换本机视图后，只保留当前版、上一版及仍被审核/重试批次引用的版本。
批次的知识版本引用与同步共用本机锁，引用落盘后才允许其他同步清理旧版；原始证据、
共享 baseline 和 publication 不参与清理。损坏的批次审计会暂停清理并报告异常，删除失败
保留已经生效的当前视图，恢复成功后清除该异常。

提交和发布都只产生不可变记录；本机可检索状态由本机 runtime 根据 baseline 与
publication 重建，不能使用对方绝对路径。同一完整 publication payload 的 `recordId`
只用于同 packet 重试幂等；即使 claim 正文相同，只要来源或适用条件不同也应保留为
不同记录，交由审核模型分别判断。冲突、替换和不确定内容保留在本机待审区，不会因
排队时间较长而自动通过。

正式页面的原证据 SHA-256 用于之后与原机证据核对；另一机只有摘录而没有
原文时，不能声称已经独立验证全文。assistant 分析只可支持带日期的 historical lesson；
完成声明、决定、已验证指标和实施事实不能从助手自述提升为知识。

### 按主题沉淀与旧页面迁移

shared v2 的页面按「项目 + canonical topic + decisionObject」组织。抽取先查找现有页，
匹配时填写 `targetPageId`，不存在时才创建主题页；独立 reviewer 同时核查归属、主题、
决策对象和证据。同一 publication 可以给一个主题页贡献多个段落，也可以涉及多个主题页。
每个 publication 只生成一份包含多个精确 quote 的 source；段落仍保留日期、状态、适用条件、
理由与逐行 citation。`knowledgeTopic`、`knowledgeDecisionObject`、`knowledgePublicationRefs`
记录匹配键与段落来源。历史分析与用户决定在同页分别标记，不因合并而提升证据权威。

物化过程不调用模型：先沿用已审目标，再使用元数据精确匹配。缺失、跨项目、不同对象或
多个可能目标均进入 conflict；独立机器未相互看到的同对象变体继续隔离。旧 publication
没有 `decisionObject` 时仍可重放，保留原 topic 粒度；旧跨主题 `targetPageId` 只是相关页
提示，不授权直接合并。旧 `record-*` 目标可解析到新主题页，但不会根据关键词自动推断
遗留碎片的决策对象和归并关系。主题页超过 12,000 字符时
停止该次 generation，保留上一版可用视图，需先审阅内容再处理。

baseline 概念页允许在保留原正文的基础上补充。首次共享更新必须匹配 baseline hash，
以后匹配 ownership hash；撤回贡献恢复 baseline 原文，baseline sources 不可被替换。
人工修改仍阻断整个共享写入批次。两台机器需使用一致的新 runtime 才会生成一致主题视图。
旧 vault 中未被 manifest 认领的页、手工入口和历史链接要先做明确迁移清单；升级、重建私有
replica 和迁移共享旧页是三个不同动作，不能直接删除旧 `record-*` / `knowledge-flow-*` 文件。

已人工审阅的历史归组可保存在 exchange 的 `v2/topic-routes.json`：外层为
`{version: 1, baselineId, reviewedAt, groups}`，每组包含 `projectId`、`topic`、
`decisionObject`、`claimRefs`（完整 publication id 加 `:索引`）。此业务文件不进入 Git。
只有旧版未填写 `decisionObject` 的记录可被重新归组；正文、证据、时间与原始 packet 不改。
同组中的精确记录表示已共同复核，可共存；未来未相互观测的新变体仍被隔离。
每次 sync 只读取一个清单快照，同时用于缓存身份和 worker 输入。错误基线、未知记录、
重复引用、跨项目、非法格式或 symlink 都会阻止切换，保留上一版视图。上限为 256 KiB、
1,000 组、5,000 个引用。迁移后应保留该文件，删除它会恢复旧路由。

旧共享文件迁移采用私有 JSON `{baselineId, files: {相对路径: sha256}}` 清单：

```sh
python3 deployment/migrate-shared.py --config /absolute/private-config.json --plan /absolute/legacy-plan.json
# 确认唯一 materializer、旧 worker 已停、新私有视图验收通过后：
python3 deployment/migrate-shared.py --config /absolute/private-config.json --plan /absolute/legacy-plan.json --apply
```

默认仅预检，不写 state 或共享文件。清单须覆盖全部未认领旧生成页面、source 和被替换的
导航，任一 hash 变化就拒绝执行。apply 复用正常共享投影的 pending 事务，把旧文件保留为
同目录隐藏 `.llmwiki-preserved-*.bak`，新主题进入正常导航；中断后由正常 sync 恢复。
已有 ownership 后不重复认领。手工运维索引和外部保存的旧路径不会自动改写，需按明确映射
处理；不要宣称备份文件等于所有旧链接仍可直接打开。

## 多主机升级

参与主机应从同一已审查提交构建 runtime，并分别保留自己的私有配置、machine ID、角色、队列和
登录。项目源码或通用部署策略通过仓库配置的 PR 目标分支集成；主机私有配置、Wiki 内容、队列
和凭据不进 PR。不要把未提交工作、`machine.json`、state、队列或凭据覆盖到另一台机器。

已有部署重新安装时，继续使用原仓库外完整配置源，显式保留原 machine ID、角色参数及
`--event-driven` 选择。例如以下值仅为占位示例，按每台主机已登记的身份填写：

```sh
private_flow_config="$HOME/.config/llmwiki/private-knowledge-flow.json"
machine_id='peer-b'
cd /path/to/llm-wiki-compiler
git status --short
git rev-parse HEAD
python3 deployment/build-runtime.py
runtime="$HOME/.local/share/llm-wiki-compiler/releases/$(git rev-parse --short=12 HEAD)"
python3 deployment/install.py --runtime "$runtime" --config-source "$private_flow_config" \
  --machine-id "$machine_id" --writer --event-driven --dry-run
```

核对 dry-run 后再按原参数执行安装。若原角色是 publisher、contributor 或 reader，应继续使用该
原角色；只有明确进行 materializer 迁移时才传入协调参数。升级前停止旧维护任务/采集并等待
现有 worker 结束，安装并恢复后不要再运行不兼容的旧 runtime。切换 materializer 时按迁移章节
停用旧共享生成进程、等待 iCloud 同步，再在参与机统一配置新身份。

v2 以冻结 baseline 隔离旧状态，不要求所有机器同时升级；尚未升级的 v1 机器仍只读取 v1 记录，
看不到 v2 新记录。全部机器升级后才会彼此互见并独立发布。构建脚本要求干净提交，并记录源码
提交和产物哈希；已有本地分支或未提交工作时先核对，不强制覆盖。

各机将稳定 machineId 保存在 `~/.config/llmwiki/machine.json`，或每次安装明确
传参。不要复制对方 projectPaths、stateDir、队列或整份 Codex 配置。
重新在 Codex 原生 `/hooks` 信任路径改变后的定义，再用新会话验收实际事件；
安装成功不代表宿主已信任。现有 MCP 进程需刷新才能使用新启动入口。

部署顺序固定为：prepare（上面的 `--writer`/`--publisher --event-driven` dry-run 与安装）、
在 Codex 原生 `/hooks` 信任新建或变化后的 hook、在已有 baseline 的前提下执行一次维护
检查并构建本机 replica，然后才 enable LaunchAgent。只有**全新的共享根且不存在
`.knowledge-exchange/v2/baseline.json`**时，才在一台机器冻结 baseline：

```sh
~/.local/bin/llmwiki-maintain --initialize-baseline --announce
```

已有共享 baseline 不重复初始化；安装器和 event worker 都不会创建或覆盖 baseline。两台机器
随后分别执行：

```sh
~/.local/bin/llmwiki-maintain --check --announce
python3 deployment/install.py --event-worker-action enable
python3 deployment/install.py --event-worker-action status
```

`--check --announce` 从 baseline 加 publication 构建本机 replica；materializer 还会尝试
更新共享 Obsidian projection。非 materializer、reader 和 contributor 只完成本机同步，不写共享
页面。共享页面出现人工编辑、ownership hash 不匹配或遗留未认领生成页时，当前本机 replica
仍保持可用，但状态会写入 `stateDir/replica-errors/shared-materialization.json`，而
`--check` 返回失败；修复后再次同步会重试，即使 publication digest 没有变化。启用后用
`launchctl print gui/$(id -u)/com.llmwiki.knowledge-flow.wake` 核对最近的 `last exit code = 0`，
再检查 `stateDir/reports/wake.log`：空队列应为 `reason=empty`、`attempts=0`，等待事件时
`state=not running` 属正常状态。用一个有真实可复用结论的业务回合验证 Stop 入队，合批后
核对审核、publication 与本机 replica；空队列通过不能代替模型链路验收。若出现原生隐私
提示，按实际提示授权并重新验证。前台命令的成功不能代替真实 launchd 后台运行。

需要手动补漏或质量抽查时使用：

```sh
~/.local/bin/llmwiki-maintain --drain --check --announce
```

每台机器维护本机待提交和已同步记录，处理最多三项。首次 baseline 成功后，普通
维护会同步 baseline 与 publication 并重建本机 replica；只有指定 materializer 会在该同步中
更新共享 Obsidian 页面。共享模式 Stop 只入队，不在
用户回合内等待模型。正常运行由事件 worker 按 WatchPaths 唤醒，原有每小时维护任务
可以停用；显式维护入口仍可用于质量抽查、诊断和补漏。
Codex 当前不执行 `async: true` command hooks，因此 Stop 注册为同步快速入队。
从早期共享提案版本升级时，重新运行安装器并在 `/hooks` 信任变更后的 Stop 定义。
当前部署模板通过 `maxDailyJobs` 设置每 UTC 日最多 300 次处理尝试；安装以 `--config-source` 指定的配置为准，使用私有配置安装的机器需在其中单独调整。v1 的 `unreceipted` 是本机当前可见但未看到回执的
提案数量；v2 以 publication 和 replica 状态分别核对，不证明 iCloud 已上传或对方
已下载。`peers.<machineId>` 为空表示本机尚未看到该机器状态，不能宣称两机已接通。

`llmwiki-wake` 默认执行一次纯 Python reconcile、可执行工作 drain 和 report；空闲时不调用
模型。v2 event worker 的 `WatchPaths` 是本机 `stateDir/queue`、`exchange/v2/publications`
根、每个 participant publication 子目录和 `exchange/v2/baseline.json`；不监听
`replica/current`、`status`、`report`、`machines` 或 exchange 根。v1 migration 保留 queue
加 publisher 的 submissions participant 目录或 contributor 的 receipts。LaunchAgent 仅用
`RunAtLoad` 和每 5 分钟 `StartCalendarInterval`，不使用 `QueueDirectories` 或 `KeepAlive`。
reader 的 v2 worker 只同步 replica，不调用模型；`--disable-event-worker` 或 `enabled:false`
会关闭 worker。失败 packet 会在下一次 reconcile 自动重读原 packet，导入成功后对应
exchange-errors 自动清理，不需手工删除。

v2 中旧提案迁移由指定迁移机的低频 reconcile 补入；旧提案目录不在 v2 WatchPaths 内。
原生 v2 业务采集和发布仍由事件触发。

触发范围按 `routing.resolve` 的既有规则：自有仓库要求 GitHub owner 在 `owners` 且非排除项，
`workingForks` 可按配置绑定但仍受排除路径约束；业务目录按最长匹配并要求工作语义和
requiredTerms；仓库外会话须同时出现业务别名与领域关键词，或是已有绑定会话的明确延续。
普通闲聊、歧义多业务提示和第三方仓库不会触发；零散但有用的会话可由用户手动触发，仍须
经过同一 resolver 和证据门禁。

Alma bridge 仍为显式手动入口，不是自动 prompt/Stop hook。当前版本的消息
格式与完整性限制尚未全部修复，首轮正式接入以 Codex 为准，不把 Alma 纳入
自动采集验收范围。

## 验收与恢复

### 会话增量与整页迁移

新采集任务按 `(projectId, sessionId)` 保存私有进度。默认静默 300 秒后整理、
连续有新材料时最长等待 1800 秒；显式整理可立即触发，批次字节数和每日额度仍生效。
会话摘要不能作证据；跨轮确认必须同时保留原方案与确认的精确引文。
`session-state/` 和 `consolidation/` 均留在本机，不通过共享目录交换。

新 publication 可包含独立审阅后的 `topicRevisions`，记录稳定 topicId、原页完整
SHA-256、新正文和 claim 索引。副本只重放受审正文，同页并发变更或原页 hash 不符时
待审；不按每轮新建页面。五分钟窗口不是永远复用模型进程，连续性由应用的持久状态保证。

全库历史整理使用 `v2/topic-routes.json` 的 version 2 envelope：保留原有 groups，
增加 `migration: {version: 1, basisRecordIds, pages}`。每个 page 包含项目与主题身份、
完整受审正文，以及 `previousPages: [{pageId, sha256}]`。先在冻结库副本验证每个旧页的
归宿、全部原始引用、导航和链接，再激活这份私有 manifest。所有机器应先升级；旧
runtime 遇 version 2 会拒绝本次 sync 并保留旧视图，不应继续承担共享 materializer。

由整页修订创建的主题页用 version 3 envelope 的 `merges` 合并（`migration` 可选并保持不变）。
每个 merge 指定存活页（必须是 `previousPages` 之一）、被吸收的修订记录 `absorbedRecordIds`、
受审正文、`mergedAt` 与理由。副本先重放被吸收的记录，核对每个旧页字节与 `sha256` 一致后
写入合并页、删除其余旧页并改写链接，再应用之后的记录；之后仍修订已删除旧页的记录待审。
migration 生成的页面可以作为存活页或被并入（每次重放 migration 都先于 merges）；
migration 已并掉或退役的旧页不能出现在 merge 中。
同样先在冻结库副本验证，所有机器升级到支持 version 3 的 runtime 后再激活。

已经有 ownership manifest 的共享库用普通 sync 迁移，无需再次运行旧文件认领。
明确合并的 baseline 页会记录 `retiredBaseline` 并保留备份；移除 migration 时恢复
原 baseline 页。人工重新创建旧路径会触发冲突，不自动覆盖。baseline 与 publication
原件始终不变，迁移文件须长期保留。库外/人工索引链接按受审映射单独修复。

知识留存遵循 [全库整理原则](../KNOWLEDGE-POLICY.md)。普通改写保留旧引用；经过明确审核的
`citationRetirements` 可以移除重复或已经由可靠出处承接的过程引用，同时保留长期决策。
`migration.retiredPages` 可按旧页 hash、项目、理由和外部记录退役纯过程页，不另建历史归档。
激活新增字段前，各参与机器都须升级；旧 runtime 会拒绝字段并继续持有旧视图。
部署验收要包括原始证据不变、必要引用有效、人工链接修复、反复重建不复活和后续主题增量更新。

1. 两机版本一致，角色为 writer/publisher 或明确 contributor；未知身份不能启用共享模式。
2. 任一机器提交有可追溯引文的真实项目决策。检查 publicationId 和本机 replica 状态。
3. 等待 iCloud 同步，另一机可见记录后由其 runtime 重建本机 replica 和索引。
4. 两机读取同步后的页面；同一 publication packet 重试保持 `recordId` 幂等。相同 claim
   但不同来源或适用条件仍作为不同记录保留并分别审核。
5. 相反结论必须待审；assistant 分析只能带日期作为 historical lesson，不能升级为事实、
   约束或用户决定；无业务闲聊不得入库。

前四项需要两台真实设备分别执行，本机的双 host fixture 不能替代它们。
不要为了验证把合成测试数据写进真实知识库。

回退前停用本机采集/发布并等 worker 结束。若回退涉及 materializer，先停止旧共享生成进程并
等待 iCloud 文件同步，再统一所有机器的 materializer 配置。安装器备份了发生变化的配置、
所有 launcher 与 hooks，必须作为一组恢复，避免 CLI 与 hooks 指向不同版本。
保留已发布知识、v2 baseline/publication 以及 v1 迁移提案和回执，不回滚整个 Wiki
到旧快照。不得让旧共享 compiler 与 v2 writer 同时写入同一状态；转换期间逐机完成
升级和同步即可。早期 runtime 产生且没有 ownership manifest 的旧页面或非 baseline 导航需要
人工核对，不能依靠升级自动认领、删除或覆盖。

私有 generation 带有构建时的完整性清单，绑定 generation 身份、文件集合和 worker response。
reader、非 materializer 和队列重试都须校验缓存；response 与页面来自同一份校验快照。
缺页、增页或改页会让本次消费报错，在 `replica-errors/generation-integrity.json` 记录原因，
将原目录保留到 `replica/quarantine/`，并撤销指向它的 current。下一次 sync 从 baseline/
publications 重建相同 generation，成功后清除活动错误，隔离目录和诊断记录不自动删除。
重建失败时保留错误和健康的 rollback，不暴露损坏内容，也不撤回共享页面。

已固定的任务不改写 `replicaBasis`；原路径重建后可以继续重试。若历史 generation 的记录
集合已经变化，无法从当前输入重建相同旧上下文，任务保持 `sync-retry`，需要人工恢复。
不要对损坏缓存重写清单或用最新 generation 替换旧任务的证据基线。没有 ownership 的旧共享页
即使与当前输出逐字节相同，也必须先人工核对迁移。

当前自动流程不等同于无限期无人维护：现有全项目审核上下文仍有单页 12,000
和项目总量 120,000 字符限制，超限会明确失败并隔离任务；冲突需人工处理。
日常维护应检查 failed/review/exchange-errors、Wiki lint 及引用抽查。
