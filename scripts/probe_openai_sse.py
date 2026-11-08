"""模拟 DSH（pi-ai openai-completions）会发的请求，逐帧打印 SSE 原文。

关注三件事：delta.tool_calls 的形状、finish_reason、"usage" 帧是否存在。
"""

import json
import urllib.request

URL = "http://127.0.0.1:8000/v1/chat/completions"

PAYLOAD = {
    "model": "deepseek-v4-pro-qwen3.5-9b",
    "messages": [
        {"role": "system", "content": "你是一个编码助手。需要查天气时必须调用工具。"},
        {"role": "user", "content": "杭州和北京今天哪个适合跑步？"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}, "unit": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ],
    "tool_choice": "auto",
    "stream": True,
    "stream_options": {"include_usage": True},
    "max_completion_tokens": 256,
    "temperature": 0.2,
}


def main() -> None:
    data = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        URL, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                continue
            print(line[:300])


if __name__ == "__main__":
    main()
