"""Keep common generated memory confirmations consistent with the Host receipt."""

import re

_CONFIRMATION = re.compile(
    r"已(?:经)?(?:为你|帮你)?(?:记住|记下|记好|记牢)"
    r"[^。！？\n]{0,60}(?:偏好|习惯|长期记忆|跨会话)"
    r"|已(?:经)?[^。！？\n]{0,80}(?:保存|写入|存入|纳入|记录)"
    r"[^。！？\n]{0,30}(?:长期记忆|跨会话|记忆|偏好)"
    r"|我(?:会|将)(?:长期|一直|永久)(?:记住|记着|记得)"
    r"|(?:I(?:'ve| have)?\s+(?:saved|stored|remembered)|saved\s+to)"
    r"[^.!?\n]{0,80}(?:preference|memory|future conversations)"
    r"|I(?:'ll| will)\s+remember[^.!?\n]{0,80}(?:preference|future conversations)",
    re.I,
)
_NEGATIVE = re.compile(r"未能|尚未|没有|无法|不能|并未|未保存|未记住|未记下")
_BUSINESS_QUOTE = re.compile(r"(?:模板|会议纪要|示例|例句|原文|引用|翻译)[：:]|^\s*>")


def sanitize_memory_claims(answer: str, receipt: dict | None) -> str:
    """Correct unsupported confirmations before streaming and final persistence.

    This is a bounded guard for common assertions, not a semantic classifier.
    The prompts and the separately rendered database receipt remain authoritative.
    """
    if receipt is None or receipt.get("status") == "committed":
        return answer
    replacement = "本轮未提交长期记忆变更。"
    changed = False

    def replace(sentence):
        nonlocal changed
        piece = sentence.group()
        plain = re.sub(r"[*_`#]", "", piece)
        match = _CONFIRMATION.search(plain)
        if (
            not match
            or _NEGATIVE.search(plain[: match.start()])
            or _BUSINESS_QUOTE.search(plain[: match.start()])
        ):
            return piece
        result = replacement if not changed else ""
        changed = True
        return result

    # Retain the original layout and every unaffected business sentence.
    return re.sub(r"[^。！？.!?\n]+[。！？.!?]?", replace, answer)
