"""
最小 LLM 客户端 — 同步 + 流式调用 OpenAI 兼容 API
不依赖任何框架，只用了 requests

配置：通过环境变量设置
  LLM_BASE_URL: API 地址
  LLM_API_KEY:  API 密钥
  MODEL_NAME:   模型名称
"""

import json
import os
import time
import requests

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-chat")


def call_llm(messages: list[dict], temperature: float = 0.6, stream: bool = False,
             json_mode: bool = False):
    """
    调用 LLM。

    Args:
        messages: [{"role": "user", "content": "..."}, ...]
        temperature: 采样温度
        stream: True 时逐块 yield 文本，False 时直接返回完整文本
        json_mode: True 时设置 response_format 为 json_object

    Returns:
        stream=False 时返回完整回复字符串
        stream=True 时逐块 yield {"type": "content", "text": "..."}
    """
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    data = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    if json_mode:
        data["response_format"] = {"type": "json_object"}

    if stream:
        return _stream_request(url, headers, data)
    else:
        return _sync_request(url, headers, data)


def _sync_request(url, headers, data):
    """同步请求，带重试。"""
    for i in range(3):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=300)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            if i == 2:
                raise RuntimeError(f"LLM 请求失败: {e}")
            time.sleep(1 * (i + 1))


def _stream_request(url, headers, data):
    """流式请求，逐块 yield。"""
    for i in range(3):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=300, stream=True)
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(data_str)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content:
                        yield {"type": "content", "text": content}
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
            return
        except Exception as e:
            if i == 2:
                raise RuntimeError(f"LLM 流式请求失败: {e}")
            time.sleep(1 * (i + 1))
