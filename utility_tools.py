import ast
import operator
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


TAIPEI = ZoneInfo("Asia/Taipei")
OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def handle_utility_message(text):
    weather = maybe_weather(text)
    if weather:
        return weather

    current_time = maybe_time(text)
    if current_time:
        return current_time

    calc = maybe_calculate(text)
    if calc:
        return calc

    return None


def maybe_weather(text):
    if "天氣" not in text and "氣溫" not in text:
        return None

    location = "Taipei"
    match = re.search(r"(.+?)(?:的)?(?:天氣|氣溫)", text)
    if match:
        candidate = match.group(1).strip(" ，,。?")
        if candidate and candidate not in {"今天", "明天", "現在", "查", "幫我查"}:
            location = candidate

    try:
        response = requests.get(
            f"https://wttr.in/{location}",
            params={"format": "j1", "lang": "zh-tw"},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        current = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0].get("areaName", [{"value": location}])[0]["value"]
        desc = current["lang_zh-tw"][0]["value"] if current.get("lang_zh-tw") else current["weatherDesc"][0]["value"]
        return (
            f"{area} 現在天氣：{desc}\n"
            f"氣溫：{current['temp_C']}°C，體感：{current['FeelsLikeC']}°C\n"
            f"濕度：{current['humidity']}%，風速：{current['windspeedKmph']} km/h"
        )
    except Exception:
        return "我剛剛查天氣失敗了，可能是天氣服務暫時連不上。"


def maybe_time(text):
    if not re.search(r"(現在幾點|現在時間|幾點了|今天日期)", text):
        return None

    now = datetime.now(TAIPEI)
    return now.strftime("現在是 %Y/%m/%d %H:%M:%S")


def maybe_calculate(text):
    match = re.fullmatch(r"(?:計算|算一下|幫我算)\s*([0-9+\-*/().% ]+)", text)
    if not match:
        return None

    expr = match.group(1)
    try:
        value = safe_eval(expr)
    except Exception:
        return "這個算式我不太敢算，換成純數字和 + - * / 試試。"
    return f"{expr.strip()} = {value}"


def safe_eval(expr):
    node = ast.parse(expr, mode="eval")
    return eval_node(node.body)


def eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](eval_node(node.left), eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](eval_node(node.operand))
    raise ValueError("unsafe expression")
