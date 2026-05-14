"""Skill Loader v2 — Agent Skills 适配器

架构：
- 覆写 get_components() 动态返回 skill tools
- 覆写 invoke_component() 统一分发 skill 调用
- Agent loop 带 token budget 和 context 截断
- Capabilities 带安全限制
- /skill reload 热加载
- /skill enable|disable 运行时开关

TODO:
- 多轮追问：维护 stream_id + skill_name 的短期会话缓存，同一 skill 短时间内再次调用时延续上下文
- 聊天上下文注入：从 kwargs 中提取 Maisaka 的近期聊天记录，注入 skill agent 的 user message 作为背景
"""

from typing import Any, Dict, List, Optional
from pathlib import Path

import asyncio
import contextlib
import importlib.util
import io
import json
import logging
import re
import sys
import time
import traceback
from urllib.parse import urlparse

import yaml

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ToolParameterInfo, ToolParamType

logger = logging.getLogger("skill_loader")

# ====== 配置 ======


class CapabilitiesConfig(PluginConfigBase):
    """能力权限配置。"""
    __ui_label__ = "能力权限"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    allow_bash: bool = Field(default=False, description="允许执行 shell 命令")
    allow_python: bool = Field(default=False, description="允许执行 Python 代码")
    allow_read_file: bool = Field(default=True, description="允许读取文件")
    allow_write_file: bool = Field(default=False, description="允许写入文件")
    allow_http: bool = Field(default=True, description="允许 HTTP 请求")
    bash_working_dir: str = Field(default="", description="bash 工作目录（空=插件目录）")
    bash_timeout: int = Field(default=30, description="bash 命令超时（秒）")
    bash_blocked_commands: List[str] = Field(
        default_factory=lambda: ["rm -rf /", "mkfs", "dd if=", "shutdown", "reboot"],
        description="禁止的命令模式",
    )
    read_file_allowed_dirs: List[str] = Field(default_factory=list, description="读取目录白名单（空=不限）")
    write_file_allowed_dirs: List[str] = Field(default_factory=list, description="写入目录白名单（空=不限）")
    write_file_max_size_kb: int = Field(default=1024, description="写入文件最大 KB")
    http_allowed_domains: List[str] = Field(default_factory=list, description="HTTP 域名白名单（空=不限）")
    http_timeout: int = Field(default=30, description="HTTP 超时（秒）")


class SkillLoaderConfig(PluginConfigBase):
    """Skill Loader 主配置。"""
    __ui_label__ = "Skill Loader"
    __ui_icon__ = "zap"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用")
    config_version: str = Field(default="1.0.0", description="配置版本")
    skills_dir: str = Field(default="skills", description="skills 目录路径")
    default_model: str = Field(default="", description="agent 默认模型（空=系统默认）")
    default_max_turns: int = Field(default=10, description="agent 默认最大轮数")
    timeout_seconds: int = Field(default=60, description="skill 调用超时（秒）")
    agent_max_context_tokens: int = Field(default=8000, description="agent 上下文 token 预算")
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)


# ====== Skill 定义与解析 ======

# allowed-tools 到 maibot capabilities 的映射
TOOLS_TO_CAPS: Dict[str, str] = {
    "bash": "bash", "Bash": "bash",
    "read": "read_file", "Read": "read_file", "read_file": "read_file",
    "write": "write_file", "Write": "write_file", "write_file": "write_file",
    "http": "http", "Http": "http", "WebFetch": "http", "web_fetch": "http",
    "python": "python", "Python": "python",
}

# name 格式校验正则：小写字母、数字、连字符，不能以连字符开头/结尾，不能连续连字符
_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CONSECUTIVE_HYPHENS = re.compile(r"--")


def _validate_name(name: str, dir_name: str) -> Optional[str]:
    """校验 skill name 是否符合规范，返回错误信息或 None。"""
    if not name:
        return "name 不能为空"
    if len(name) > 64:
        return f"name 超过 64 字符 ({len(name)})"
    if not _NAME_PATTERN.match(name):
        return f"name '{name}' 格式不合法（只允许小写字母、数字、连字符）"
    if _CONSECUTIVE_HYPHENS.search(name):
        return f"name '{name}' 包含连续连字符"
    if name != dir_name:
        return f"name '{name}' 与目录名 '{dir_name}' 不匹配"
    return None


def _parse_allowed_tools(allowed_tools: str) -> List[str]:
    """解析 allowed-tools 字段为 capabilities 列表。
    
    支持规范格式如 'Bash(git:*) Read' 和简写格式如 'bash read_file'。
    """
    if not allowed_tools:
        return []
    caps: List[str] = []
    # 按空格分割，去掉括号内的参数
    for token in allowed_tools.split():
        base_tool = token.split("(")[0]
        cap = TOOLS_TO_CAPS.get(base_tool)
        if cap and cap not in caps:
            caps.append(cap)
    return caps


class SkillDefinition:
    """解析后的 Skill（符合 Agent Skills 规范 + maibot 扩展）。"""
    __slots__ = (
        "name", "description", "mode", "model", "max_turns",
        "instructions", "scripts", "skill_path", "capabilities",
        "license", "compatibility", "metadata", "references_dir", "assets_dir",
    )

    def __init__(self, *, name: str, description: str, mode: str, model: str,
                 max_turns: int, instructions: str, scripts: Dict[str, Path],
                 skill_path: Path, capabilities: List[str],
                 license: str = "", compatibility: str = "",
                 metadata: Optional[Dict[str, str]] = None,
                 references_dir: Optional[Path] = None,
                 assets_dir: Optional[Path] = None):
        self.name = name
        self.description = description
        self.mode = mode
        self.model = model
        self.max_turns = max_turns
        self.instructions = instructions
        self.scripts = scripts
        self.skill_path = skill_path
        self.capabilities = capabilities
        self.license = license
        self.compatibility = compatibility
        self.metadata = metadata or {}
        self.references_dir = references_dir
        self.assets_dir = assets_dir


def parse_skill(skill_path: Path) -> Optional[SkillDefinition]:
    """解析单个 skill 目录（符合 Agent Skills 规范）。"""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
    frontmatter: Dict[str, Any] = {}
    instructions = content

    # 解析 YAML frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as e:
                logger.warning(f"SKILL.md 解析失败 ({skill_path.name}): {e}")
                return None
            instructions = parts[2].strip()

    # 标准字段
    name = str(frontmatter.get("name", skill_path.name)).strip()
    description = str(frontmatter.get("description", "")).strip()

    # name 校验
    name_error = _validate_name(name, skill_path.name)
    if name_error:
        logger.warning(f"Skill '{skill_path.name}' 跳过: {name_error}")
        return None

    # description 校验
    if not description:
        logger.warning(f"Skill '{name}' 缺少 description，跳过")
        return None
    if len(description) > 1024:
        logger.warning(f"Skill '{name}' description 超过 1024 字符，已截断")
        description = description[:1024]

    # 可选标准字段
    license_field = str(frontmatter.get("license", "")).strip()
    compatibility = str(frontmatter.get("compatibility", "")).strip()
    if len(compatibility) > 500:
        compatibility = compatibility[:500]
    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # allowed-tools → capabilities
    allowed_tools = str(frontmatter.get("allowed-tools", "")).strip()
    capabilities = _parse_allowed_tools(allowed_tools)

    # maibot 扩展（从 metadata 读取）
    mode = str(metadata.get("maibot-mode", "agent")).strip()
    if mode not in ("direct", "agent"):
        mode = "agent"
    model = str(metadata.get("maibot-model", "")).strip()
    max_turns = int(metadata.get("maibot-max-turns", 10))

    # scripts/ 目录
    scripts: Dict[str, Path] = {}
    scripts_dir = skill_path / "scripts"
    if scripts_dir.exists():
        for f in scripts_dir.glob("*.py"):
            scripts[f.stem] = f

    # references/ 和 assets/ 目录
    references_dir = skill_path / "references"
    assets_dir = skill_path / "assets"

    return SkillDefinition(
        name=name, description=description, mode=mode, model=model,
        max_turns=max_turns, instructions=instructions, scripts=scripts,
        skill_path=skill_path, capabilities=capabilities,
        license=license_field, compatibility=compatibility,
        metadata=metadata,
        references_dir=references_dir if references_dir.exists() else None,
        assets_dir=assets_dir if assets_dir.exists() else None,
    )


def scan_skills(skills_dir: Path) -> Dict[str, SkillDefinition]:
    """扫描目录返回 {name: SkillDefinition}。
    
    支持多种布局：
    1. 直接子目录: skills_dir/<skill-name>/SKILL.md
    2. .agents/skills 标准路径: skills_dir/.agents/skills/<skill-name>/SKILL.md
    3. 项目根 .agents/skills: 向上查找项目根目录的 .agents/skills/
    """
    result: Dict[str, SkillDefinition] = {}
    if not skills_dir.exists():
        return result

    # 扫描直接子目录
    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(("_", ".")):
            continue
        skill = parse_skill(item)
        if skill:
            result[skill.name] = skill

    # 扫描 .agents/skills/ 标准路径（npx skills add 在 skills_dir 下执行时的安装位置）
    agents_skills_dir = skills_dir / ".agents" / "skills"
    if agents_skills_dir.exists():
        for item in sorted(agents_skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith(("_", ".")):
                continue
            skill = parse_skill(item)
            if skill and skill.name not in result:
                result[skill.name] = skill

    # 扫描项目根目录的 .agents/skills/（npx skills add 在项目根执行时的安装位置）
    # 从 skills_dir 向上找到包含 bot.py 或 pyproject.toml 的目录
    project_root = skills_dir.parent.parent  # plugins/skill_loader/skills → plugins/skill_loader → plugins → project_root
    # 再上一级到真正的项目根
    if not (project_root / "bot.py").exists():
        project_root = project_root.parent
    root_agents_dir = project_root / ".agents" / "skills"
    if root_agents_dir.exists() and root_agents_dir != agents_skills_dir:
        for item in sorted(root_agents_dir.iterdir()):
            if not item.is_dir() or item.name.startswith(("_", ".")):
                continue
            skill = parse_skill(item)
            if skill and skill.name not in result:
                result[skill.name] = skill

    return result


# ====== Capabilities 执行器 ======

CAPABILITY_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "bash": {"type": "function", "function": {"name": "bash", "description": "执行 shell 命令", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "shell 命令"}}, "required": ["command"]}}},
    "read_file": {"type": "function", "function": {"name": "read_file", "description": "读取文件内容", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}, "max_lines": {"type": "integer", "description": "最大行数，默认200"}}, "required": ["path"]}}},
    "write_file": {"type": "function", "function": {"name": "write_file", "description": "写入文件", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}, "content": {"type": "string", "description": "内容"}}, "required": ["path", "content"]}}},
    "http": {"type": "function", "function": {"name": "http", "description": "发起 HTTP 请求", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "URL"}, "method": {"type": "string", "description": "GET/POST/PUT/DELETE"}, "body": {"type": "string", "description": "请求体"}}, "required": ["url"]}}},
    "python": {"type": "function", "function": {"name": "python", "description": "执行 Python 代码并返回 stdout", "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "Python 代码"}}, "required": ["code"]}}},
}


def get_allowed_caps(skill: SkillDefinition, cap_cfg: CapabilitiesConfig) -> List[str]:
    """返回 skill 实际被允许的 capabilities。"""
    perm = {"bash": cap_cfg.allow_bash, "read_file": cap_cfg.allow_read_file,
            "write_file": cap_cfg.allow_write_file, "http": cap_cfg.allow_http,
            "python": cap_cfg.allow_python}
    return [c for c in skill.capabilities if perm.get(c, False)]


async def run_capability(name: str, args: Dict[str, Any], cfg: CapabilitiesConfig) -> str:
    """执行单个 capability tool。"""
    if name == "bash":
        return await _cap_bash(args.get("command", ""), cfg)
    elif name == "read_file":
        return await _cap_read_file(args.get("path", ""), cfg, int(args.get("max_lines", 200)))
    elif name == "write_file":
        return await _cap_write_file(args.get("path", ""), args.get("content", ""), cfg)
    elif name == "http":
        return await _cap_http(args.get("url", ""), cfg, args.get("method", "GET"), args.get("body", ""))
    elif name == "python":
        return await _cap_python(args.get("code", ""), cfg)
    return f"未知 capability: {name}"


async def _cap_bash(command: str, cfg: CapabilitiesConfig) -> str:
    for blocked in cfg.bash_blocked_commands:
        if blocked in command:
            return f"安全策略阻止: 命令包含 '{blocked}'"
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cfg.bash_working_dir or None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cfg.bash_timeout)
        out = stdout.decode("utf-8", errors="replace")[:20000]
        err = stderr.decode("utf-8", errors="replace")[:5000]
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        parts.append(f"[exit={proc.returncode}]")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"命令超时 ({cfg.bash_timeout}s)"
    except Exception as e:
        return f"执行失败: {e}"


async def _cap_read_file(path: str, cfg: CapabilitiesConfig, max_lines: int = 200) -> str:
    fp = Path(path).resolve()
    if cfg.read_file_allowed_dirs:
        allowed_roots = [Path(d).resolve() for d in cfg.read_file_allowed_dirs]
        if not any(fp == root or root in fp.parents for root in allowed_roots):
            return f"安全策略阻止: {path} 不在白名单目录中"
    if fp.is_symlink():
        return f"安全策略阻止: 不允许读取符号链接"
    if not fp.exists():
        return f"文件不存在: {path}"
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... (截断，共 {len(lines)} 行)"
        return "\n".join(lines)
    except Exception as e:
        return f"读取失败: {e}"


async def _cap_write_file(path: str, content: str, cfg: CapabilitiesConfig) -> str:
    fp = Path(path).resolve()
    if cfg.write_file_allowed_dirs:
        allowed_roots = [Path(d).resolve() for d in cfg.write_file_allowed_dirs]
        if not any(fp == root or root in fp.parents for root in allowed_roots):
            return f"安全策略阻止: {path} 不在白名单目录中"
    if len(content.encode()) > cfg.write_file_max_size_kb * 1024:
        return f"内容超过 {cfg.write_file_max_size_kb}KB 限制"
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字符到 {path}"
    except Exception as e:
        return f"写入失败: {e}"


async def _cap_http(url: str, cfg: CapabilitiesConfig, method: str = "GET", body: str = "") -> str:
    if cfg.http_allowed_domains:
        domain = urlparse(url).hostname or ""
        if not any(domain.endswith(d) for d in cfg.http_allowed_domains):
            return f"安全策略阻止: 域名 {domain} 不在白名单中"
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=cfg.http_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kw: Dict[str, Any] = {}
            if body and method.upper() in ("POST", "PUT", "PATCH"):
                kw["data"] = body
            async with session.request(method.upper(), url, **kw) as resp:
                text = await resp.text()
                text = text[:30000] + "..." if len(text) > 30000 else text
                return f"[{resp.status}]\n{text}"
    except asyncio.TimeoutError:
        return f"HTTP 超时 ({cfg.http_timeout}s)"
    except Exception as e:
        return f"HTTP 失败: {e}"


async def _cap_python(code: str, cfg: CapabilitiesConfig) -> str:
    stdout_buf = io.StringIO()
    local_vars: Dict[str, Any] = {}
    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compile(code, "<skill_python>", "exec"), {"__builtins__": __builtins__}, local_vars)
        output = stdout_buf.getvalue()
        if not output and "result" in local_vars:
            output = str(local_vars["result"])
        return output[:20000] if output else "(无输出)"
    except Exception as e:
        return f"Python 错误: {e}\n{traceback.format_exc()}"


# ====== Agent Loop ======


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 token/字，英文约 0.75 token/word）。"""
    return max(len(text) // 2, len(text.split()) * 2)


def _truncate_messages(messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
    """保留 system + 最近的消息，确保不超过 token 预算。"""
    if not messages:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    system_tokens = sum(_estimate_tokens(m.get("content", "")) for m in system_msgs)
    budget = max_tokens - system_tokens
    if budget <= 0:
        return system_msgs

    # 从后往前保留消息
    kept: List[Dict[str, Any]] = []
    used = 0
    for msg in reversed(other_msgs):
        msg_tokens = _estimate_tokens(str(msg.get("content", "")))
        if used + msg_tokens > budget:
            break
        kept.append(msg)
        used += msg_tokens
    kept.reverse()
    return system_msgs + kept


def _load_script_fn(script_path: Path) -> Optional[Any]:
    """加载脚本的 run 函数。"""
    try:
        spec = importlib.util.spec_from_file_location(f"skill_script_{script_path.stem}", script_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "run", None)
    except Exception as e:
        logger.warning(f"加载脚本失败 {script_path}: {e}")
        return None


def _build_script_tools(skill: SkillDefinition) -> List[Dict[str, Any]]:
    """从 scripts 构建 tool schema。"""
    tools = []
    for name, path in skill.scripts.items():
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            schema = getattr(module, "TOOL_SCHEMA", None)
            if isinstance(schema, dict):
                tools.append(schema)
            elif hasattr(module, "run"):
                tools.append({"type": "function", "function": {
                    "name": name,
                    "description": (getattr(module, "__doc__", None) or f"执行 {name}").strip(),
                    "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "输入"}}}
                }})
        except Exception:
            pass
    return tools


async def run_agent_loop(
    skill: SkillDefinition, task: str, ctx: Any, config: SkillLoaderConfig,
    chat_context: str = "",
) -> str:
    """执行 agent 模式 skill。"""
    model = skill.model or config.default_model
    max_turns = skill.max_turns or config.default_max_turns
    max_tokens = config.agent_max_context_tokens
    cap_cfg = config.capabilities

    # 构建 tools = scripts + allowed capabilities
    tools = _build_script_tools(skill)
    allowed_caps = get_allowed_caps(skill, cap_cfg)
    denied_caps = [c for c in skill.capabilities if c not in allowed_caps]
    for cap in allowed_caps:
        if cap in CAPABILITY_SCHEMAS:
            tools.append(CAPABILITY_SCHEMAS[cap])
    cap_names = set(allowed_caps)

    # 加载脚本函数
    script_fns: Dict[str, Any] = {}
    for sname, spath in skill.scripts.items():
        fn = _load_script_fn(spath)
        if fn:
            script_fns[sname] = fn

    # System prompt + 权限提示 + 资源目录提示
    system_content = skill.instructions

    # 输出格式要求（结果会直接发送到聊天平台，不支持 markdown）
    system_content += "\n\n[重要：输出格式] 你的回复将直接发送到 QQ 等聊天平台，这些平台不支持 markdown 渲染。严禁使用任何 markdown 语法，包括但不限于：## 标题、**加粗**、*斜体*、```代码块```、- 列表。请用纯文本、换行和空格缩进来组织内容。"

    # 告知 agent 可用的资源目录（progressive disclosure）
    resource_hints = []
    if skill.references_dir:
        refs = [f.name for f in skill.references_dir.iterdir() if f.is_file()]
        if refs:
            resource_hints.append(f"参考文档目录 (references/): {', '.join(refs)}")
    if skill.assets_dir:
        assets = [f.name for f in skill.assets_dir.iterdir() if f.is_file()]
        if assets:
            resource_hints.append(f"资源文件目录 (assets/): {', '.join(assets)}")
    if resource_hints:
        system_content += "\n\n[可用资源]\n" + "\n".join(resource_hints)
        system_content += f"\n使用 read_file 工具读取，路径前缀: {skill.skill_path}/"

    if denied_caps:
        system_content += f"\n\n[系统提示] 以下能力因权限未开启不可用: {', '.join(denied_caps)}。请在不使用它们的前提下完成任务。"

    # 构建 user message：聊天上下文 + 任务
    user_content = task
    if chat_context:
        user_content = f"[最近的聊天记录，供你了解对话背景]\n{chat_context}\n\n[用户当前的需求]\n{task}"

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    response_text = ""
    for turn in range(max_turns):
        # Token 截断
        messages = _truncate_messages(messages, max_tokens)

        try:
            if tools:
                result = await ctx.llm.generate_with_tools(prompt=messages, tools=tools, model=model)
            else:
                result = await ctx.llm.generate(prompt=messages, model=model)
        except Exception as e:
            return f"Agent LLM 调用失败: {e}"

        if not result.get("success", False):
            error = result.get("error", "未知错误")
            if turn > 0 and response_text:
                return response_text + f"\n\n[Agent 在第 {turn+1} 轮遇到错误: {error}]"
            return f"Agent 调用失败: {error}"

        response_text = result.get("response", "")
        tool_calls = result.get("tool_calls", [])

        if not tool_calls:
            return response_text

        messages.append({"role": "assistant", "content": response_text, "tool_calls": [
            {"id": tc.get("id", ""), "type": "function", "function": tc.get("function", {})}
            for tc in tool_calls
        ]})

        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "")
            fn_args_raw = tc.get("function", {}).get("arguments", "{}")
            tc_id = tc.get("id", "")
            try:
                fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else (fn_args_raw or {})
            except (json.JSONDecodeError, TypeError):
                fn_args = {}

            # 分发：capability 还是 script
            if fn_name in cap_names:
                tool_result = await run_capability(fn_name, fn_args, cap_cfg)
            elif fn_name in script_fns:
                try:
                    fn = script_fns[fn_name]
                    if asyncio.iscoroutinefunction(fn):
                        tool_result = str(await fn(**fn_args))
                    else:
                        tool_result = str(await asyncio.to_thread(fn, **fn_args))
                except Exception as e:
                    tool_result = f"脚本执行错误: {e}"
            else:
                tool_result = f"未知工具: {fn_name}"

            # 截断过长的 tool 结果
            if len(tool_result) > 10000:
                tool_result = tool_result[:10000] + "\n... (结果已截断)"

            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})

    return response_text or f"Agent 达到最大轮数 ({max_turns})"


async def run_direct_skill(skill: SkillDefinition, task: str) -> str:
    """执行 direct 模式 skill。"""
    if not skill.scripts:
        return f"Skill '{skill.name}' 没有可执行脚本"
    entry = list(skill.scripts.values())[0]
    fn = _load_script_fn(entry)
    if fn is None:
        return f"无法加载脚本: {entry.name}"
    try:
        if asyncio.iscoroutinefunction(fn):
            return str(await fn(task))
        return str(await asyncio.to_thread(fn, task))
    except Exception as e:
        return f"执行失败: {e}\n{traceback.format_exc()}"


def _strip_markdown(text: str) -> str:
    """移除常见 markdown 标记，保留纯文本内容。"""
    import re as _re
    # 代码块 → 保留内容
    text = _re.sub(r'```\w*\n?', '', text)
    # 标题 → 保留文字
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
    # 加粗/斜体
    text = _re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = _re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    # 链接 [text](url) → text
    text = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # 行内代码
    text = _re.sub(r'`([^`]+)`', r'\1', text)
    return text.strip()


# ====== 后台任务管理 ======


class TaskManager:
    """管理超时后转入后台的 skill 任务。使用 asyncio.shield 保护原 task。"""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, str] = {}

    def is_running(self, key: str) -> bool:
        t = self._tasks.get(key)
        return t is not None and not t.done()

    def get_result(self, key: str) -> Optional[str]:
        return self._results.pop(key, None)

    def shield_and_track(self, key: str, task: asyncio.Task) -> None:
        """跟踪一个已存在的 task（由 shield 保护的）。"""
        self._tasks[key] = task
        task.add_done_callback(lambda t: self._on_done(key, t))

    def _on_done(self, key: str, task: asyncio.Task) -> None:
        self._tasks.pop(key, None)
        try:
            exc = task.exception()
            if exc:
                self._results[key] = f"后台执行失败: {exc}"
            else:
                self._results[key] = task.result()
        except asyncio.CancelledError:
            self._results[key] = "任务被取消"

    def cleanup(self, max_age: float = 600.0) -> None:
        """清理过期结果（简单实现）。"""
        pass  # v2 暂不实现 TTL，结果取走即删


# ====== 插件主类 ======


class SkillLoaderPlugin(MaiBotPlugin):
    """Skill Loader 插件 — 加载 Agent Skills 并注册为独立 Tool。"""

    config_model = SkillLoaderConfig

    def __init__(self):
        super().__init__()
        self._skills: Dict[str, SkillDefinition] = {}
        self._task_mgr = TaskManager()
        self._load_skills()

    @property
    def config(self) -> SkillLoaderConfig:
        return self._plugin_config_instance or SkillLoaderConfig()

    def _load_skills(self) -> None:
        """扫描并加载 skills。"""
        plugin_dir = Path(__file__).parent
        skills_dir = plugin_dir / self.config.skills_dir
        self._skills = scan_skills(skills_dir)
        if self._skills:
            logger.info(f"Skill Loader: 加载了 {len(self._skills)} 个 skill: {list(self._skills.keys())}")
        else:
            logger.warning(f"Skill Loader: 未找到任何 skill (目录: {skills_dir})")

    def get_components(self) -> List[Dict[str, Any]]:
        """覆写：返回动态 skill tools + 静态 /skill command。"""
        components = []

        # 动态 skill tools
        for skill in self._skills.values():
            params: Dict[str, Any] = {
                "task": {"type": "string", "description": f"要 {skill.name} 执行的任务描述", "required": True}
            }
            components.append({
                "name": skill.name,
                "type": "TOOL",
                "metadata": {
                    "handler_name": f"__skill__{skill.name}",  # 不存在的名字，触发 invoke_component 回退
                    "description": skill.description,
                    "parameters": params,
                },
            })

        # 静态 /skill command
        components.append({
            "name": "skill",
            "type": "COMMAND",
            "metadata": {
                "handler_name": "__skill__command",  # 不存在的名字，触发 invoke_component 回退
                "description": "管理 Agent Skills",
                "parameters": {
                    "action": {"type": "string", "description": "list|caps|enable|disable|reload", "required": True},
                    "target": {"type": "string", "description": "能力名称或 all", "required": False},
                },
            },
        })

        return components

    async def invoke_component(self, component_name: str, **kwargs) -> Any:
        """覆写：统一分发组件调用。"""
        if component_name == "skill":
            return await self._handle_skill_command(**kwargs)
        elif component_name in self._skills:
            return await self._invoke_skill(component_name, **kwargs)
        return {"name": component_name, "content": f"未知组件: {component_name}"}

    async def _get_chat_context(self, stream_id: str, limit: int = 10) -> str:
        """获取最近的聊天记录作为上下文。"""
        try:
            messages = await self.ctx.message.get_recent(stream_id, limit=limit)
            if not messages:
                return ""
            readable = await self.ctx.message.build_readable(messages)
            if readable:
                return str(readable)
        except Exception as e:
            logger.debug(f"获取聊天上下文失败: {e}")
        return ""

    async def _invoke_skill(self, skill_name: str = "", task: str = "", **kwargs) -> Dict[str, str]:
        """执行 skill。"""
        # invoke_component 传入 component_name，也可能直接被 handler 调用
        name = skill_name or kwargs.get("component_name", "")
        skill = self._skills.get(name)
        if not skill:
            return {"name": name, "content": f"未找到 skill: {name}"}

        stream_id = kwargs.get("stream_id", "")
        cap_cfg = self.config.capabilities
        timeout = self.config.timeout_seconds

        # 权限检查
        denied_caps: List[str] = []
        if skill.capabilities:
            allowed = get_allowed_caps(skill, cap_cfg)
            denied_caps = [c for c in skill.capabilities if c not in allowed]
            if denied_caps and not allowed and skill.mode == "agent":
                notice = (
                    f"[Skill Loader] {skill.name} 需要以下能力但均未开启: {', '.join(denied_caps)}\n"
                    f"请使用 /skill enable <capability> 开启。"
                )
                if stream_id:
                    await self.ctx.send.text(notice, stream_id)
                return {"name": skill.name, "content": f"执行失败：所需能力未开启，已通知用户。"}

        # 部分权限缺失时通知用户
        if denied_caps and stream_id:
            await self.ctx.send.text(
                f"[Skill Loader] {skill.name} 部分能力未开启: {', '.join(denied_caps)}，功能可能受限。",
                stream_id,
            )

        # 检查后台任务结果
        bg_result = self._task_mgr.get_result(skill.name)
        if bg_result is not None:
            if stream_id and bg_result:
                await self.ctx.send.text(_strip_markdown(bg_result), stream_id)
                return {"name": skill.name, "content": f"[{skill.name}] 后台任务完成，已将结果直接发送给用户。"}
            return {"name": skill.name, "content": bg_result}
        if self._task_mgr.is_running(skill.name):
            return {"name": skill.name, "content": f"{skill.name} 正在后台执行中，请稍后再次调用。"}

        # 执行 skill（带超时 + shield）
        if skill.mode == "direct":
            coro = run_direct_skill(skill, task)
        else:
            # 获取聊天上下文注入给 agent
            chat_context = ""
            if stream_id:
                chat_context = await self._get_chat_context(stream_id)
            coro = run_agent_loop(skill, task, self.ctx, self.config, chat_context=chat_context)

        real_task = asyncio.ensure_future(coro)
        try:
            result = await asyncio.wait_for(asyncio.shield(real_task), timeout=timeout)
            # 直接发送结果给用户，不再走 Maisaka reply 流程
            if stream_id and result:
                await self.ctx.send.text(_strip_markdown(result), stream_id)
                return {"name": skill.name, "content": f"[{skill.name}] 已将结果直接发送给用户。"}
            return {"name": skill.name, "content": result}
        except asyncio.TimeoutError:
            # 超时：shield 保护了 real_task，它继续在后台跑
            self._task_mgr.shield_and_track(skill.name, real_task)
            return {
                "name": skill.name,
                "content": f"{skill.name} 执行超时 ({timeout}s)，已转为后台运行。稍后再次调用可获取结果。",
            }
        except Exception as e:
            return {"name": skill.name, "content": f"执行异常: {e}"}

    async def _handle_skill_command(self, action: str = "list", target: str = "", **kwargs) -> str:
        """处理 /skill 命令。"""
        if action == "list":
            if not self._skills:
                return "当前没有加载任何 skill。"
            lines = ["已加载的 Skills:"]
            for s in self._skills.values():
                caps = f" [{', '.join(s.capabilities)}]" if s.capabilities else ""
                lines.append(f"  - {s.name} ({s.mode}): {s.description}{caps}")
            return "\n".join(lines)

        elif action == "caps":
            cfg = self.config.capabilities
            status = {
                "bash": cfg.allow_bash, "python": cfg.allow_python,
                "read_file": cfg.allow_read_file, "write_file": cfg.allow_write_file,
                "http": cfg.allow_http,
            }
            lines = ["Capabilities 状态:"]
            for name, enabled in status.items():
                icon = "ON" if enabled else "OFF"
                lines.append(f"  {name}: {icon}")
            return "\n".join(lines)

        elif action == "enable":
            return self._set_cap(target, True)

        elif action == "disable":
            return self._set_cap(target, False)

        elif action == "reload":
            self._load_skills()
            return f"已重新加载，当前 {len(self._skills)} 个 skill: {list(self._skills.keys())}"

        return f"未知操作: {action}。可用: list, caps, enable, disable, reload"

    def _set_cap(self, target: str, enable: bool) -> str:
        """设置 capability 开关。"""
        cfg = self.config.capabilities
        cap_map = {
            "bash": "allow_bash", "python": "allow_python",
            "read_file": "allow_read_file", "write_file": "allow_write_file",
            "http": "allow_http",
        }
        action_word = "开启" if enable else "关闭"

        if target == "all":
            for attr in cap_map.values():
                setattr(cfg, attr, enable)
            return f"已{action_word}所有 capabilities。"
        elif target in cap_map:
            setattr(cfg, cap_map[target], enable)
            return f"已{action_word} {target}。"
        else:
            return f"未知 capability: {target}。可用: {', '.join(cap_map.keys())}, all"

    async def on_load(self) -> None:
        logger.info(f"Skill Loader 已启动，{len(self._skills)} 个 skill 就绪")

    async def on_unload(self) -> None:
        logger.info("Skill Loader 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置热更新。"""
        if scope == "self":
            logger.info("Skill Loader 配置已更新")


def create_plugin() -> SkillLoaderPlugin:
    return SkillLoaderPlugin()
