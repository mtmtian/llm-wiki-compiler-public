# CI 门禁与验收标准

本项目的自动门禁是 **`CI Gate`**。只有所有必需 job 成功，它才成功。
工作流位于 [ci.yml](.github/workflows/ci.yml)。两台机器统一通过 `npm run pr:merge`
核验审查版本与 CI 后合并；GitHub 服务器端的强制拦截另需分支保护支持。

## 1. 好的 CI 应满足什么

| 标准 | 本项目的判断方式 |
| --- | --- |
| 能发现会影响用户的问题 | 验证 CLI/SDK 可安装、原始证据与人工修改不会损坏、重试不重复写、冲突不被自动接受 |
| 可复现 | `npm ci` 使用锁文件；Node 与 `package.json` 的 Volta pin 一致；Fallow 固定 2.82.0；Actions 固定 commit SHA |
| 失败可信 | 不自动重试到绿、不使用 `continue-on-error`；失败、取消、跳过都不能通过最终 gate |
| 反馈及时 | Linux 全量测试分两组并行；同一 PR 的旧运行自动取消；job 有超时；JUnit 结果保存 7 天 |
| 能在本地解释和复现 | 使用仓库脚本和现有测试；类型错误按文件比较历史基线；Fallow 在本地与 CI 使用相同全量命令 |
| 权限与成本合适 | `contents: read`、checkout 不保留凭据；常规测试不调用真实模型；macOS 验证实际使用的平台 |
| 合并依据准确 | 共享命令核对受审 HEAD/base 与最新自动 PR CI；本地拒绝不等于服务器保护，不能仅看某次 workflow 曾经变绿 |

## 2. 自动执行的硬门禁

| Job | 检查 | 通过标准 |
| --- | --- | --- |
| `static-checks` | 主项目与 knowledge-flow 的 TypeScript 类型检查 | 零错误 |
| `static-checks` | 测试代码类型检查 | 不增加任何文件的诊断数；不允许向上修改基线；历史错误不冒充已清零 |
| `static-checks` | release docs | 版本变更时 README 与 CHANGELOG 同步；比较使用真实 PR base / push before / merge-group base commit |
| `build-and-test` | Linux 两个分组、macOS 全量 Vitest；CLI/SDK build；扩展 build | 所有自动用例通过；worker 和 hook 构建产物存在 |
| `build-and-test` | CLI tarball 安装冒烟；Linux 第一组额外执行 SDK tarball 安装冒烟 | 安装后的 CLI 可启动、帮助可读，SDK 可导入且声明文件存在 |
| `knowledge-flow-python` | Linux 与 macOS 各跑 hook/queue/replica suite 和 deployment suite | 全部通过；临时文件与 fake 服务隔离真实机器状态 |
| `codebase-health` | 全量 `npm run fallow:ci` | 死代码、重复代码、超过现有复杂度阈值的问题为零；一般重构建议不等于失败项 |
| `ci-gate` | 所有上述 job 的结果 | 每项都必须是 `success`，失败、取消、跳过及空依赖集均拒绝 |

`main` 与 `personal/stable` 的 PR、push 都触发检查；同时支持 `merge_group`
和手动运行。没有路径过滤，文档 PR 也会产生门禁结果。原独立 test-typecheck
workflow 已合入这条依赖链，避免其失败被最终门禁遗漏。
手动运行允许指定调试基准，但最终检查名为 **`CI Gate (manual)`**，不能用它
满足 PR 的 `CI Gate` 要求，避免用 `HEAD` 自比较掩盖基线或 release docs 回归。

Node 固定 24.16.0，Python 验证 3.14。Python 3.10 是目前代码语法推导的下限，
**本配置没有据此宣称 3.10 已验收**。Windows 的文件系统隔离还有已知限制，
不列入已验证平台。更换运行时版本时应同时修改 pin 和验证矩阵。

### 重点保护的业务不变量

| 不变量 | 现有测试入口 |
| --- | --- |
| 多进程追加关系只有一个锁持有者；关系和最终事件链完整；owner 写入失败不发布锁 | `test/lock-publication.test.ts`、`test/relation-store-concurrent.test.ts` |
| evidence 来自允许的项目、会话与当前回合；隐藏消息、越界路径和凭据被拦截或脱敏 | `extensions/knowledge-flow/test_capture.py`、`test_hooks.py` |
| hook 只做有界采集入队；预算、消息大小和 claim 数量限制在模型调用前生效 | `extensions/knowledge-flow/test_intake.py`、`test_queue_limits.py`、`test/knowledge-flow-limits.test.ts` |
| 引用与独立审核不可绕过；冲突保留 review 状态，不自动发布 | `test/knowledge-flow-pipeline.test.ts`、`knowledge-flow-sharing.test.ts` |
| publication 重放幂等；相同路由在不同输入顺序下产生相同文件内容 | `test/knowledge-flow-pipeline.test.ts`、`knowledge-flow-topic-routes.test.ts` |
| replica 完整性受检验；只有指定 materializer 写共享页；人工编辑发生冲突时被保留 | `extensions/knowledge-flow/test_replica_integrity.py`、`test_shared_materialize.py` |
| 接管先持久让出后发送回执；缺失或延迟的记录不恢复旧权限；一次性更新后归还默认机 | `extensions/knowledge-flow/test_writer_handoff.py`、`test_writer_sync.py` |
| 安装 dry-run 不写入；损坏 runtime 不形成部分安装；现有 hook、队列和证据被保留 | `deployment/test_install.py`、`test_install_events.py` |

覆盖率和测试数量适合发现趋势，不能证明这些不变量成立。本轮不新增一个缺少
业务依据的覆盖率百分比门槛；先要求现有关键行为测试真实通过。400 行文件、
40 行函数的工程规则仍需要 review，Fallow 不等同于这些行数规则的完整检查器。

## 3. 本地复现

从锁文件安装后执行；`origin/personal/stable` 应换为本次 PR 的实际目标：

```bash
npm ci
npx tsc --noEmit
npx tsc --noEmit -p extensions/knowledge-flow/tsconfig.json
npm run typecheck:tests -- --base-ref origin/personal/stable
RELEASE_DOCS_BASE=origin/personal/stable npm run release:check-docs
npm run build
node extensions/knowledge-flow/build.mjs /absolute/temporary/knowledge-flow-runtime
CI=true npm test
npm run test:pack
python3 -m unittest discover -s extensions/knowledge-flow -p 'test_*.py'
python3 -m unittest discover -s deployment -p 'test_*.py'
npm run fallow:ci
```

`CI=true` 同时开启现有 CLI 安装冒烟，因此需要 npm registry 网络。依赖下载或
安装失败也会使 gate 失败；先根据日志区分网络故障与代码缺陷，不吞掉失败。
`RUN_CODEX_LIVE_SMOKE=1` 才允许真实 Codex smoke，常规验证不需要模型密钥或登录态。
pre-push 会清除 Git 为 hook 导出的仓库定位环境变量，再运行完整构建和测试；
临时测试仓库的 Git 操作因此不会误改当前 checkout 的 HEAD 或 index。
pre-commit 与 CI 共用带 `--fail-on-issues` 的 Fallow 命令，警告级 finding 也会失败。

## 4. 两台机器共用的 PR 与合并流程

**顺序固定为：提交并创建 PR → 最终提交的 thermo review → 当前版本 CI 全绿 → 受控合并。**
审查与 CI 可以并行；只有两者都满足才合并。规则保存在仓库，两台机器更新仓库后
使用同一份；本机已有的 agent 规则继续生效，不需要复制机器路径或更改 GitHub 套餐。

### 提交与审查

1. 从最新目标分支建立独立任务分支；本 fork 默认目标为 `personal/stable`，上游为
   `main`。保留另一台机器正在工作的分支，通过各自 PR 集成。
2. 按第 3 节和本次风险完成验证、提交并推送，再创建 PR。PR 正文说明问题、结果、
   实际验证与限制。
3. PR 创建后，对已提交的最终 HEAD 使用 **`thermo-nuclear-code-quality-review`**，
   检查相对目标 base 的完整 PR diff 和必要关联代码。不得只审未提交工作区或最后一个 commit。
4. 在 PR 正文或链接的 review 中留下记录：审查者、完整 HEAD SHA、完整 base SHA、
   结论、发现与处理情况、验证命令及结果。没有阻断问题才能声明可合并；仍未解决的
   P1/P2、关键验收缺失或检查失败都会阻断。
5. 审查后代码有变化，提交修复、重跑受影响检查并复核最终 HEAD；目标分支推进时，
   将新 base 合入 PR 分支，刷新 review 与 CI。不能沿用旧 SHA 的审查结论。

审查记录的最小格式（必须填写真实结果）：

```text
Thermo review
Reviewer: <审查者或 agent>
HEAD: <40 位 SHA>
Base: <40 位 SHA>
Conclusion: <可合并 / 有阻断项>
Findings: <发现、修复情况、剩余问题>
Validation: <实际命令、结果及 CI 链接>
```

### 共用合并入口

需要 Node 24、Git、已登录且有仓库权限的 GitHub CLI（`gh`）。从本仓库 checkout
运行，命令以 **`origin` 指向的仓库**为目标，避免 fork 被 gh 默认解析为 upstream。
先从已完成的审查记录复制两个 SHA；参数表示调用者确认 review 已完成且无阻断项，
**脚本不会生成审查结论，也不会判断 thermo review 的质量**。

```bash
# 11 为示例 PR 号；将两个变量设为审查记录中的完整 SHA。
npm run pr:merge -- 11 \
  --reviewed-head "$REVIEWED_HEAD" --reviewed-base "$REVIEWED_BASE" --dry-run

# 已获得合并授权，且 dry-run 就绪后，以相同参数执行：
npm run pr:merge -- 11 \
  --reviewed-head "$REVIEWED_HEAD" --reviewed-base "$REVIEWED_BASE"
```

命令检查 PR 状态、受审 HEAD/base、分支是否包含最新目标分支，以及当前 HEAD 的
最新自动 PR CI。只有 `.github/workflows/ci.yml` 的最新运行和全部 job 成功，且包含
`CI Gate`，才放行；缺失、等待、失败、取消、跳过或手动运行均不能代替。
`CI Gate` 的 `PR context` step 保存触发时的 PR 编号、HEAD 和 base；命令通过
当前 attempt 的 jobs API 核对这份快照，避免把 PR 的实时 base 当成当时测试的 base。
执行前再次读取 GitHub 状态，并通过 merge API 的 `sha` 参数原子匹配 HEAD，防止另一台机器
在检查后推送新 HEAD。合并保留 merge commit，不删除分支，也不启用 auto-merge。

被拒绝时先处理输出指出的原因：等待当前运行结束、解决真实失败、同步新 base，
或为新 HEAD 补齐 review。检查阶段的网络/API 错误同样阻止发起合并；若最终合并请求
返回 `Merge result unconfirmed`，先去 GitHub 核验 PR 状态，不能假定未合并而直接重试。
可以重跑被确认属于基础设施故障的 CI，但必须在 PR 记录原因并 **Re-run all jobs**。
命令要求当前 attempt 包含
完整的 8 个必需 job，不接受只重跑失败 job 后拼接旧结果，也不能拿历史绿灯替代。
修改 CI 矩阵时同步 `scripts/merge-pr.ts` 中的必需 job 列表；契约测试会核对两者。
禁止绕过脚本直接点 Merge、直接 push 稳定分支或使用 `--admin`。

**能力边界：** 当前套餐下这是一项仓库协作规则和本地检查，无法禁止有写权限的人
绕过入口。GitHub 合并接口只能原子校验 HEAD，不能同时锁定预期 base；最终读取与
合并之间仍有短暂的 base 竞争窗口。两台机器避免同时合并到同一分支；若合并后发现
base 在窗口内变化，应检查实际合并结果和稳定分支 push CI，不能声称受审组合原封不动。
接口约束见 [GitHub 合并 PR API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)。

### 可选的 GitHub 服务器端强制拦截

GitHub 是否允许配置服务器端分支保护，取决于仓库可见性、当前套餐和账号权限。
配置前应在仓库设置中核验可用规则；若当前套餐不支持目标仓库的保护能力，工作流配置本身
不能被描述为“GitHub 已禁止红灯合并”。现有 Actions 与上述共用命令仍可作为协作门禁；
如需服务器端强制执行，应选择支持目标仓库分支保护的套餐或仓库设置。

工作流进入仓库并实际跑绿、套餐支持后，为 `personal/stable` 配置：

1. 要求通过 Pull Request 合并。
2. Required status checks 选择精确名称 **`CI Gate`**，来源选择 GitHub Actions。
3. 要求合并前与目标分支保持最新，避免只检查过时的代码组合。
4. 对管理员同样生效，禁止绕过；关闭 force push 和分支删除。
5. 如 `main` 也接受维护性合并，对它应用同一规则。审批人数按实际协作方式决定。

验证：用失败检查的 PR 确认合并入口被禁用，再恢复为成功检查确认门禁放行。
这项验证不需要真的合并 PR。更改 workflow 或 gate 名称时同步分支规则。
分支保护可叠加在本流程之上，仍需完成 review；本地命令不能替代服务器端保护。

官方说明：[受保护分支](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)、
[必需状态检查与跳过 job 的处理](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)。

## 5. 发布验收与后续衡量

PR CI 不证明真实模型语义、Codex 原生 hook 信任、launchd 后台权限、两台 Mac 的
iCloud 延迟或 Obsidian 最终展示正确。这些按 [部署指南](deployment/README.md)
在真实设备上验收，记录真实事件与结果，不能拿空队列或 mock 成功替代。

运行一段时间后跟踪：CI 总时长的 P95、相同提交重跑结果不一致的比例、每 PR 的
runner 分钟数、合并后回归缺陷。初期以 **P95 不超过 15 分钟、无未处理的不稳定
必需测试**为目标；这不是已测得的成绩，job 超时仍留有余量。macOS 费用较高，
以真实运行量评估矩阵，而不通过忽略失败节约成本。

依赖漏洞扫描、自动依赖更新、真实模型评测可另设定期检查。把漏洞门槛变成 PR
硬门禁前，先确认存量告警、影响范围和处理责任，避免未分流的旧告警长期堵住全部改动。
