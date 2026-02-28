"""
LLM Layer — OpenAI API integration.
Generates: micro_step, next_step_upgrade, intervention, if_then_plan.
All outputs are strict JSON, validated, with 1 retry on failure.
"""

import os
import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

client = None


def get_client() -> OpenAI:
    global client
    if client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            logger.warning("OPENAI_API_KEY not set, LLM calls will use fallback")
            return None
        client = OpenAI(api_key=api_key)
    return client


# ── Shared call helper with retry ──

def _call_llm(system_prompt: str, user_prompt: str, max_retries: int = 1) -> dict:
    c = get_client()
    if not c:
        return None

    for attempt in range(max_retries + 1):
        try:
            extra = ""
            if attempt > 0:
                extra = "\n\n⚠️ 上一次你的回复不是合法JSON。这次你必须只输出一个JSON对象，不包含任何其他文字、markdown、代码块。"

            resp = c.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": system_prompt + extra},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=800,
                response_format={"type": "json_object"}
            )
            text = resp.choices[0].message.content.strip()
            parsed = json.loads(text)
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(f"LLM JSON parse error (attempt {attempt+1}): {e}")
            if attempt == max_retries:
                return None
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            return None
    return None


# ── Generators ──

SYSTEM_BASE = """你是"执行陪伴系统"的AI引擎。你只输出JSON，不输出任何其他文字。
语气：坚定、温和、短句。不说教，不用"你应该/你必须"。用"我们现在只做…""下一步是…"。
所有中文输出。"""


def generate_micro_step(mainline_title: str, task_title: str = None, context: str = None) -> dict:
    """Generate a ≤2 min micro step."""
    system = SYSTEM_BASE + """
输出格式（严格JSON）：
{"type":"micro_step","micro_step":{"duration_min":2,"instruction":"具体动作指令","acceptance_criteria":"可验证的完成标准"}}
instruction必须是2分钟内可完成的具体动作。不能用抽象词（研究/优化/弄清楚）。"""

    user = f"今日主线：{mainline_title}"
    if task_title:
        user += f"\n关联任务：{task_title}"
    if context:
        user += f"\n背景：{context}"
    user += "\n\n请生成一个2分钟起步动作。"

    result = _call_llm(system, user)
    if result and "micro_step" in result:
        return result
    # Fallback
    return {
        "type": "micro_step",
        "micro_step": {
            "duration_min": 2,
            "instruction": f"打开「{mainline_title[:20]}」相关材料，找到你要开始的位置。只是打开，不用做别的。",
            "acceptance_criteria": "材料已打开在屏幕上"
        }
    }


def generate_upgrade_step(mainline_title: str, task_title: str = None, micro_instruction: str = None) -> dict:
    """Generate an 8 min upgrade step after micro completion."""
    system = SYSTEM_BASE + """
输出格式（严格JSON）：
{"type":"next_step","step":{"duration_min":8,"instruction":"具体动作指令","acceptance_criteria":"可验证的完成标准","difficulty":1}}
这是用户完成2分钟起步后的升级动作（8分钟）。基于他刚完成的内容，给下一步。"""

    user = f"今日主线：{mainline_title}"
    if task_title:
        user += f"\n关联任务：{task_title}"
    if micro_instruction:
        user += f"\n刚完成的起步动作：{micro_instruction}"
    user += "\n\n请生成一个8分钟的升级动作。"

    result = _call_llm(system, user)
    if result and "step" in result:
        return result
    return {
        "type": "next_step",
        "step": {
            "duration_min": 8,
            "instruction": f"继续推进「{mainline_title[:20]}」——完成下一个小节或练习题。",
            "acceptance_criteria": "能用1句话说出完成了什么",
            "difficulty": 1
        }
    }


def generate_if_then_plan(mainline_title: str) -> dict:
    """Generate an if-then implementation intention."""
    system = SYSTEM_BASE + """
输出格式（严格JSON）：
{"type":"if_then_plan","plan":{"if_trigger":"如果…的情境","then_action":"那么我会…的具体动作","reward":"完成后…的小奖励"}}
生成一个实施意图，帮用户应对最可能的分心/犹豫场景。"""

    user = f"今日主线：{mainline_title}\n请生成一个if-then实施意图。"

    result = _call_llm(system, user)
    if result and "plan" in result:
        return result
    return {
        "type": "if_then_plan",
        "plan": {
            "if_trigger": "如果我开始犹豫或想刷手机",
            "then_action": "我先做2分钟起步动作",
            "reward": "完成后允许自己休息3分钟"
        }
    }


def generate_intervention(stuck_type: str, emotion_label: str = None,
                          mainline_title: str = None, step_instruction: str = None,
                          recent_evidence: list = None) -> dict:
    """Generate stuck intervention with body reset and restart step."""
    evidence_section = ""
    if stuck_type == "SELF_LIMITING" and recent_evidence:
        evidence_section = f"""
这是SELF_LIMITING卡点，你必须在输出中包含 evidence_quotes 数组（1-3条），从下面的证据中选取：
{json.dumps(recent_evidence, ensure_ascii=False)}"""

    system = SYSTEM_BASE + f"""
输出格式（严格JSON）：
{{"type":"intervention","stuck_type":"{stuck_type}","emotion_label":"用户选择的情绪",
"body_reset":"30秒身体动作指令",
"intervention_text":"30-90秒的干预文字，短句，行动导向",
"restart_step":{{"duration_min":2,"instruction":"2分钟起步动作","acceptance_criteria":"验收标准"}},
"push_line":"一句话推回计时器",
"evidence_quotes":null}}
{evidence_section}
intervention_text要短（<150字），不说教。body_reset必须是具体的身体动作（深呼吸/握拳松开/站起来等）。"""

    user = f"卡点类型：{stuck_type}"
    if emotion_label:
        user += f"\n情绪：{emotion_label}"
    if mainline_title:
        user += f"\n今日主线：{mainline_title}"
    if step_instruction:
        user += f"\n当前步骤：{step_instruction}"
    user += "\n\n请生成干预内容。"

    result = _call_llm(system, user)
    if result and "intervention_text" in result:
        if stuck_type == "SELF_LIMITING" and not result.get("evidence_quotes") and recent_evidence:
            result["evidence_quotes"] = [e[:60] for e in recent_evidence[:3]]
        return result

    # Fallback interventions
    fallbacks = {
        "PERFECTIONISM": {
            "body_reset": "双手握拳3秒，用力，然后松开。感受手指放松。",
            "intervention_text": "完美是陷阱。我们现在只做一个烂版本——比空白好一万倍。",
            "restart_step": {"duration_min": 2, "instruction": "写下关于这个任务你知道的3个词。不准修改。", "acceptance_criteria": "3个词出现在屏幕上"},
            "push_line": "烂版本 > 空白。开始 →"
        },
        "GOAL_TOO_BIG": {
            "body_reset": "站起来，伸展双臂过头顶，保持5秒，放下。",
            "intervention_text": "你不需要看到终点，只看下一步。现在这一步只有2分钟。",
            "restart_step": {"duration_min": 2, "instruction": "打开你需要的页面或文件。只是打开。", "acceptance_criteria": "页面/文件已打开"},
            "push_line": "打开了就是开始。继续 →"
        },
        "OVERTHINKING": {
            "body_reset": "做两轮生理叹息：鼻子吸气→再吸一点→嘴巴长呼气。",
            "intervention_text": "大脑在转圈不是在前进。不需要想清楚才开始，开始了才会想清楚。",
            "restart_step": {"duration_min": 2, "instruction": "不做选择——直接做第一个动作：打开/点击/写第一个字。", "acceptance_criteria": "已动手做了第一个物理动作"},
            "push_line": "动了就对了 →"
        },
        "EMOTIONAL_FRICTION": {
            "body_reset": "双脚踩实地面，感受脚底压力，保持10秒。",
            "intervention_text": "给情绪取个名字。说出来。情绪不需要消失，我们带着它做2分钟。",
            "restart_step": {"duration_min": 2, "instruction": "带着这个情绪，写下今天任务的标题。", "acceptance_criteria": "写下了标题"},
            "push_line": "情绪还在？没关系，我们已经在动了 →"
        },
        "REWARD_MISMATCH": {
            "body_reset": "把手机翻面朝下，推到伸手够不到的地方。",
            "intervention_text": "先做2分钟，做完再刷——带着「完成了一步」的感觉刷，完全不一样。",
            "restart_step": {"duration_min": 2, "instruction": "手机远离后，打开任务材料。", "acceptance_criteria": "手机已远离+材料已打开"},
            "push_line": "2分钟后你自由了 →"
        },
        "SELF_LIMITING": {
            "body_reset": "双手放在桌上，手指用力按压桌面5秒，然后松开。",
            "intervention_text": "「我不行」是想法，不是事实。现在只需要「试2分钟」。",
            "restart_step": {"duration_min": 2, "instruction": "写下：「我不确定我行，但我可以试2分钟。」然后开始。", "acceptance_criteria": "写下了这句话"},
            "push_line": "试了就是证据 →"
        },
    }
    fb = fallbacks.get(stuck_type, fallbacks["OVERTHINKING"])
    return {
        "type": "intervention",
        "stuck_type": stuck_type,
        "emotion_label": emotion_label or "",
        "evidence_quotes": [e[:60] for e in recent_evidence[:3]] if stuck_type == "SELF_LIMITING" and recent_evidence else None,
        **fb
    }
