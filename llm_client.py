#!/usr/bin/env python3
"""
llm_client.py — agent-eye LLM 客户端

提供 OpenAI 兼容接口的 chat 函数，支持 DeepSeek / Claude / 本地模型。
配置方式：
    1. 环境变量: DEEPSEEK_API_KEY / OPENAI_API_KEY / CLAUDE_API_KEY
    2. 代码: create_llm(provider='deepseek', api_key='...')

返回 (system_prompt, user_message) -> str 格式的 callable，
可直接传入 thinker.decide(llm=fn)。
"""

import os
import json
import urllib.request
import urllib.error

# 默认端点
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "claude": "https://api.anthropic.com/v1/messages",
}

# 默认模型
MODELS = {
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "claude": "claude-sonnet-4-20250514",
}


def _chat_openai_compat(
    system: str,
    user: str,
    endpoint: str,
    api_key: str,
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> str:
    """OpenAI 兼容 API 调用（DeepSeek / OpenAI / 本地 OpenAI 兼容服务）。"""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 错误 {e.code}: {body[:300]}")
    except Exception as e:
        raise RuntimeError(f"API 调用失败: {e}")


def create_llm(
    provider: str = "deepseek",
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> "callable":
    """
    创建 LLM 调用函数。

    Args:
        provider: 'deepseek' | 'openai' | 'claude'
        api_key: API 密钥。默认从环境变量读取
        model: 模型名。默认按 provider 选择
        endpoint: API 端点。默认按 provider 选择

    Returns:
        (system_prompt: str, user_message: str) -> str
    """
    provider = provider.lower()

    # API Key: 参数 > 环境变量
    env_keys = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "claude": "CLAUDE_API_KEY",
    }
    key = api_key or os.environ.get(env_keys.get(provider, ""))
    if not key:
        raise ValueError(
            f"未设置 {provider.upper()}_API_KEY。"
            f"请在环境变量中设置或传入 api_key 参数。"
        )

    ep = endpoint or ENDPOINTS.get(provider)
    m = model or MODELS.get(provider, "deepseek-chat")

    if not ep:
        raise ValueError(f"未知 provider: {provider}。可用: {list(ENDPOINTS)}")

    # Claude 使用不同的 API 格式
    if provider == "claude":
        return _make_claude_fn(ep, key, m)
    else:
        return lambda s, u: _chat_openai_compat(s, u, ep, key, m)


def _make_claude_fn(endpoint: str, api_key: str, model: str):
    """创建 Claude 专用的 chat 函数。"""
    def fn(system: str, user: str) -> str:
        payload = json.dumps({
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"].strip()
        except Exception as e:
            raise RuntimeError(f"Claude API 调用失败: {e}")
    return fn


if __name__ == "__main__":
    # 自检：尝试从环境变量读取
    print("支持的 provider:", list(ENDPOINTS.keys()))
    for p in ["deepseek", "openai", "claude"]:
        env_key = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "claude": "CLAUDE_API_KEY"}[p]
        has = "✅" if os.environ.get(env_key) else "❌ 未设置"
        print(f"  {p}: {has} ({env_key})")
