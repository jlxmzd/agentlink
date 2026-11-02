# AgentLink 1.0.0

采用 [Apache License 2.0](LICENSE)。本发行版为源码包，不含 EXE；Python 后端仅依赖标准库。

## 环境要求

- Python 3.8+，Git（任务改动范围检查需要）。
- 自行安装需要连接的 Claude Code、Codex 或 DeepSeek Harness，使用自己的账户。本项目不包含宿主、模型或凭据。
- Windows 使用 BAT，Linux/macOS 提供 SH。本次发布在 Windows 验证，跨平台 CI 配置随附。宿主插件 API 变更可能需要适配。
- 首次启动在面板添加项目、选择会话即可生成配置，无需复制个人配置。移动本工具目录后，请重新安装宿主插件以更新路径。

Claude Code · Codex · DeepSeek（dsh）三方协作的独立工具。由 MLUQOMS 的 `scripts/tri` 总线演化而来，去掉了项目绑定：任何目录都能成为协作项目。

## 使用

```bash
python agentlink.py panel
```

Windows 可以直接双击项目根目录的 `start_agentlink.bat`；Linux / macOS 用 `./start_agentlink.sh`（没有执行权限时先 `chmod +x start_agentlink.sh`，也可以直接 `sh start_agentlink.sh`）。两个脚本都从自身所在目录启动，不依赖当前工作目录，参数原样传给面板（例如 `--no-browser`），路径含空格或中文可用。Python（需要 3.8+）或入口文件缺失时给出中文提示并以非零码退出：Windows 保留窗口等待，Linux/macOS 在交互终端里等一次回车（有 zenity/xmessage 时改为弹窗，`AGENTLINK_NO_PAUSE=1` 可跳过）。提交到仓库时请保留执行位，例如 `git add --chmod=+x start_agentlink.sh`。

浏览器打开 `http://127.0.0.1:8765/`（只监听本机，页面带一次性令牌）。

1. 「宿主插件」页：勾选并安装三个宿主的插件（全局一次，改动前自动备份），之后在各宿主空闲时重启。Codex 还需运行 `codex plugin add agentlink@agentlink` 并信任钩子。
2. 左侧添加项目目录。
3. 「绑定」页：四张紧凑卡片——Codex、Claude Code、DeepSeek 执行、DeepSeek 验收。卡片默认只显示角色名、已选对话标题和状态（ID 缩短显示，完整值在「高级」里），点「选择对话」才展开可搜索的会话列表；「确认绑定」在页面顶部。「协作设置」（项目名称、总控、集成人、协作文件开关）默认折叠。前三张各选一个对话（或不绑定）并填写分工；第四张「DeepSeek 验收」从当前项目的 DeepSeek 对话里单独选一个作为 AI 审查对话（不能与执行对话相同），并可勾选是否启用。
4. 点「确认绑定并生成协作文件」，项目目录里会写入：
   - `AGENTLINK.md`：参与方、绑定会话、分级、命令与规则
   - `AGENTS.md` / `CLAUDE.md`：`<!-- agentlink:start -->` 托管段（原有内容保留）
   - `.agentlink/config.json`：绑定配置；`.agentlink/bus/`：任务总线状态（已加入 `.gitignore`）
5. 「任务」页：以“你”的身份发任务、验收或驳回、转交，查看事件，开关自动唤醒。
6. 「设置 / 迁移」页：调整程序自动验收级别、导出/导入迁移包。AI 验收对话在绑定页的第四张卡片上选择并随绑定一起保存，设置页不会覆盖它。

默认分工：Codex 为总控（整体设计与方向、拆分路由、主要功能、集成），DeepSeek 为主力执行（全部简单任务，可调度子智能体；中难任务按指引实现），Claude 负责具体代码实现与疑难代码。交给 DeepSeek 的 L2/L3 任务必须附实现指引；DeepSeek 返工两次未解决时，代码问题自动转 Claude、设计问题转 Codex 整改。分工、总控和集成人都可以在绑定页修改。

程序验收：派发任务时写验收命令（`--check`，面板里的「验收命令」），交付时由程序检查改动范围并运行命令。失败直接退回执行方；DeepSeek 的 L0/L1 通过即自动验收，不唤醒 Codex / Claude。命令里的通配符（如 `python -m py_compile agentlink/*.py`）由程序展开，cmd 里也能用。

DeepSeek 会话列表：以 Harness 工作区成员为依据，保留显式收录的分支对话，排除未收录的内部子对话和已归档对话。成员切换工作目录不会导致其从所属工作区消失；“显示所有目录的对话”会保留其他工作区的可见分支。按最近活动排序、按 ID 去重，不设 40 条截断。工作区元数据读取失败时明确报错。

DeepSeek AI 验收（可选）：在「绑定」页的第四张卡片「DeepSeek 验收」里选一个独立的 DeepSeek 审查对话（与执行对话不同）并启用后，程序验收通过的 DeepSeek 实现任务（L1 及以上）先进入 `ai_review`，由 Harness 向该对话投递一次审查提示（含任务 ID、规格、允许路径、程序验收结果，并要求只审查、不改文件）。审查对话用带一次性令牌的命令回传结论：

```text
python <AgentLink>/agentlink.py reviewer --as deepseek --task <ID> --attempt <N> --verdict accept|reject --token <令牌> [--note "具体问题"]
```

通过后任务回到 `delivered` 由发起方最终验收；驳回则回到 DeepSeek `rework` 并附具体意见（再次交付会重新跑程序验收和 AI 审查）。投递失败、会话缺失或令牌不对时任务停在 `ai_review` 并在面板标为「未送达 / 投递不确定」，可用「重试 AI 审查」或 `dispatch <ID>` 重发；不会静默通过。未开启 AI 验收时行为与原来完全一致。

迁移：设置页可导出迁移包（JSON，含项目名称、分工、选项、协作文件开关，可选会话元数据；不含任务正文、回执、事件、本机路径或凭据）。导入时保留当前项目目录和任务总线；会话 ID 与本机相关，只有当前能看到的会话才会直接沿用，其余标记为「待重新绑定」，未知字段忽略，版本不符会明确报错。导入的 AI 验收会话同样只在本机可见时沿用，否则关闭 AI 审查并提示重新选择。

只有绑定的对话会收到任务提示、被自动唤醒。Claude 插件每 30 秒检查一次，Codex 通过 `codex queue` 唤醒，DeepSeek 由 Harness 插件投递。

## 省 token 的策略

Claude 与 Codex 的额度最贵、DeepSeek 便宜，所以附加上下文按身份和时机分档，DeepSeek 侧的提示保持完整以便少返工：

- 会话启动只给「一行一个待办」（ID、状态、标题、下一步命令），开工前用 `show` 读真正要接的那条；只有进行中的一轮才附完整规格、回执和 diff。
- DeepSeek 的 L0 机械回执只给一行 + `accept` 提示，不带程序验收块和 diff。
- 超过约 600 字符的 diff 不内联，只给改动统计和一条精确到文件的 `git diff HEAD -- <文件>` 命令。
- `list` 默认短行（ID/状态/标题），加 `--full` 才显示分级与方向。
- 同一件事只唤醒一次、连续变化合并成一次唤醒；进展事件不唤醒任何人；已读过的项不再重复出现，无事可做时钩子不输出任何内容。等待用 `wait`，不轮询。

## 代理侧命令

```text
python <AgentLink>/agentlink.py --as claude|codex|deepseek list [--all] [--full] | locks | post | show | claim | deliver | block | accept | reject | reassign | reviewer | wait
```

在项目目录（或子目录）里运行即可，项目由当前目录自动识别，也可以用 `--project <根目录>` 指定。

## 结构

| 路径 | 作用 |
| --- | --- |
| `agentlink/bus.py` | 任务、路径锁、状态机、事件游标、唤醒、DeepSeek 投递 |
| `agentlink/project.py` | 项目注册表（`~/.agentlink/projects.json`）、绑定、协作文件生成 |
| `agentlink/sessions.py` | 读取三方对话列表（只读元数据：ID、标题、目录、时间） |
| `agentlink/server.py`、`agentlink/web/` | 管理面板 |
| `agentlink/install.py` | 宿主插件安装与卸载 |
| `hosts/claude/agentlink/` | Claude Code 插件（`/agentlink` 命令、上下文、自唤醒） |
| `hosts/codex/` | Codex 插件与 skill，市场 `hosts/.agents/plugins/marketplace.json` |
| `hosts/harness/agentlink-bridge.mjs` | DeepSeek Harness 插件：发布本机回环地址，每 15 秒 `pump-all` |

## 测试

```bash
python -I tests/test_agentlink.py
python -I tests/test_harness.py
```

使用临时家目录和临时项目，不读写真实配置，不投递、不唤醒；Harness 侧用假客户端覆盖投递与 AI 审查流程。


## 贡献与安全

参见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md)、[CHANGELOG.md](CHANGELOG.md) 和 [THIRD_PARTY.md](THIRD_PARTY.md)。个人绑定、任务总线和运行日志不应提交到公共仓库。
