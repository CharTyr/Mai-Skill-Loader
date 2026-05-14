"""Echo skill — 简单回声测试"""


async def run(task: str) -> str:
    """原样返回输入内容。"""
    return f"[Echo] {task}"
