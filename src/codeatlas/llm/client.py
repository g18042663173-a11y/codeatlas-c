"""LLM 客户端（OpenAI 兼容）。未配置 API key 时 get_client() 返回 None，
全流程降级为无 LLM 模式 —— 这是架构约束 #4 的实现点。"""
from __future__ import annotations
import json, os, logging, time, math, uuid
import http.client
import urllib.error
from urllib.parse import urlsplit
log = logging.getLogger(__name__)
GO_BASE_URL = "https://opencode.ai/zen/go/v1"
GO_MODEL = "deepseek-v4-flash"
USER_AGENT = "CodeAtlas/0.1 (proof-campaign; OpenAI-compatible)"


class BudgetExceeded(RuntimeError):
    """A predeclared evaluation request/cost ceiling has been reached."""


class OutputTruncated(RuntimeError):
    """The upstream stopped because the configured completion budget was exhausted."""


class UpstreamHTTPError(RuntimeError):
    """Display-safe upstream failure; response bodies and credentials are discarded."""

    def __init__(self, category: str, http_status: int) -> None:
        self.category = category
        self.http_status = http_status
        super().__init__(f"{category} (HTTP {http_status})")


class ProviderUsageLimitReached(UpstreamHTTPError):
    """The provider's included allowance is exhausted; never enable paid fallback."""


def _classify_http_error(exc: urllib.error.HTTPError) -> UpstreamHTTPError:
    """Classify an OpenAI-compatible error without retaining its untrusted body."""
    provider_type = ""
    try:
        raw = exc.read(64 * 1024)
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            provider_type = error["type"]
    except Exception:
        # The response body is deliberately neither logged nor re-raised.
        pass
    if exc.code == 429 and provider_type == "GoUsageLimitError":
        return ProviderUsageLimitReached("provider_usage_limit", exc.code)
    category = {
        400: "request_rejected",
        401: "authentication_failed",
        403: "authorization_failed",
        404: "endpoint_not_found",
        408: "upstream_timeout",
        409: "upstream_conflict",
        429: "rate_limited",
    }.get(exc.code, "upstream_http_error")
    return UpstreamHTTPError(category, exc.code)


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, *,
                 max_requests: int | None = None, max_cost_usd: float | None = None,
                 input_usd_per_million: float | None = None,
                 output_usd_per_million: float | None = None,
                 user_agent: str = USER_AGENT, session_id: str | None = None,
                 ledger=None, thinking_mode: str = "default",
                 transport_retries: int = 0) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("base URL must be credential-free HTTP(S) without query parameters or fragments")
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.user_agent = user_agent
        self.session_id = session_id or "codeatlas-" + uuid.uuid4().hex
        if thinking_mode not in {"default", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be default, enabled or disabled")
        if type(transport_retries) is not int or not 0 <= transport_retries <= 3:
            raise ValueError("transport_retries must be an integer from 0 to 3")
        self.thinking_mode = thinking_mode
        self.transport_retries = transport_retries
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise ValueError("User-Agent must be explicit and nonempty")
        if any(ch in value for value in (self.user_agent, self.session_id) for ch in "\r\n"):
            raise ValueError("request identity headers must not contain newlines")
        self.ledger = ledger
        self.trial_id = None
        self.last_usage: dict = {}
        self.last_response_meta: dict = {}
        self.last_latency_ms: float = 0.0
        self.usage_totals: dict[str, int] = {}
        self.usage_observations: dict[str, int] = {}
        self.request_count = 0
        self.max_requests = max_requests
        self.max_cost_usd = max_cost_usd
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        self.reserved_cost_usd = 0.0
        self.measured_cost_usd = 0.0

    def reset_metrics(self) -> None:
        """Reset process-local measurements; credentials are never part of them."""
        self.last_usage = {}
        self.last_response_meta = {}
        self.last_latency_ms = 0.0
        self.usage_totals = {}
        self.usage_observations = {}
        self.request_count = 0
        self.reserved_cost_usd = 0.0
        self.measured_cost_usd = 0.0

    def _chat(self, system: str, user: str, max_tokens: int = 1200, *,
              protected_user_prefix: str = "", json_object: bool = False) -> str:
        import urllib.request
        from ..contracts import budget_messages, OUTPUT_BUDGET, estimated_tokens, digest
        system, user, self.last_budget = budget_messages(
            system, user, protected_prefix=protected_user_prefix,
        )
        output_limit = min(max_tokens, OUTPUT_BUDGET)
        reservation = None
        if self.max_cost_usd is not None:
            if self.input_usd_per_million is None or self.output_usd_per_million is None:
                raise BudgetExceeded(
                    "cost ceiling requires LLM_INPUT_USD_PER_MILLION and LLM_OUTPUT_USD_PER_MILLION")
            reservation = (
                estimated_tokens(system + user) * self.input_usd_per_million
                + output_limit * self.output_usd_per_million
            ) / 1_000_000
        request_body = {
            "model": self.model, "max_tokens": output_limit, "temperature": 0.1,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self.thinking_mode != "default":
            request_body["thinking"] = {"type": self.thinking_mode}
        if json_object:
            request_body["response_format"] = {"type": "json_object"}
        body = json.dumps(request_body).encode()
        started = time.perf_counter()
        timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
        answer = None
        for attempt in range(self.transport_retries + 1):
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise BudgetExceeded("LLM_MAX_REQUESTS reached before the next request")
            if (self.max_cost_usd is not None and reservation is not None
                    and self.reserved_cost_usd + reservation > self.max_cost_usd + 1e-12):
                raise BudgetExceeded("LLM_MAX_COST_USD would be exceeded by the next request")
            request_id = None
            if self.ledger is not None:
                from .ledger import LedgerBudgetExceeded
                try:
                    request_id = self.ledger.reserve(
                        request_hash=digest(body), model=self.model, session_id=self.session_id,
                        input_tokens=self.last_budget["estimated_input_tokens"], output_tokens=output_limit,
                        trial_id=self.trial_id)
                except LedgerBudgetExceeded as exc:
                    raise BudgetExceeded(str(exc)) from exc
            self.request_count += 1  # Every wire attempt counts, including retryable failures.
            self.reserved_cost_usd += reservation or 0.0
            self.last_usage = {}
            self.last_response_meta = {}
            attempt_started = time.perf_counter()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}",
                         "User-Agent": self.user_agent,
                         "x-opencode-session": self.session_id})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    payload = json.loads(r.read())
                self.last_usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                choice = payload["choices"][0]
                answer = choice["message"]["content"]
                if not isinstance(answer, str):
                    raise ValueError("model response content must be text")
                self.last_response_meta = {
                    "model": payload.get("model"),
                    "finish_reason": choice.get("finish_reason"),
                    "transport_attempts": attempt + 1,
                }
                if choice.get("finish_reason") == "length":
                    raise OutputTruncated("model response reached the completion-token limit")
            except BaseException as raw_exc:
                exc = _classify_http_error(raw_exc) if isinstance(raw_exc, urllib.error.HTTPError) else raw_exc
                if request_id is not None:
                    self.ledger.finish(request_id, usage=self.last_usage, error_type=type(exc).__name__,
                                       latency_ms=(time.perf_counter() - attempt_started) * 1000)
                retryable = (
                    isinstance(exc, UpstreamHTTPError)
                    and not isinstance(exc, ProviderUsageLimitReached)
                    and exc.category in {"rate_limited", "upstream_timeout", "upstream_conflict", "upstream_http_error"}
                ) or isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                                      http.client.RemoteDisconnected))
                if retryable and attempt < self.transport_retries:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise exc
            if request_id is not None:
                self.ledger.finish(request_id, usage=self.last_usage,
                                   latency_ms=(time.perf_counter() - attempt_started) * 1000)
            break
        assert answer is not None
        self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
        for key, value in self.last_usage.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage_totals[key] = self.usage_totals.get(key, 0) + value
                self.usage_observations[key] = self.usage_observations.get(key, 0) + 1
        prompt = self.last_usage.get("prompt_tokens")
        completion = self.last_usage.get("completion_tokens")
        if (isinstance(prompt, int) and isinstance(completion, int)
                and self.input_usd_per_million is not None
                and self.output_usd_per_million is not None):
            self.measured_cost_usd += (
                prompt * self.input_usd_per_million
                + completion * self.output_usd_per_million
            ) / 1_000_000
        return answer

    @staticmethod
    def _json(raw: str) -> dict:
        text = raw.strip().lstrip("\ufeff")
        # A few OpenAI-compatible models still wrap a requested JSON object in
        # exactly one Markdown fence.  Accept that lossless representation;
        # prose, multiple objects and trailing content remain invalid and the
        # parsed action still passes the normal schema/tool allowlist checks.
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"}:
                raise json.JSONDecodeError("unsupported JSON fence", text, 0)
            text = "\n".join(lines[1:-1]).strip()
        decoder = json.JSONDecoder()
        parsed, end = decoder.raw_decode(text)
        rest = text[end:].strip()
        # Luna can occasionally repeat the exact same object back-to-back.
        # Treat only byte-semantic duplicates as one action; differing objects
        # remain ambiguous and are rejected before any tool can run.
        while rest:
            repeated, end = decoder.raw_decode(rest)
            if repeated != parsed:
                raise json.JSONDecodeError("multiple different JSON values", rest, 0)
            rest = rest[end:].strip()
        if not isinstance(parsed, dict):
            raise ValueError("模型 JSON 根节点必须是对象")
        return parsed

    def answer(self, system: str, q: str, context: str) -> str:
        # Reserve the complete question before trimming the potentially long
        # evidence.  A request with an oversized question fails before network I/O.
        prefix = f"问题：{q}\n\n证据：\n"
        return self._chat(
            system, prefix + context, protected_user_prefix=prefix,
        )

    def one_liner(self, ctx: dict) -> str:
        return self._chat(
            "用不超过30个中文字概括这个C函数的职责。只输出这一句话，不要任何前后缀。",
            f"函数名: {ctx['name']}\n签名: {ctx['sig']}\n调用: {', '.join(ctx['callees'][:10])}",
            max_tokens=80).strip().strip('"')

    def extract_experience(self, messages: list, schema: dict) -> dict:
        raw = self._chat(
            "从开发会话中抽取可复用的排障经验。只输出 JSON，不要 markdown 代码块。\n"
            f"schema: {json.dumps(schema, ensure_ascii=False)}",
            json.dumps(messages, ensure_ascii=False)[:12000], json_object=True)
        return self._json(raw)

    def next_action(self, query: str, events: list[dict], *, read_mode: str = "progressive") -> dict:
        """Plan one step; local code validates and executes every returned action."""
        wiki_available = not any(event.get("wiki_available") is False for event in events)
        wiki_required = any(event.get("wiki_required") is True for event in events)
        used = [event.get("tool") for event in events]
        if read_mode == "progressive" and wiki_available and wiki_required:
            if "search_evidence" not in used:
                allowed_next = ["search_evidence"]
            elif "wiki_outline" not in used:
                allowed_next = ["wiki_outline"]
            elif "wiki_section" not in used:
                allowed_next = ["wiki_section"]
            else:
                allowed_next = ["resolve_symbol", "code_read", "analyze_impact", "finish"]
        else:
            allowed_next = ["resolve_symbol", "code_read", "analyze_impact", "finish"]
        raw = self._chat(
            "你是受限代码诊断助手。只输出一个 JSON 对象，不要解释。"
            "必须从输入的 allowed_next 中选择下一动作。调用工具严格返回"
            "{\"action\":\"tool\",\"tool\":\"工具名\",\"arguments\":{...}}；"
            "结束严格返回 {\"action\":\"finish\"}，不得增加 reason 等字段。"
            "工具白名单只有 search_evidence(query)、resolve_symbol(symbol)、"
            "code_read(usr 或 path+line_start+line_end)、analyze_impact(symbol,depth)。" +
            ("另有 wiki_outline(module?)、wiki_section(page_id,section)。" if wiki_available
             else "当前没有可读 Wiki，请直接检索和核验源码，不要尝试 Wiki 工具。") +
            ("本题属于概念、功能或排障问题，必须先 search_evidence，再 wiki_outline、wiki_section，"
             "之后才允许 code_read/resolve_symbol/analyze_impact；定位、调用链和影响问题可直接走结构工具。"
             if read_mode == "progressive" and wiki_available and wiki_required
             else "本题可直接使用结构工具，按证据需要选择工具，不要求 Wiki-first。") +
            "code_read 的两种参数形式互斥：只能给 usr，或者只能给 path、line_start、line_end，绝不能两者都给。"
            "不要重复 prior_events 中已有的同一工具与参数；证据足够后立即 finish。"
            "禁止 shell、网络、写入、修改代码和审核知识卡。",
            json.dumps({"query": query, "allowed_next": allowed_next,
                        "prior_events": events}, ensure_ascii=False),
            max_tokens=800, json_object=True,
        )
        return self._json(raw)

    def next_baseline_action(self, query: str, events: list[dict], *,
                             fixed_skill: bool = False) -> dict:
        """Plan one A/B baseline step with the same iterative budget as CodeAtlas."""
        skill = ""
        if fixed_skill:
            skill = (
                "固定工作法：先判断任务类型和入口，再核对定义、调用关系、分支与错误路径；"
                "区分源码事实与推测，最后给出验证步骤。"
            )
        raw = self._chat(
            "你是受限代码诊断助手。只输出一个 JSON 对象，不要解释。"
            "调用工具严格返回 {\"action\":\"tool\",\"tool\":\"工具名\",\"arguments\":{...}}；"
            "结束严格返回 {\"action\":\"finish\"}，不得增加其他字段。"
            "工具白名单只有 repo_search(query) 与 "
            "code_read(path,line_start,line_end)，每次 code_read 最多 300 行。"
            "禁止 shell、网络、写入、修改代码和使用未提供的知识库。"
            "不得重复 prior_events 中已有的同一工具与参数；已有足够源码时立即 finish。" + skill,
            json.dumps({"query": query,
                        "allowed_next": ["repo_search", "code_read", "finish"],
                        "prior_events": events}, ensure_ascii=False),
            max_tokens=800, json_object=True,
        )
        return self._json(raw)

    def plan_tool(self, query: str, events: list[dict]) -> dict:
        """Compatibility alias for pre-v2 callers."""
        return self.next_action(query, events)

    def extract_knowledge_card(self, messages: list, schema: dict) -> dict:
        """Extract a review candidate only; the caller keeps it pending until review."""
        raw = self._chat(
            "从公开合成开发会话提取待审核知识卡。只能复述会话已有事实；"
            "没有证据时在 root_cause 写“证据不足”。只输出 JSON，不要代码块。\n"
            f"schema: {json.dumps(schema, ensure_ascii=False)}",
            json.dumps(messages, ensure_ascii=False)[:12000],
            json_object=True,
        )
        return self._json(raw)


def get_client(*, ledger=None, session_id=None, base_url=None, model=None,
               thinking_mode=None, transport_retries=None):
    # Use a project-specific variable so an unrelated shell-wide OpenAI key
    # cannot accidentally turn a deterministic proof run into a paid run.
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    def number(name, cast):
        raw = os.environ.get(name)
        if raw in (None, ""):
            return None
        value = cast(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive")
        return value
    go = os.environ.get("LLM_PROVIDER") == "opencode-go"
    retries = (int(os.environ.get("LLM_TRANSPORT_RETRIES", "0"))
               if transport_retries is None else transport_retries)
    return OpenAICompatClient(
        base_url or os.environ.get("LLM_BASE_URL", GO_BASE_URL if go else "https://api.openai.com/v1"), key,
        model or os.environ.get("LLM_MODEL", GO_MODEL if go else "gpt-4o-mini"),
        max_requests=number("LLM_MAX_REQUESTS", int),
        max_cost_usd=number("LLM_MAX_COST_USD", float),
        input_usd_per_million=number("LLM_INPUT_USD_PER_MILLION", float),
        output_usd_per_million=number("LLM_OUTPUT_USD_PER_MILLION", float),
        user_agent=os.environ.get("LLM_USER_AGENT", USER_AGENT),
        session_id=session_id or os.environ.get("LLM_SESSION_ID"), ledger=ledger,
        thinking_mode=thinking_mode or os.environ.get("LLM_THINKING_MODE", "default"),
        transport_retries=retries,
    )


def capabilities() -> dict:
    """Return display-safe model configuration; never expose credentials."""
    client = get_client()
    go = os.environ.get("LLM_PROVIDER") == "opencode-go"
    base_url = os.environ.get("LLM_BASE_URL", GO_BASE_URL if go else "https://api.openai.com/v1")
    parsed = urlsplit(base_url)
    safe_endpoint = f"{parsed.scheme}://{parsed.hostname}" if parsed.scheme and parsed.hostname else "custom"
    return {
        "configured": client is not None,
        "provider": "opencode-go" if go else "openai-compatible",
        "model": client.model if client else os.environ.get("LLM_MODEL", GO_MODEL if go else "gpt-4o-mini"),
        "endpoint": safe_endpoint,
        "timeout_seconds": float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
        "max_requests": client.max_requests if client else None,
        "max_cost_usd": client.max_cost_usd if client else None,
        "pricing_configured": bool(client and client.input_usd_per_million is not None
                                   and client.output_usd_per_million is not None),
        "key_source": "environment" if client else None,
    }
