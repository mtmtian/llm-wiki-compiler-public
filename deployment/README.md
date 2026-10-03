# 配置与新设备接入

**共享知识协议 v2：每台 participants 机器都可独立发布不可变记录。请先读 [SHARED-KNOWLEDGE.md](SHARED-KNOWLEDGE.md)。**
本目录的 `--writer`、`--publisher`、`--contributor` 和明确 machineId 取代旧单 writer
切机流程。v2 中 writer 与 publisher 都是本机采集并发布，contributor 只提交提案，
reader 不采集也不发布；v1 仍接受唯一 `publisherMachineId` 以便迁移。v2 仅为旧提案迁移指定
`legacyImporterMachineId`，该字段只放在仓库外的私有完整配置中；未配置时关闭旧提案导入，
contributor 必须配置迁移机。原生 v2 发布不依赖该机器在线。

仓库保存可复用代码和通用部署模板。提交的 `knowledge-flow.json` 不含 owners、workingForks、
checks、项目路径或 exchange 参与者，新安装默认 reader。实际项目策略保存在仓库外的完整
`knowledge-flow.json`，安装时通过 `--config-source /absolute/path/to/knowledge-flow.json` 指定；
主机身份与本地路径保存在 `~/.config/llmwiki/machine.json`。GitHub/Codex 登录、Wiki 正文、
运行队列、待审证据和会话记录均不随仓库复制。

## 可选 Jev 排序

[Jev 接入与双机同步](JEV-TRIAL.md)说明默认关闭、单机额度分配、凭据准备和 on/off/status 命令。只同步源码不会自动启用付费请求。

## 已有部署升级

从仓库配置的维护分支已审查提交构建；先确认工作树干净、分支与远端提交一致，再构建。
不要覆盖本机未提交工作，也不要使用不含当前共享协议或仍注册不受支持异步 Stop 的旧 runtime。

v2 中每台 writer/publisher 都独立采集、审核并写入自己的不可变 publication，随后在本机
replica 中重建私有检索视图。共享 Obsidian 页面只有一个当前写入者。启用
[`exchange.sharedWriter` 协调模式](SHARED-WRITER.md) 后，`materializerMachineId` 指定默认机，
另一台可在用户明确授权后申请一次性接管，收到持久让出回执后写入并自动归还。升级不会
无意切换既有 materializer。publisher 是独立发布角色，
不等于当前页面写入者；reader 和 contributor 不取得页面写入权。兼容模式缺少
`materializerMachineId` 时，shared projection 关闭。当前写入机
离线只会延迟共享 Obsidian 展示；两台机器仍可独立发布记录并继续使用各自的私有 replica。

共享 projection 的冲突不会冻结本机 `replica/current`：同步保留已构建的本机视图，同时在
`stateDir/replica-errors/shared-materialization.json` 记录错误。`--check` 会把该状态判为失败，
修复人工冲突后每次同步都会重试，包括 publication digest 没变化的同步。materializer 在
私有 `stateDir/shared-materialization.json` 保存受管文件 ownership hash，并用
`shared-materialization-pending.json` 恢复中断计划。替换或撤回旧文件时，原文件保留在同目录
`.llmwiki-preserved-*.bak`；它们不是 Markdown，不会被采集，也不会自动删除。人工编辑永远
报冲突且不覆盖。

1. 主机运维记录、真实路径与观察结果保存在本机，不提交到仓库或覆盖其他主机的记录。

   ```sh
   mkdir -p "$HOME/.config/llmwiki"
   # 按本机实测维护 ~/.config/llmwiki/README.local.md
   ```

2. 检查 `~/.config/llmwiki/machine.json` 的 `wikiRoot`、`projectPaths`、`excludedPaths` 和 `machineId`。路径必须是本机真实路径；已有 `machineId` 要保持并核对，不要复制另一主机的 `machine.json`、state 或运行队列。
3. 在每台设备先构建并用 `--reader` 做 dry-run 与实际安装。确认检索、过滤和 hook 后，
按共享协议显式安装 `--writer` 或 `--publisher`；两者在 v2 都开启本机采集和独立发布，
需要只采集时使用 `--contributor`。

   ```sh
   cd /path/to/llm-wiki-compiler
   git status --short
   git rev-parse HEAD
   python3 deployment/build-runtime.py
   runtime="$HOME/.local/share/llm-wiki-compiler/releases/$(git rev-parse --short=12 HEAD)"
   private_flow_config="$HOME/.config/llmwiki/private-knowledge-flow.json"
   machine_id="YOUR_REGISTERED_MACHINE_ID"
   python3 deployment/install.py --runtime "$runtime" --wiki-root "$WIKI" \
     --config-source "$private_flow_config" --machine-id "$machine_id" --reader --dry-run
   python3 deployment/install.py --runtime "$runtime" --wiki-root "$WIKI" \
     --config-source "$private_flow_config" --machine-id "$machine_id" --reader
   ```

重新安装已有双机时，使用原完整私有配置源，并显式沿用每台原有 `machineId`、角色参数和
`--event-driven` 选择。不要用公开中性模板替换私有配置或复制另一主机的凭据、Codex/Alma
state、队列、review、登录凭据或绝对路径；只迁移仓库源码和非密钥通用配置。Alma 当前没有
受支持的原生自动 prompt 注入，必须显式调用 MCP 或
`~/.local/bin/llmwiki-alma-session --thread "$ALMA_THREAD_ID" --cwd "$PWD"`，不能把 bridge
或 fixture 验证宣称为自动注入。

### 共享 materializer 的切换与旧页迁移

materializer 默认写入与一次性接管按 [SHARED-WRITER.md](SHARED-WRITER.md) 的双机升级、
版本申报和原所有者 bootstrap 执行。以下步骤只适用于尚未启用协调协议的旧部署；
启用后不能通过直接改身份、删除回执或旧 runtime 回退来切换。

兼容模式下 `exchange.materializerMachineId` 是共享 Obsidian 投影的唯一写入身份，不是 publication
发布者选择。iCloud 的 `flock` 只能协调同一台机器上的进程，不能提供跨机器锁。切换
materializer 前，先停止旧 materializer 的共享生成进程，并升级或停用仍不认识当前门禁的
旧 runtime；等待 iCloud 完成文件同步，再在所有机器统一 `materializerMachineId`。
不要让新旧 runtime 同时写同一共享根。

早期实现可能没有 ownership manifest。旧 runtime 生成但未被当前
`stateDir/shared-materialization.json` 认领的页面，以及不再等于 baseline 的 `wiki/MOC.md`
或 `wiki/index.md`，需要人工逐项核对和迁移；升级流程不宣称自动兼容。保留本机
`stateDir`、publication、review、队列和其他 metadata，只迁移源码与非密钥配置，不复制另一台
机器的 credentials、state 或绝对路径。

新 generation 在构建完成时记录投影文件清单、内容 hash 和 worker response hash。
所有角色复用缓存或重试已固定的任务上下文前都必须校验，response 与页面使用同一份校验快照。
发现损坏的这次 sync/prepare 会记录 `stateDir/replica-errors/generation-integrity.json` 并报错，
把原目录保留到 `stateDir/replica/quarantine/`；若它是 `current`，立即撤销该指针。
下次 sync 从不可变 baseline/publications 重建相同 generation，成功后清除活动错误，
隔离目录及其诊断记录继续保留。重建失败时不恢复损坏的 current，共享页面保持原状。

已有队列任务的 `replicaBasis` 保持不变，同一代在原路径重建后即可重试。如果损坏的是历史
任务固定的旧 generation，而当前记录集合已改变，任务会保持 `sync-retry`，需要人工恢复
原来的可信上下文；不能静默改用最新 generation。不要给损坏缓存重新生成清单，也不要删除
共享页面、ownership/pending 状态或 publication 来绕过错误。

## 仓库中的配置

| 文件 | 用途 |
|---|---|
| `knowledge-flow.json` | 中性默认值；新安装 reader-first，项目集合为空，不定义私有路由或 exchange |
| `compiler-environment.json` | Luna、中文输出、并发 2、Ollama nomic-embed-text 等非密钥配置 |
| `machine.example.json` | 虚构的主机身份与路径示例；实际文件位于 `~/.config/llmwiki/machine.json` |
| `--config-source` | 安装时读取仓库外私有完整配置的显式入口 |
| `build-runtime.py` | 从干净提交和锁文件构建独立运行目录 |
| `install.py` | 生成本机配置、入口和 hooks，保留无关 hooks 并备份变更 |
| `maintenance-prompt.md` | 事件 worker 的低频兜底与周一抽查说明；不再要求每小时启动 Codex 维护会话 |

`${HOME}`、`${REPO}`、`${RUNTIME}`、`${NODE}`、`${GH}`、`${WIKI_ROOT}` 由安装器展开，不绑定用户名。项目 ID 不要随机器改名。共享模式必须在私有 `machine.json` 或安装参数中明确提供稳定 `machineId`；安装器不会臆造或迁移机器身份。v2 的 `sharedWikiRoot` 是原始 iCloud 根，`wikiRoot` 是本机 `<stateDir>/replica/current` 视图；`machine.json.wikiRoot` 仍填写共享根。公用模板只排除编译器运行和状态目录，不定义任何真实项目路径。

自动回用按已核实的项目检索相关决定段落，保留适用范围和对应引用，不再注入 operational README。明确询问 Wiki 运维时，才读取当前配置、运行提交、队列和经过协议校验的共享写入状态，并标注观测时间；历史维护快照不会冒充实时状态。README、`*.history.md`、`old-machine` 和备份目录仍不作为自动 artifact/evidence。完整读链路、原生 hooks 验收及升级注意事项见 [Wiki 回用](CONTEXT-REUSE.md)。

## 新主机安装

先准备 Git、Python 3、Node.js 24+、npm、GitHub CLI、Codex CLI、Ollama。通过各自的官方登录方式完成 `gh auth login` 和 Codex 登录。确认 iCloud Wiki 已完整下载；首次由后台进程访问 iCloud 文件时，在 macOS 隐私提示中授予对应终端/运行宿主访问权限，并以实际读取结果核验。两机 embedding 模型都使用 `nomic-embed-text`。

```sh
gh repo clone OWNER/REPOSITORY
cd llm-wiki-compiler
git rev-parse HEAD
git remote add upstream https://github.com/atomicstrata/llm-wiki-compiler.git
ollama pull nomic-embed-text
python3 deployment/build-runtime.py
```

构建完成会输出固定运行目录。首次下载依赖及 embedding 模型需要网络；没有登录或模型时不自动购买、使用其他账号或换模型。

将 `deployment/machine.example.json` 复制到 `~/.config/llmwiki/machine.json`，填写本机身份与路径。
实际 owners、working forks、项目路由、检查项和 exchange 保存在仓库外的完整私有
`knowledge-flow.json`，安装时显式传入 `--config-source "$PRIVATE_FLOW_CONFIG"`。
`machine.json` 的 `projectPaths` 是本机的精确路径覆盖；不要复制其他主机的路径，也不要把
临时聊天父目录映射为项目路径。

```sh
task_runtime="$HOME/.local/share/llm-wiki-compiler/releases/$(git rev-parse --short=12 HEAD)"
python3 deployment/install.py --runtime "$task_runtime" --reader --dry-run
python3 deployment/install.py --runtime "$task_runtime" --reader
```

新安装默认 reader。共享模式必须在私有配置或 `--machine-id` 中提供已登记的
`machineId`，不得新增未知参与者。v2 的 writer/publisher 都设置 `intakeEnabled=true` 与
`publishEnabled=true`；contributor 为 `true/false`，reader 为 `false/false`。升级已有主机时
继续使用原配置、machineId、角色参数和 `--event-driven` 选择。

事件驱动 worker 是单独的显式选项。准备 writer、publisher 或 contributor 时加 `--event-driven`，安装器写入 launcher、LaunchAgent plist、私有状态目录，以及该角色需要的共享 exchange 目录（v2 会预建 `v2/publications/<participant>`，不会创建 `baseline.json` 或 publication 文件）；`--dry-run` 会逐项报告这些目录的 `create`/`unchanged` 状态，且不写入任何目录或文件。安装器**不会调用 `launchctl`**。v2 reader 如显式启用，只执行无模型 replica sync；`enabled: false` 和 `--disable-event-worker` 会关闭 worker。先确认配置已实际落盘，再用专门命令启用、查看或回退：

```sh
python3 deployment/install.py --runtime "$task_runtime" --machine-id YOUR_MACHINE_ID \
  --writer --event-driven --dry-run
python3 deployment/install.py --runtime "$task_runtime" --machine-id YOUR_MACHINE_ID \
  --writer --event-driven
python3 deployment/install.py --event-worker-action status
```

两台 v2 机器按以下顺序完成接入：先用 `--writer --event-driven` 或 `--publisher --event-driven` prepare 并完成
dry-run/安装；在 Codex 原生 `/hooks` 中信任新建或变化后的 hook；若这是**全新的共享根且
尚无 `.knowledge-exchange/v2/baseline.json`**，仅在一台机器执行一次 `~/.local/bin/llmwiki-maintain
--initialize-baseline --announce`，已有共享 baseline 不重复初始化；随后在每台机器执行
`~/.local/bin/llmwiki-maintain --check --announce`，让本机 replica 建立并核对 generation；
确认配置、replica 和 hook 已落盘后再启用服务：

```sh
~/.local/bin/llmwiki-maintain --check --announce
python3 deployment/install.py --event-worker-action enable
python3 deployment/install.py --event-worker-action status
```

其他机器使用相同步骤，保留各自已登记的 machineId、本机 state、队列和登录。

### 旧配置的源码排除迁移

旧版本曾把自己的源码 checkout 写进 `excludedPaths`，并把编译器仓库写进
`excludedRepos`。新模板已经去掉这两个默认项，但安装器会保留已有配置中的路径，避免
静默放开用户自己配置的目录。若要恢复一个明确的自有 checkout，升级时显式传入该目录：

```sh
python3 deployment/install.py --runtime "$task_runtime" --machine-id YOUR_MACHINE_ID \
  --writer --event-driven --allow-repository "/absolute/path/to/llm-wiki-compiler" --dry-run
python3 deployment/install.py --runtime "$task_runtime" --machine-id YOUR_MACHINE_ID \
  --writer --event-driven --allow-repository "/absolute/path/to/llm-wiki-compiler"
```

`--allow-repository` 可以重复使用，但每个路径都必须是现存 Git checkout，origin 必须是
GitHub 仓库，且 owner 或 `workingForks` 允许该仓库。它只删除与该 checkout
根目录完全相等的旧排除项及同一仓库身份的旧 `excludedRepos` 项；父目录、其他自定义排除和
编译器的 runtime/state/replica 目录不会被清理。第三方旧任务目录仍保持精确排除。

启用后用 `launchctl print gui/$(id -u)/com.llmwiki.knowledge-flow.wake` 检查最近一次
`last exit code = 0`，并查看本机 `stateDir/reports/wake.log`。空队列应为 `reason=empty`、
`attempts=0`；服务等待事件时显示 `state=not running` 属正常状态。再用一个有真实可复用
结论的业务回合验证 Stop 入队，等待合批后核对审核结果、publication 和本机 replica。空队列
成功只能证明同步路径，完整后台验收还须覆盖实际提炼与审核。若出现原生隐私提示，按实际
提示授权并重新核验；前台直接运行 `llmwiki-wake` 的结果不能代替 launchd 验收。这里的
`Stop` 是当前 Codex 回合准备返回最终回复时的同步回调，不是任务归档事件；归档不会额外
触发一次 Wiki 整理。

`enable` 只接受已准备好的本机配置；`bootout` 停止自身 LaunchAgent；`rollback` 先尝试 bootout，再删除自身 plist 和 `~/.local/bin/llmwiki-wake`，不会删除队列、证据、review 或 exchange。`--dry-run` 不运行 `launchctl`。配置缺失或 JSON 损坏时，`bootout`、`status` 和 `rollback` 仍可操作本工具的固定服务标识；只有启用需要有效配置。Linux 或其他平台只生成和检查配置，不会声称事件 worker 已激活。

在 Codex 原生 `/hooks` 信任新建或定义变化后的三个 Wiki hook（UserPromptSubmit、Stop、SessionStart），再新开任务检验项目参考与 Stop 事件。安装器不会伪造信任或绕过原生检查。hook 命令调用稳定启动器 `~/.local/bin/llmwiki-codex-hook --config <配置>`，启动器在运行时按配置的 `worker` 找到已安装 runtime 的 `hooks.py`；因此升级 runtime 时 hook 定义和启动器都不变，不需要重新信任。只有首次安装、从旧版（命令中带 runtime 路径）迁移，或配置路径改变时才需要信任一次。其他设备或旧版本的验证不能证明当前安装已生效；桌面宿主须在该设备的新任务中另行验证。

### 按语义主题组织 Wiki

新版本支持按知识主题和决策对象跨项目更新、追加。项目保留为证据来源与适用条件，
采集准入和预算不变；同主题的不同约束和冲突必须分别保留，不能把项目结论直接泛化。
旧页面 ID、引用和原有修订不变，只有后续受审更新会移除页面的单项目所有权。

先在所有 exchange participants 安装新版、完成各宿主 Hook 信任并执行：

```sh
llmwiki-maintain --announce
llmwiki-maintain --semantic-topics status
```

申报必须来自各机器实际安装的 runtime；build manifest 和 announcement 都应包含
`semantic-topic-revisions-v1`（能读取账本记录的 runtime 还会申报 `knowledge-ledger-v1`，见
[KNOWLEDGE-LEDGER.md](KNOWLEDGE-LEDGER.md) 第 7 节）。所有参与者就绪后，在任一机器执行：

```sh
llmwiki-maintain --semantic-topics enable
llmwiki-maintain --semantic-topics enable --apply
llmwiki-maintain --check
```

账本记录的启用规则相同，但是独立的门控：所有参与者的申报都包含 `knowledge-ledger-v1` 后，用
`llmwiki-maintain --knowledge-ledger status` 查看，`--knowledge-ledger enable` 预览，加 `--apply` 写入共享策略
`v2/knowledge-ledger.json`。启用后不提供停用：已有的账本记录必须一直可读。启用前须单独授权。

第一条只预演；第二条写入共享 `v2/topic-scope.json`，所有新版机器下一次运行时读取。
任一旧版、缺失或本机版本不一致的申报都会阻止启用，发布前还会复核兼容状态。
旧 reader 可能跳过未知修订，因此不能只升级本机就启用，也不能代写其他机器的申报。
启用后继续使用支持此协议的 runtime；不要通过删除策略文件或降级 reader 回退语义数据。

新采集任务按全库主题规划，旧队列和旧待审重试保留冻结的项目范围。主题导航会重建，
存量页面不会因启用而被批量改写、合并或删除。`get_knowledge_context` 提供跨项目主题补查；
`get_project_context` 仍可按来源项目筛选。

### 采集延迟与待审重处理

Stop 早于原生记录落盘时，输入保存在本机 `capture-pending`，worker 按有限退避自动补采。
待补采独立计数，不立即通知为采集异常；超出重试次数或时间窗口后才进入 `capture-errors`。
补采只读指定会话原文，并核对 session、turn、cwd 和受信目录；支持同名归档路径和有界长文件读取，
不会从其他会话猜测证据，也不会把后来改过的本地文件重新当作当时证据。

草稿校验失败会把具体 claim、证据 ID、引用或权限错误交给一次纠正。纠正时模型只能选择
冻结原文片段的 `quoteId` 和冻结的 `targetPageId`，程序恢复原文、对应来源以及目标页的规范
topic/decisionObject，避免模型改写引用、把片段配到错误来源或改写本次计划的目标；未知片段或目标页被拒绝。
既有页更新保留 pageId、topicId 和原文哈希；明确的更新计划可以修正过期主题标签，
由独立审核核对仍是同一业务对象，并保留原决策的理由与变更脉络。标签描述长期讨论对象，不能把一次性结论固定成永久身份。
纠正上下文按目标页列出尚未保留或声明退役的旧引用；模型须保留它们，或提出带理由和有效替代的退役申请，仍由独立审核判断。
每个退役项的 replacement 只能是一个存活引用、一个 `{{claim:N}}` 或证据内的 HTTPS URL；说明文字放在 reason。
没有独立复用价值的一次性执行流水可以由同决策对象的存活知识承接，无需保留执行细节或另建历史档案；独有决策理由、预算、约束、风险和必要验证证据仍须保留。
初稿格式错误仍进入原有的一次纠正，纠正失败继续待审。审核回执只允许列出本次实际修改页、claim 和退役引用，不能把目录中未修改的页面列作已审核。
纠正输出按主证据角色限制 `kind`、`status` 和补充证据范围：assistant 只能形成历史 lesson，
artifact 只能形成历史事实、约束或经验；只有用户主证据可选择 assistant 补充引文。
宿主以 user 角色注入、但并非用户本人输入的消息（后台任务通知 `<task-notification>`、自动化心跳 `<heartbeat>`、
页面事件 `<external_codex_apps_open_page>`、环境上下文 `<environment_context>`），若整条消息只由这些块组成，采集时记为 artifact 证据；
消息里还有用户自己的文字时仍按 user 证据处理。
结构约束通过仍须经过原文校验与独立全文审核，真实的事实矛盾和未确认决策会继续待审。
草稿阶段与发布阶段使用相同的证据角色约束：assistant 补充引文只能解释用户主证据，
不能给另一条 assistant 或文件主证据增加决策权限；不合规组合先纠正，再独立审核。
已保存的模型结果如果在发布时因自身内容违反契约而被拒绝（例如在当前证据角色约束之前生成），
重试同一结果永远不会成功：批次不再退避，直接记为待审，原因写明违反的契约，被拒结果保留在
批次审计中，可用下面的 `--retry-review` 按当前约束重新起草。交换目录或网络等其他发布失败仍按原退避重试。
一个批次等待发布或副本重试时，只暂停同一会话的后续整理，其他会话可以继续；
每日预算耗尽仍暂停所有新模型工作，已生成的结果保留并按原退避时间重试。
某个项目的待审数量达到 `maxPendingPerProject` 时，它尚未领取的新工作留在队列中等待：
不领取批次、不固定副本版本、不调用模型、不扣日预算，等待审数量回落后再与同会话的新回合
一起处理；唤醒状态的原因显示为 `review-queue-full`。已经领取的批次照常处理，以便完成收尾。
等待中的任务最多占用共享队列（`maxQueuedJobs`）的一半，超出后不再等待，
批次按原有规则记为 `review queue is full` 待审，以免一个项目的积压阻塞所有项目的采集。
已有待审项不会随升级自动批准。修复原因后，可逐项先检查再加入普通队列：

```sh
llmwiki-maintain --retry-review JOB_ID --dry-run
llmwiki-maintain --retry-review JOB_ID
llmwiki-maintain --drain
```

该入口仅接收已完成且结果为 `needs_review` 的会话批次。新尝试保留原证据，以当前已接受的
Wiki 副本重新规划；原批次、固定 basis 和模型缓存保持原状。重处理遵守原队列和日预算，
不会重放旧会话进度。成功或无新增知识时移除原待审通知；仍有疑点则保留后继待审项。
原输入或人工审阅记录变化时停止接续并报告。`review-retries`、`resolved` 仅保存本机操作证据，
不生成 Wiki 历史归档。处理失败或后继记录丢失时不会删除原待审项。
普通处理三次失败后，如原因已修复，再次显式执行 `--retry-review` 会创建新尝试；
旧失败记录保留，不自动无限重试。输入漂移在调用模型和扣日预算前拒绝并隔离。
因待审已满而记为 `review queue is full` 的批次没有待审文件，以其 `audit` 记录作为冻结依据，
同样可以用 `--retry-review` 重处理。它不会腾出待审名额，所以只在该项目待审未满时才能加入队列；
若处理时再次因待审已满被拒，原记录保持不变，可在有空位后再试。

## MCP 与维护调度

Claude Code 与 Pi 使用[可插拔 Agent 接入](AGENT-PLUGINS.md)：同一安装器登记原生事件和共享 MCP launcher，
策略仍只保存在 `knowledge-flow.json`，升级运行时不需要复制两份规则。安装器只管理自己的注册项，
支持 dry-run、精确禁用和私有备份；配置存在与真实会话成功投递分别验收。
Pi 扩展由宿主加载，`@earendil-works/pi-coding-agent` 仅作宿主类型导入，不属于 compiler 的运行依赖；
Fallow 将扩展标记为独立入口，并排除这一个宿主提供的依赖检查。

MCP 可用于主动查询和手动工作；hooks 不依赖 MCP 调用才能注入参考。新机器通过 Codex 原生 CLI 注册：

```sh
codex mcp add llmwiki -- "$HOME/.local/bin/llmwiki-local" serve --root "$HOME/.local/share/llm-wiki-compiler/knowledge-flow-state/replica/current"
```

若使用自定义 stateDir，替换 `--root` 为该机配置的 `wikiRoot`。只有全新的共享根且不存在
`.knowledge-exchange/v2/baseline.json` 时，才执行一次 `~/.local/bin/llmwiki-maintain --initialize-baseline --announce`；
已有共享 baseline 不重复初始化。之后维护从 baseline 加 publication 重建本机 replica。安装器
不复制 Wiki、不初始化 baseline、不创建 replica 链接；replica 缺失时 `llmwiki-local` 会明确
退出，不会回退共享写入。已有同名 MCP 时先查看 `codex mcp get llmwiki`，更新该项而不覆盖
整个 Codex 配置。若实际启动或工具耗时需要更长时间，只在
`[mcp_servers.llmwiki]` 设置适合该主机的 `startup_timeout_sec` 和 `tool_timeout_sec`。本机视图
更新后自动保留当前版本、上一版和仍被审核批次引用的版本，回收其余可重建视图；不会清理
原始证据或共享记录。已有 MCP 进程需刷新后加载新入口。安装器不会覆盖整个 Codex 配置；先用
`codex mcp get llmwiki` 核对 command、root 和超时，再用真实 Codex 会话调用 `search`/`read_page`，
确认返回当前本机 replica 内容。

Alma 集成边界：当前 Alma 0.4.43 CLI 没有原生 hooks 注册命令，也没有发现可写入的 Alma MCP server 配置入口；`alma tool find 'llm wiki MCP server'` 返回 0。因此安装器只生成 `~/.local/bin/llmwiki-alma-session`，不伪造 Alma hook。它通过 `alma thread messages THREAD 80 --full --offset N --json` 分页读取完整可见 user/assistant 消息，并保留、校验每页的 `hasMore`、`nextOffset`、`missingMessageIds` 和 `sourceTextTruncated` envelope 字段；缺失消息、文本截断或游标不连续都会 fail closed。随后走同一 `routing.resolve`、`hooks.py` Stop、独立提炼/审核和 compiler 发布路径。它不会向已经发送的 Alma prompt 注入检索上下文；Alma 侧检索必须显式调用已注册 MCP，或等待 Alma 提供受支持的 prompt hook。

```sh
~/.local/bin/llmwiki-alma-session --thread "$ALMA_THREAD_ID" --cwd "$PWD"
```

该命令不创建第二个队列，也不绕过 `intakeEnabled`、指定 publisher、每日预算或原过滤审核路径。Alma bridge 的消息格式与完整性限制仍有未完成项，首轮正式接入以 Codex 为准；不能把 bridge 或 fixture 结果作为 Alma 自动采集验收。

共享模式不再依赖每小时启动 Codex 会话。Stop 事件只做有界采集和入队；已准备并显式启用的 `llmwiki-wake` LaunchAgent 在新队列、v2 publications、baseline 或 v1 migration signals 出现时唤醒，并以 `RunAtLoad` 加每 5 分钟的 `StartCalendarInterval` 日历兜底恢复睡眠期间错过的事件。安静 300 秒和最长等待 1800 秒的会话合批规则保持不变，短时进程内 debounce 仍最多等待 120 秒；日历唤醒只提供下一次处理机会，不提前处理未到期会话。空闲唤醒仍会执行有界 reconcile；v2 reader 会同步本机 replica，但不会调用模型，所以 `run_once` 不是纯计数操作。v2 writer/publisher 各自发布不可变记录，contributor 只提交提案。原有 Codex 自动任务可以停用，不要创建新的每小时维护会话；周一质量抽查仍需显式运行维护命令，wake 不会自行执行质量抽查。

v2 LaunchAgent 的 `WatchPaths` 包含本机 `stateDir/queue`、`exchange/v2/publications`、每个 participant 的 publication 子目录和 `exchange/v2/baseline.json`；协调模式额外监听 `v2/shared-writer` 及各机的 requests/releases 子目录。不监听 replica/current、status、report、machines 或 exchange 根。v1 迁移保留 queue 加 publisher 的 submissions participant 目录或 contributor 的 receipts。所有版本都不使用 `QueueDirectories` 或 `KeepAlive` 热重启；事件目录在准备实际配置时预创建，路径含空格时仍使用 plist 原生参数。

事件 launcher 为 launchd 显式生成受控 `PATH`：按优先级包含发现到的 `codex` 所在目录、配置的 Node 目录和 `/usr/bin:/bin:/usr/sbin:/sbin`，不继承交互 shell 的整份 PATH。writer/contributor 在准备可处理模型工作的 launcher 时找不到可执行 `codex` 会直接失败；v2 reader 的无模型 replica sync 不需要 codex。后台首次读取 iCloud 的授权仍以 macOS 实际隐私提示和读取结果为准。

## v1 迁移与 v2 回退

此节描述未激活共享写入协调协议的部署。已激活部署遵循 [共享写入恢复约束](SHARED-WRITER.md#故障与恢复)，不能直接恢复旧 runtime。

1. 在旧写入设备停止采集、队列处理和共享页面生成，确认正在运行的 worker 已结束；不认识
   当前 materializer 门禁的旧 runtime 不能继续写同一共享根。保留旧 runtime
   和本机 state 作为回退材料，不把它们复制到另一台机器。
2. 等待 iCloud 完成文件同步；两台机器核对共享 baseline、模型和项目 ID。首次 v2 接入只在
   真实全新共享根执行一次 baseline 初始化，已有 baseline 不重新冻结。
3. 从已审查的维护分支构建并在各台机器分别安装各自角色。保留独立 writer/publisher 的
   publication 与私有 replica；在所有机器配置同一个 `exchange.materializerMachineId`
   （例如虚构身份 `peer-a`），只让该 machineId 的后台进程生成共享 Obsidian 页面。
4. 对旧 runtime 已生成但没有 ownership manifest 的页面，以及不再等于 baseline 的导航文件，
   人工核对后再纳入新 materializer 管理。升级流程不自动删除或覆盖这些遗留文件。

旧提案只由所有机器配置一致的 `legacyImporterMachineId` 迁移。更换迁移机时先停旧进程、
处理/移交积压并同步配置，再启动新进程；旧 runtime 不识别该门禁，必须先停掉其他旧导入端。
v2 下已有的非迁移机旧队列或待发布结果会保留并报告重试异常，不调用模型，也不重复发布。

`--reader` 不能终止已经在运行的任务，也不阻止手动 compiler 写命令。v2 不共享编译状态；
禁止手动运行旧共享 compiler 写状态。切换角色本身不改变 hook 命令；变更运行目录才需要重新
信任。materializer 切换前必须先停止旧共享生成进程、等待云端同步，再统一配置；不能把
iCloud `flock` 当成跨机锁。

## 验证与回退

```sh
python3 -m unittest discover -s deployment -p 'test_*.py'
python3 -m unittest discover -s extensions/knowledge-flow -p 'test_*.py'
~/.local/bin/llmwiki-maintain --check
(cd "$HOME/.local/share/llm-wiki-compiler/knowledge-flow-state/replica/current" && ~/.local/bin/llmwiki-local status)
```

用真实项目问题确认只注入相关来源；普通闲聊应没有 Wiki 注入。自有仓库（`owners` 且非排除项）和 `workingForks` 会按仓库身份触发；配置项目目录还需要工作语义和平台 requiredTerms。仓库外会话只有业务别名与领域关键词同时出现，或已有会话绑定明确延续时才触发。正常业务回合中可由 user/assistant 的可见原文和明确引用证据形成候选；助手分析只能作为带日期的 historical lesson，不能升级为事实、约束或用户决定；没有新价值时产出零条。正式发布还要经过精确引文和独立审核，冲突不自动批准。

安装器只备份发生变化的本机文件，备份位于 `~/.local/share/llm-wiki-compiler/install-backups/`。回退时先停写并等待 worker 结束，作为一组恢复配置、所有 launcher 和 hooks，原生信任恢复后的定义；保留旧运行目录。不要将已产生业务记录的 Wiki 直接覆盖成旧快照。

上游集成规则见 [FORK.md](../FORK.md)。提交通用代码时排除私有项目配置、主机覆盖、Wiki 内容和部署状态。
