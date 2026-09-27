# 切换到公开源码仓库

源码仓库：[mtmtian/llm-wiki-compiler-public](https://github.com/mtmtian/llm-wiki-compiler-public)。
维护与 PR 目标分支为 `personal/stable`。各设备使用独立 checkout 和各自的私有配置。

## 取得干净源码

```sh
git clone --branch personal/stable \
  https://github.com/mtmtian/llm-wiki-compiler-public.git
cd llm-wiki-compiler-public
git remote -v
git status --short
git rev-parse HEAD
git remote add upstream https://github.com/atomicstrata/llm-wiki-compiler.git
```

这是不含私有 Git 历史的独立快照。保留旧 checkout 供私有追溯；直接替换旧 checkout 的
origin、合并旧分支或推送旧 tags 都可能把私有历史带入公开仓库。通过新 clone 开始后续工作。
尚未提交的旧工作先逐项审查，再以文件改动或脱敏 patch 迁入新分支，按正常 PR 流程集成。

## 更新本机私有映射

先备份本机 `~/.config/llmwiki/knowledge-flow.json` 和 `machine.json`。仅调整源码身份与路径：

1. 找到既有编译器项目条目，保留原项目 ID。在该条目的 `repos` 中加入
   `mtmtian/llm-wiki-compiler-public`；为兼容历史会话，可以保留旧仓库别名。
2. 若该项目有源码 `paths`、`machine.json` 的 `projectPaths` 或维护检查的 `cwd`，
   将对应源码路径改为本机的新 checkout。其他业务项目不随此次切换修改。
3. 更新本机运维说明中的源码入口。机器身份、Wiki 根、角色、预算、共享参与者、
   materializer、队列和凭据继续使用本机原值。
4. 核对新 checkout 能解析为原编译器项目，其他项目映射没有变化。

实际配置、运维备份及迁移记录均留在仓库外。提交的 `deployment/knowledge-flow.json`
是中性模板，不能用它覆盖已经运行的私有配置。

## 运行环境与后续部署

只切换源码仓库不需要重新安装当前 runtime，也不会迁移 Wiki 或共享写入所有权。
现役 MCP、hooks 和 worker 可以继续使用原不可变运行包。

后续部署从新仓库已经通过 CI 并合并的提交构建，按[安装指南](README.md)执行。
安装时显式使用本机的完整私有 `--config-source`，沿用原 machine ID、角色以及
`--event-driven` 选择。先核对 dry-run，再进行实际安装及原生 hooks 验收。
运行包升级和另一台设备验收应分别记录，clone 成功不代表它们已经完成。

新提交通过 PR 集成，审查与 CI 门禁沿用 [CI.md](../CI.md)。首次快照提交仅用于建立
比较基准；它的 `[skip ci]` 不作为验收证据，后续自动 PR CI 会对完整源码执行全部必需检查。
