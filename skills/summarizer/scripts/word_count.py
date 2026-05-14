"""Word count 工具 — 统计文本字数"""

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "word_count",
        "description": "统计输入文本的字数和字符数",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要统计的文本"}
            },
            "required": ["text"],
        },
    },
}


def run(text: str = "") -> str:
    """统计文本字数。"""
    chars = len(text)
    words = len(text.split())
    return f"字符数: {chars}, 词数: {words}"
