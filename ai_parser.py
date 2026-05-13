import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


TAIPEI = ZoneInfo("Asia/Taipei")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def local_now():
    return datetime.now(TAIPEI)


def _groq_chat(messages, temperature=0.5, response_format=None):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None

    payload = {
        "model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def analyze_message(text):
    now = local_now()
    system = (
        "你是 LINE bot 的訊息判斷器。只輸出 JSON。"
        "欄位：intent: reminder|chat|none, confidence: 0-1, title: string|null, "
        "remind_at: YYYY-MM-DDTHH:MM:SS+08:00|null, "
        "recurrence: daily|weekly:0|weekly:1|weekly:2|weekly:3|weekly:4|weekly:5|weekly:6|monthly:1|interval:5|null, "
        "category: string|null, memory: string|null, reply_hint: string|null。"
        "像提醒但缺時間，intent=reminder 且 remind_at=null。"
        "聊天才 chat；不需要回覆就 none。"
    )
    user = f"現在台北時間是 {now.strftime('%Y-%m-%d %H:%M:%S %z')}。\n訊息：{text}"
    content = _groq_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    if content:
        try:
            return normalize_analysis(json.loads(content))
        except json.JSONDecodeError:
            pass
    return fallback_analyze_message(text, now)


def normalize_analysis(data):
    intent = data.get("intent")
    if intent not in {"reminder", "chat", "none"}:
        intent = "none"
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    recurrence = clean_or_none(data.get("recurrence"))
    if recurrence and not valid_recurrence(recurrence):
        recurrence = None
    return {
        "intent": intent,
        "confidence": max(0, min(1, confidence)),
        "title": clean_or_none(data.get("title")),
        "remind_at": clean_or_none(data.get("remind_at")),
        "recurrence": recurrence,
        "category": clean_or_none(data.get("category")),
        "memory": clean_or_none(data.get("memory")),
        "reply_hint": clean_or_none(data.get("reply_hint")),
    }


def clean_or_none(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def valid_recurrence(value):
    return bool(
        value == "daily"
        or re.fullmatch(r"weekly:[0-6]", value)
        or re.fullmatch(r"monthly:([1-9]|[12]\d|3[01])", value)
        or re.fullmatch(r"interval:[1-9]\d*", value)
    )


def fallback_analyze_message(text, now=None):
    now = now or local_now()
    category = extract_category(text)
    remind_at, recurrence = guess_datetime_and_recurrence(text, now)
    if remind_at:
        return {
            "intent": "reminder",
            "confidence": 0.82 if is_clear_reminder(text) else 0.7,
            "title": guess_title(text),
            "remind_at": remind_at.isoformat(timespec="seconds"),
            "recurrence": recurrence,
            "category": category,
            "memory": None,
            "reply_hint": None,
        }
    if is_incomplete_reminder(text):
        return {
            "intent": "reminder",
            "confidence": 0.62,
            "title": guess_title(text),
            "remind_at": None,
            "recurrence": recurrence,
            "category": category,
            "memory": None,
            "reply_hint": "missing_time",
        }
    memory = extract_memory(text)
    if memory:
        return base_chat(memory=memory)
    if looks_like_chat(text):
        return base_chat()
    return {
        "intent": "none",
        "confidence": 0.4,
        "title": None,
        "remind_at": None,
        "recurrence": None,
        "category": None,
        "memory": None,
        "reply_hint": None,
    }


def base_chat(memory=None):
    return {
        "intent": "chat",
        "confidence": 0.6,
        "title": None,
        "remind_at": None,
        "recurrence": None,
        "category": None,
        "memory": memory,
        "reply_hint": None,
    }


def guess_datetime_and_recurrence(text, now):
    interval = guess_interval_recurrence(text)
    if interval:
        minutes = int(interval.split(":", 1)[1])
        return now + timedelta(minutes=minutes), interval
    recurrence = guess_recurrence(text)
    relative = guess_relative_datetime(text, now)
    if relative:
        return relative, recurrence
    monthly = guess_recurring_monthly_datetime(text, now, recurrence)
    if monthly:
        return monthly, recurrence
    month_day = guess_month_day(text, now)
    if month_day:
        return month_day, recurrence
    weekday = guess_weekday_datetime(text, now)
    if weekday:
        if recurrence is None and re.search(r"每週|每周|每禮拜|每星期", text):
            recurrence = f"weekly:{weekday.weekday()}"
        return weekday, recurrence
    absolute = guess_absolute_datetime(text, now)
    if absolute:
        return absolute, recurrence
    return None, recurrence


def guess_interval_recurrence(text):
    match = re.search(r"每\s*(\d+)\s*(分鐘|分)\s*(?:提醒|叫|通知)?(?:我)?(?:一次)?", text)
    if match:
        return f"interval:{max(1, int(match.group(1)))}"
    return None


def guess_recurrence(text):
    if re.search(r"每天|每日|天天", text):
        return "daily"
    weekly = re.search(r"每(?:週|周|禮拜|星期)([一二三四五六日天])", text)
    if weekly:
        return f"weekly:{WEEKDAYS[weekly.group(1)]}"
    monthly = re.search(r"每(?:個)?月\s*(\d{1,2})\s*(?:號|日)", text)
    if monthly:
        return f"monthly:{max(1, min(31, int(monthly.group(1))))}"
    return None


def guess_relative_datetime(text, now):
    match = re.search(r"(\d+)\s*(秒|秒鐘|分鐘|分|小時|個小時|天)\s*後", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in {"秒", "秒鐘"}:
            return now + timedelta(seconds=amount)
        if unit in {"分鐘", "分"}:
            return now + timedelta(minutes=amount)
        if unit in {"小時", "個小時"}:
            return now + timedelta(hours=amount)
        return now + timedelta(days=amount)
    if re.search(r"等一下|等等|待會|待會兒|稍後", text):
        return now + timedelta(minutes=5)
    return None


def guess_recurring_monthly_datetime(text, now, recurrence):
    if not recurrence or not recurrence.startswith("monthly:"):
        return None
    day = int(recurrence.split(":", 1)[1])
    hour, minute, second = guess_time_parts(text)
    if hour is None:
        hour, minute, second = 9, 0, 0
    result = now.replace(day=min(day, days_in_month(now.year, now.month)), hour=hour, minute=minute, second=second, microsecond=0)
    if result <= now:
        year, month = next_month(now.year, now.month)
        result = result.replace(year=year, month=month, day=min(day, days_in_month(year, month)))
    return result


def guess_month_day(text, now):
    if "月底" not in text:
        return None
    day = days_in_month(now.year, now.month)
    result = now.replace(day=day, hour=20, minute=0, second=0, microsecond=0)
    if result <= now:
        year, month = next_month(now.year, now.month)
        result = result.replace(year=year, month=month, day=days_in_month(year, month))
    return result


def guess_weekday_datetime(text, now):
    match = re.search(r"(?:下週|下周|下禮拜|下星期|週|周|禮拜|星期)([一二三四五六日天])", text)
    if not match:
        return None
    target = WEEKDAYS[match.group(1)]
    days_ahead = (target - now.weekday()) % 7
    if re.search(r"下週|下周|下禮拜|下星期", match.group(0)):
        days_ahead = days_ahead or 7
    elif days_ahead == 0:
        days_ahead = 7
    hour, minute, second = guess_time_parts(text)
    if hour is None:
        hour, minute, second = 9, 0, 0
    return (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=second, microsecond=0)


def guess_absolute_datetime(text, now):
    day = now
    if "後天" in text:
        day = now + timedelta(days=2)
    elif "明天" in text:
        day = now + timedelta(days=1)
    hour, minute, second = guess_time_parts(text)
    if hour is None:
        return None
    result = day.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if result <= now:
        result += timedelta(days=1)
    return result


def guess_time_parts(text):
    colon = re.search(r"(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2}))?", text)
    if colon:
        return int(colon.group(1)), int(colon.group(2)), int(colon.group(3) or 0)
    match = re.search(r"(\d{1,2})\s*點(?:半|(\d{1,2})分?)?", text)
    if not match:
        return None, 0, 0
    hour = int(match.group(1))
    minute = 30 if "點半" in match.group(0) else int(match.group(2) or 0)
    if ("下午" in text or "晚上" in text) and hour < 12:
        hour += 12
    if "中午" in text and hour < 11:
        hour += 12
    return hour, minute, 0


def extract_category(text):
    match = re.search(r"分類\s*([^\s，,。]+)", text)
    return match.group(1) if match else None


def is_incomplete_reminder(text):
    return bool(re.search(r"提醒我|幫我記得|記得|叫我|通知我|不要忘|別忘", text))


def is_clear_reminder(text):
    return bool(re.search(r"提醒我|幫我記得|叫我|通知我|不要忘|別忘|鬧鐘", text))


def extract_memory(text):
    if re.search(r"我喜歡|我討厭|我叫|我是|記住|你要記得", text):
        return text
    return None


def looks_like_chat(text):
    if len(text) <= 1:
        return False
    return bool(re.search(r"嗎|呢|怎麼|為什麼|可不可以|可以嗎|你覺得|嗨|哈囉|你好|好累|難過|開心|煩|無聊|壓力|睡不著|心情", text))


def guess_title(text):
    title = text
    title = re.sub(r"分類\s*[^\s，,。]+", "", title)
    title = re.sub(r"每\s*\d+\s*(分鐘|分)\s*(?:提醒|叫|通知)?(?:我)?(?:一次)?", "", title)
    title = re.sub(r"直到我說好|直到說好|一直提醒|重複提醒", "", title)
    title = re.sub(r"每(?:週|周|禮拜|星期)[一二三四五六日天]", "", title)
    title = re.sub(r"每(?:個)?月\s*\d{1,2}\s*(?:號|日)", "", title)
    title = re.sub(r"每天|每日|天天|每週|每周|每禮拜|每星期|每個月|每月", "", title)
    title = re.sub(r"今天|明天|後天|早上|中午|下午|晚上|凌晨|月底", "", title)
    title = re.sub(r"(下週|下周|下禮拜|下星期|週|周|禮拜|星期)[一二三四五六日天]", "", title)
    title = re.sub(r"\d+\s*(秒|秒鐘|分鐘|分|小時|個小時|天)\s*後", "", title)
    title = re.sub(r"等一下|等等|待會|待會兒|稍後", "", title)
    title = re.sub(r"\d{1,2}[:：]\d{1,2}(?:[:：]\d{1,2})?", "", title)
    title = re.sub(r"\d{1,2}\s*點(?:半|\d{1,2}分?)?", "", title)
    title = re.sub(r"\d{1,2}\s*(號|日)", "", title)
    title = re.sub(r"提醒我|提醒|幫我記得|記得|叫我|通知我|不要忘記|不要忘|別忘|鬧鐘", "", title)
    title = re.sub(r"\s+", " ", title).strip(" ，,。.")
    return title or "提醒事項"


def make_chat_reply(user_text, memories, history, settings=None):
    settings = settings or {}
    tone = settings.get("tone") or "自然、像朋友、不要太 AI"
    city = settings.get("city")
    memory_text = "\n".join(f"- {item['content']}" for item in memories) or "沒有特別記憶。"
    if city:
        memory_text += f"\n- 使用者所在城市偏好：{city}"
    messages = [
        {
            "role": "system",
            "content": (
                "你是在 LINE 裡聊天的朋友型 AI 助手。"
                "回覆要像真人訊息：短、自然、有上下文，不要條列太多，不要自稱 AI，"
                "不要每句都反問，不要像客服罐頭。可以吐槽一點但不要油。"
                f"語氣偏好：{tone}\n已知記憶：\n{memory_text}"
            ),
        }
    ]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})
    content = _groq_chat(messages, temperature=0.85)
    if content:
        return content[:900]
    return f"我懂，你剛剛說的是「{user_text[:80]}」。我先接住這句。"


def days_in_month(year, month):
    if month == 12:
        return 31
    return (datetime(*next_month(year, month), 1, tzinfo=TAIPEI) - datetime(year, month, 1, tzinfo=TAIPEI)).days


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)
