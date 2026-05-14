# Mai-Skill-Loader

为MaiBot（MaiSaka）提供标准的[Agent Skills](https://agentskills.io)兼容支持，允许独立配置skill调用的LLM以获得更好的效果

## 快速开始

### 1. 安装插件

把本仓库克隆到 MaiBot 的 `plugins/` 目录：

```bash
cd your-maibot/plugins
git clone https://github.com/CharTyr/Mai-Skill-Loader.git skill_loader
```

重启 MaiBot，插件会自动加载。

### 2. 安装 Skill

**方式一：用 npx 命令安装（推荐）**

```bash
cd plugins/skill_loader/skills
npx skills add https://github.com/vercel-labs/skills --skill find-skills -y
```

安装完成后使用 `/skill reload` 让 bot 加载新 skill。

**方式二：手动添加**

在 `plugins/skill_loader/skills/` 下创建文件夹，放入 `SKILL.md` 即可：

```
plugins/skill_loader/skills/
└── my-skill/
    └── SKILL.md
```

### 3. 使用

安装好的 skill 会自动注册为 bot 的工具。当用户的对话匹配到 skill 的描述时，bot 会自动调用它。

skill 的执行结果会直接发送到聊天中，不需要额外等待。

## 管理命令

在聊天中发送以下命令管理 skill：

| 命令 | 说明 |
|------|------|
| `/skill list` | 查看已加载的所有 skill |
| `/skill caps` | 查看能力权限状态 |
| `/skill enable bash` | 开启 bash 能力 |
| `/skill enable all` | 开启所有能力 |
| `/skill disable python` | 关闭 python 能力 |
| `/skill reload` | 重新加载 skill（添加新 skill 后使用） |

## 能力权限

部分 skill 需要特定能力才能工作（比如执行命令、读写文件）。出于安全考虑，默认只开启了安全的能力：

| 能力 | 默认 | 说明 |
|------|------|------|
| `read_file` | 开启 | 读取文件 |
| `http` | 开启 | 发起网络请求 |
| `bash` | 关闭 | 执行 shell 命令 |
| `python` | 关闭 | 执行 Python 代码 |
| `write_file` | 关闭 | 写入文件 |

当 skill 需要的能力未开启时，bot 会直接告诉你需要执行什么命令来开启。

## 编写自己的 Skill

创建一个文件夹，写一个 `SKILL.md`，就是一个 skill：

```
my-skill/
├── SKILL.md          # 必须：描述和指令
├── scripts/          # 可选：脚本
├── references/       # 可选：参考文档
└── assets/           # 可选：资源文件
```

### SKILL.md 格式

```markdown
---
name: my-skill
description: 简要描述这个 skill 做什么，以及什么时候应该使用它。
allowed-tools: Bash Read
metadata:
  maibot-mode: agent
  maibot-max-turns: "10"
---

这里写给 AI 的详细指令。
告诉它具体怎么完成任务、注意什么、输出什么格式。
```

### 标准字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 小写字母、数字和连字符，需与文件夹名一致 |
| `description` | 是 | 描述功能和触发条件，bot 根据这个决定何时调用 |
| `allowed-tools` | 否 | 需要的能力，如 `Bash Read Write Http Python` |
| `license` | 否 | 许可证 |
| `compatibility` | 否 | 环境要求 |
| `metadata` | 否 | 扩展字段 |

### MaiBot 扩展字段（放在 metadata 里）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `maibot-mode` | `agent` | `agent`=独立 AI 执行，`direct`=直接运行脚本 |
| `maibot-model` | 系统默认 | 指定使用的模型 |
| `maibot-max-turns` | `10` | agent 模式最大对话轮数 |

### 两种模式

**agent 模式**（默认）：skill 的指令会交给一个独立的 AI 来执行，它可以使用 allowed-tools 中声明的能力多轮完成任务。适合复杂任务。

**direct 模式**：直接运行 `scripts/` 目录下的第一个 Python 脚本的 `run()` 函数，适合简单的确定性任务。

```python
# scripts/my_script.py
async def run(task: str) -> str:
    return f"处理完成: {task}"
```

## 兼容性

本插件完全兼容 [Agent Skills 规范](https://agentskills.io/specification)。从 VS Code Copilot、Claude Code、Codex 等平台获取的标准 skill 可以直接使用，无需任何修改。

## 配置

插件配置通过 MaiBot 的插件配置系统管理，可在 WebUI 中修改：

- 默认模型、最大轮数、超时时间
- 各项能力的开关和安全策略（命令黑名单、目录白名单等）

## 许可证

MIT
