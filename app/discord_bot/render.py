"""Discord 訊息格式：把 CMD 的文字輸出放進程式碼區塊並依 Discord 長度限制分段。"""

from dataclasses import dataclass
from typing import Any

DISCORD_LIMIT = 2000
CHUNK_LIMIT = 1900  # 保留程式碼區塊標記的空間
MAX_MESSAGES = 4  # 超過時改以文字檔附件回覆，避免洗版與觸發速率限制


@dataclass
class Reply:
    """服務層執行結果；code=True 時以等寬程式碼區塊顯示（表格對齊）。"""

    text: str
    code: bool = True
    # 在事件迴圈中建立的互動元件（例如確認按鈕）；需要時由回覆流程呼叫
    view_factory: Any = None


def _escape(text: str) -> str:
    # 防止內容提前結束程式碼區塊
    return text.replace("```", "`​``")


def split_lines(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines() or [""]:
        while len(line) > limit:  # 單行過長時硬切
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def to_messages(reply: Reply) -> list[str] | None:
    """回傳要依序送出的訊息內容；太長時回傳 None（改用附件）。"""
    text = reply.text.strip() or "（沒有內容）"
    if reply.code:
        chunks = [f"```\n{c}\n```" for c in split_lines(_escape(text))]
    else:
        chunks = split_lines(text, DISCORD_LIMIT - 50)
    return chunks if len(chunks) <= MAX_MESSAGES else None
