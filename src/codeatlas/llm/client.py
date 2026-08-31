"""LLM 客户端（OpenAI 兼容）。未配置 API key 时 get_client() 返回 None，
全流程降级为无 LLM 模式 —— 这是架构约束 #4 的实现点。"""
from __future__ import annotations
import json, os, logging
log = logging.getLogger(__name__)


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    def _chat(self, system: str, user: str, max_tokens: int = 1200) -> str:
        import urllib.request
        body = json.dumps({
            "model": self.model, "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]

    def answer(self, system: str, q: str, context: str) -> str:
        return self._chat(system, f"证据：\n{context}\n\n问题：{q}")

    def one_liner(self, ctx: dict) -> str:
        return self._chat(
            "用不超过30个中文字概括这个C函数的职责。只输出这一句话，不要任何前后缀。",
            f"函数名: {ctx['name']}\n签名: {ctx['sig']}\n调用: {', '.join(ctx['callees'][:10])}",
            max_tokens=80).strip().strip('"')

    def extract_experience(self, messages: list, schema: dict) -> dict:
        raw = self._chat(
            "从开发会话中抽取可复用的排障经验。只输出 JSON，不要 markdown 代码块。\n"
            f"schema: {json.dumps(schema, ensure_ascii=False)}",
            json.dumps(messages, ensure_ascii=False)[:12000])
        return json.loads(raw.replace("```json", "").replace("```", "").strip())


def get_client():
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        return None
    return OpenAICompatClient(
        os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"), key,
        os.environ.get("LLM_MODEL", "gpt-4o-mini"))
