---
name: mac-storage-inspector
description: "Investigate storage usage on a Mac, distinguish APFS volume usage from file allocation and scan gaps, and create local scripts tailored to the current machine. Use for macOS disk-space audits and cleanup planning; inspection does not itself authorize deletion."
---

# Mac 存储空间检查

用通用元数据扫描器收集证据，再为当前电脑编写本地定制调查脚本。不要将上一台电脑的目录、软件名单、磁盘编号或清理决定当作默认配置。

## 开始检查

将 `SKILL_DIR` 设为本文件所在目录的绝对路径。需要 Python 3.9+、现代 macOS 的 APFS 启动 Data 卷；首次构建原生扫描器需要已安装的 Apple Command Line Tools。只分发 C 源码，在目标电脑本机编译，不携带另一架构的二进制。

```sh
python3 "$SKILL_DIR/scripts/storage_inspector.py" doctor
python3 "$SKILL_DIR/scripts/storage_inspector.py" build
python3 "$SKILL_DIR/scripts/storage_inspector.py" collect --path "$HOME" --seconds 30 --budget 120
python3 "$SKILL_DIR/scripts/storage_inspector.py" query --limit 15
```

`doctor` 动态查询启动 APFS 容器及其后端存储，不假定容量、用户名或固定磁盘编号。依赖缺失时说明缺失项，由用户选择安装途径，不自动安装工具链。HFS+、旧系统或外接卷不在原生扫描器的支持范围；保留预检证据，说明限制并选择适合该机器的只读检查方法。

默认数据目录是当前用户的 `~/Library/Application Support/MacStorageInspector/`，可用全局参数 `--state-dir` 改到其他私有目录；它必须位于 Skill 目录之外。报告包含 `analysis.json`、`report.html`、`summary.txt` 和 `raw/` 原始证据。`--budget` 限制目录遍历时间；预检、编译及卷信息查询另有超时。按 Control-C 会停止当前扫描并保留已完成的证据。

先按已知问题选择范围，默认检查当前用户目录。需要覆盖启动 Data 卷时显式指定 `/System/Volumes/Data`；需要补查时只重新扫描对应路径。`query --report REPORT_DIR --path ABSOLUTE_PATH --limit 15` 查询现有证据，避免反复整盘扫描。每份报告独立保存，不把新旧扫描混称为同一次完整扫描。

## 为这台电脑生成定制脚本

初次检查后读取 [定制脚本工作流](references/customization.md)，据当前证据完成定制，而不是仅交付空模板：

1. 确认大目录归属、当前用途、开发项目或应用状态和访问缺口。
2. 选择 Skill 和项目仓库之外的私有案例目录，用 `make-local --destination ABSOLUTE_LOCAL_DIR` 生成 `profile.json`、`investigate_local.py` 和忽略规则。
3. 将实际发现的目标目录、项目根目录、保护路径和日志目录填入本地配置；根据问题补充范围明确的只读检查代码。不要将配置或新增的机器专用逻辑写回共享 Skill。
4. 运行本地脚本的 `plan`，核对目标和范围；再运行有期限的 `scan`，验证它能产出报告。说明证据怎样支持这些定制选择。

生成脚本默认只输出计划；`scan` 读取元数据。JSON 配置只保存路径和参数，不能成为 shell 命令。只在发现相应项目之后配置 `project_roots`，不能仅凭目录叫 `target`、`build` 或 `dist` 就认定它可清理。`protected_paths` 优先于其他分类。

## 权限和数据边界

- 检查工具没有删除入口，不读取被检查文件的正文，不计算文件内容哈希，不上传报告。原生扫描不跟随符号链接、不跨文件系统；云端 DATALESS 目录只记录元数据及覆盖缺口。
- 协调器以普通用户运行。确需管理员补查且已有相应授权时，由用户在自己的 Terminal 运行 `sudo -v`，再在同一终端使用 `collect --elevated`。只有固定参数的原生元数据扫描器获得提升；不要请求用户把密码发给 Agent。
- sudo 和“完全磁盘访问”相互独立。权限拒绝、超时、云端目录和挂载边界均为未知范围，不等于零占用。不要自动反复提权、关闭 SIP 或安装后台服务；一次补查仍被拒绝时保留缺口并解释。
- 定制配置、脚本、报告和历史清理记录只保存在本机，不放入共享 Skill、发布压缩包或远程仓库。生成目录带有 `.gitignore`；发布前仍须检查实际归档成员，忽略规则不能替代内容核对。

检查请求本身不授权删除、卸载或停止进程。如果用户另有明确清理授权，沿用其实际范围，将具体执行脚本另存本地，先核实进程占用、项目改动、可重建性和保留项，再执行必要的预检与验证。不要套用其他电脑或历史日期的一次性清理脚本。缺少必要授权时才向用户询问。

## 解释与交付

分开报告容器可用空间、各卷占用、文件分配块、逻辑大小和访问覆盖。硬链接去重；APFS 克隆共享块、快照、元数据及采样时序仍会造成差额。候选目录占用不是保证可回收量，同大小文件只是重复候选，未经内容验证。

向用户提供主要占用、实际完成的检查、剩余未知范围，以及已生成的本地定制脚本位置。查看生成的 HTML 报告，核对大目录、覆盖提示及交互，不只确认命令退出成功。不能把单机测试说成已完成 Intel、Apple Silicon 和所有 macOS 版本的跨机验证。

## 分发与验证

可分享的内容仅限本 Skill 的 `SKILL.md`、`agents/`、`scripts/`、`assets/`、`references/` 和 `.gitignore`。禁止混入运行生成的数据。将完整 `mac-storage-inspector/` 文件夹复制到目标 Agent 的 skills 目录；Codex 默认位置为 `${CODEX_HOME:-$HOME/.codex}/skills/`。

在 Mac 上可运行 `python3 "$SKILL_DIR/scripts/test_storage_inspector.py"` 验证扫描器的硬链接、稀疏文件、权限缺口、路径边界和报告转义。测试在临时目录构造数据，不扫描整台电脑，不需要 sudo。
