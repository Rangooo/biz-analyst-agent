"""
LLM 统一客户端 —— OpenAI 兼容层 + 主备 fallback + 角色路由。
设计借鉴 credit-intel agent/base_client.py：统一接口、容错、结构化输出校验。

关键能力：
1. 多 provider（DeepSeek/welm/OpenAI）统一走 OpenAI 格式
2. 角色路由：analyst（主分析）与 red_team（红队）可指向不同模型，实现双模型对抗
3. 主备 fallback：首选 provider 失败自动降级
4. JSON 结构化输出 + 解析容错
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

_CONFIG_PATH = Path(__file__).parent / "providers.yaml"


class LLMError(RuntimeError):
    pass


class TokenTracker:
    """P1.6: Token 成本追踪——按 provider/role/stage 累计 token 用量与估算成本。"""
    # 粗略定价（$/1M tokens），可根据实际调整
    PRICING = {
        "deepseek": {"input": 0.27, "output": 1.10},
        "openai": {"input": 2.50, "output": 10.00},
        "anthropic": {"input": 3.00, "output": 15.00},
        "google": {"input": 1.25, "output": 5.00},
        "default": {"input": 0.50, "output": 1.50},
    }

    def __init__(self):
        self._records: list[dict] = []

    def record(self, provider: str, role: str, stage: str,
               input_tokens: int, output_tokens: int):
        if input_tokens <= 0 and output_tokens <= 0:
            return
        family = "default"
        for key in self.PRICING:
            if key in provider.lower():
                family = key
                break
        pricing = self.PRICING[family]
        cost = (input_tokens / 1_000_000 * pricing["input"] +
                output_tokens / 1_000_000 * pricing["output"])
        self._records.append({
            "provider": provider, "role": role, "stage": stage,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": round(cost, 4),
        })

    def summary(self) -> dict:
        if not self._records:
            return {
                "total_calls": 0,
                "total_input": 0,
                "total_output": 0,
                "total_cost_usd": 0,
                "by_stage": {},
                "by_role": {},
                "by_provider": {},
            }
        total_in = sum(r["input_tokens"] for r in self._records)
        total_out = sum(r["output_tokens"] for r in self._records)
        total_cost = sum(r["cost_usd"] for r in self._records)

        def add(bucket: dict[str, dict], key: str, record: dict) -> None:
            if key not in bucket:
                bucket[key] = {"calls": 0, "input": 0, "output": 0, "cost_usd": 0}
            bucket[key]["calls"] += 1
            bucket[key]["input"] += record["input_tokens"]
            bucket[key]["output"] += record["output_tokens"]
            bucket[key]["cost_usd"] = round(bucket[key]["cost_usd"] + record["cost_usd"], 4)

        by_stage: dict[str, dict] = {}
        by_role: dict[str, dict] = {}
        by_provider: dict[str, dict] = {}
        for r in self._records:
            add(by_stage, r["stage"], r)
            add(by_role, r["role"], r)
            add(by_provider, r["provider"], r)
        return {
            "total_calls": len(self._records),
            "total_input": total_in, "total_output": total_out,
            "total_cost_usd": round(total_cost, 4),
            "by_stage": by_stage,
            "by_role": by_role,
            "by_provider": by_provider,
        }


class LLMClient:
    def __init__(self, config_path: Path | str = _CONFIG_PATH):
        with open(config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        # 合并本地私有配置 providers.local.yaml（gitignore，不随仓库共享；如内网代理/自部署）
        local_path = Path(config_path).parent / "providers.local.yaml"
        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
            (self.cfg.setdefault("providers", {})).update(local.get("providers", {}))
            if local.get("fallback_order"):
                self.cfg["fallback_order"] = local["fallback_order"]
        self.providers: dict[str, dict] = self.cfg["providers"]
        self.fallback_order: list[str] = self.cfg.get("fallback_order", list(self.providers))
        self.role_defaults: dict[str, str] = self.cfg.get("role_defaults", {}) or {}
        self.token_tracker = TokenTracker()
        self._current_stage = "unknown"
        self._current_role = "analyst"
        self._auto_assign_roles()

    def _available_keys(self) -> set[str]:
        return {k for k in self.providers if self._has_key(k)}

    def _auto_assign_roles(self):
        """role_defaults 为空时，按可用模型的 tier/family 自动指派三角色。"""
        if self.role_defaults:
            return  # 已显式配置则尊重
        try:
            from llm.model_catalog import auto_assign_roles
            avail = self._available_keys()
            if avail:
                r = auto_assign_roles(self.providers, avail)
                self.role_defaults = {
                    "analyst": r["analyst"], "red_team": r["red_team"] or r["analyst"],
                    "reviewer": r["reviewer"] or r["analyst"],
                }
                self._auto_reason = r.get("reason", "")
            else:
                self._auto_reason = "无可用 LLM"
        except Exception:  # noqa: BLE001
            self._auto_reason = ""

    def auto_role_reason(self) -> str:
        return getattr(self, "_auto_reason", "")

    def role_plan(self) -> dict:
        """返回当前自动/显式角色指派（供前端展示）。"""
        return {
            "analyst": self.effective_provider("analyst"),
            "red_team": self.effective_provider("red_team"),
            "reviewer": self.effective_provider("reviewer"),
            "same_source": self.is_same_source_review(None),
            "reason": self.auto_role_reason(),
        }

    # ---------- provider 选择 ----------
    def primary_provider(self) -> str:
        """主 provider：is_primary 标记者优先，否则取 fallback 首个。供 analyze 默认指派。"""
        for k, p in self.providers.items():
            if p.get("is_primary"):
                return k
        return self.fallback_order[0] if self.fallback_order else next(iter(self.providers))

    def list_available(self) -> list[dict]:
        """返回所有 provider（供前端下拉/配置面板），含 family/tier/reasoning 元信息。"""
        try:
            from llm.model_catalog import _meta
            metas = {m["key"]: m for m in _meta(self.providers)}
        except Exception:  # noqa: BLE001
            metas = {}
        out = []
        for key, p in self.providers.items():
            m = metas.get(key, {})
            out.append({
                "key": key,
                "label": p["label"],
                "model": p["model"],
                "is_primary": p.get("is_primary", False),
                "available": self._has_key(key),
                "family": m.get("family", p.get("family", "custom")),
                "tier": m.get("tier", p.get("tier", 3)),
                "reasoning": m.get("reasoning", p.get("reasoning", False)),
            })
        return out

    def _has_key(self, key: str) -> bool:
        p = self.providers.get(key)
        if not p or not os.getenv(p["api_key_env"]):
            return False
        # 额外鉴权头（如内网代理的自定义 header）也必须齐备，否则视为不可用
        for envvar in (p.get("extra_headers") or {}).values():
            if not os.getenv(envvar):
                return False
        return True

    def _resolve(self, provider: Optional[str], role: str) -> list[str]:
        """返回按优先级排列的候选 provider 列表（含 fallback）。"""
        candidates: list[str] = []
        if provider and provider in self.providers:
            candidates.append(provider)
        else:
            default = self.role_defaults.get(role)
            if default:
                candidates.append(default)
        for k in self.fallback_order:
            if k not in candidates:
                candidates.append(k)
        # 只保留有 key 的
        return [k for k in candidates if self._has_key(k)]

    def effective_provider(self, role: str, provider: Optional[str] = None) -> Optional[str]:
        """返回该角色实际会用的第一个 provider（用于降级检测与前端展示）。"""
        cands = self._resolve(provider, role)
        return cands[0] if cands else None

    def is_same_source_review(self, analyst_provider: Optional[str] = None) -> bool:
        """是否处于同源审查降级：analyst 与 red_team 指向同一 provider，或任一缺失。"""
        a = self.effective_provider("analyst", analyst_provider)
        r = self.effective_provider("red_team", None)
        if not a or not r:
            return True
        return a == r

    # ---------- 核心调用 ----------
    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """可重试的瞬时错误：超时、连接、5xx、429 限流。4xx 鉴权类不重试。"""
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code == 429 or code >= 500
        return False

    def chat(
        self,
        messages: list[dict],
        *,
        provider: Optional[str] = None,
        role: str = "analyst",
        temperature: float = 0.4,
        max_tokens: int = 8192,
        timeout: float = 120.0,
        retries: int = 2,
    ) -> str:
        """同步调用，返回文本。瞬时错误按 backoff 重试，仍失败再降级到下一个 provider。"""
        self._current_role = role
        candidates = self._resolve(provider, role)
        if not candidates:
            raise LLMError(
                f"没有可用的 LLM provider（role={role}）。请在 .env 配置至少一个 API Key。"
            )
        last_err: Optional[Exception] = None
        for key in candidates:
            p = self.providers[key]
            for attempt in range(retries + 1):
                try:
                    return self._call_one(p, messages, temperature, max_tokens, timeout)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if self._is_transient(e) and attempt < retries:
                        time.sleep(1.5 * (attempt + 1))  # 退避：1.5s、3s
                        continue
                    break  # 非瞬时或重试用完 → 换下一个 provider
        raise LLMError(f"所有候选 provider 调用失败（已重试 {retries} 次）：{last_err}")

    def set_stage(self, stage: str):
        """P1.6: 设置当前流水线阶段，供 token 追踪归类。"""
        self._current_stage = stage

    def _call_one(self, p, messages, temperature, max_tokens, timeout) -> str:
        api_key = os.getenv(p["api_key_env"], "")
        url = p["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": p["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # 额外鉴权头（如内网代理的 Platform-User/Token/Business），值为环境变量名
        for h, envvar in (p.get("extra_headers") or {}).items():
            val = os.getenv(envvar)
            if val:
                headers[h] = val
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        # P1.6: 记录 token 用量
        usage = data.get("usage", {})
        if usage:
            self.token_tracker.record(
                p.get("model", "unknown"), self._current_role, self._current_stage,
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
            )
        msg = data["choices"][0]["message"]
        # 推理模型(R1/GLM-5)在 max_tokens 不足时 content 可能为 None（token 全用于 reasoning_content）
        content = msg.get("content")
        if not content:
            content = msg.get("reasoning_content") or ""
        return content

    # ---------- 结构化输出 ----------
    def chat_json(
        self,
        messages: list[dict],
        *,
        provider: Optional[str] = None,
        role: str = "analyst",
        temperature: float = 0.3,
        max_tokens: int = 8192,
    ) -> Any:
        """要求模型输出 JSON 并解析，带容错。"""
        text = self.chat(
            messages, provider=provider, role=role,
            temperature=temperature, max_tokens=max_tokens,
        )
        return self._extract_json(text)

    @staticmethod
    def _extract_json(text: str) -> Any:
        # 去掉 ```json ... ``` 包裹
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else text
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 尝试截取首个 { 或 [ 到末尾
            for opener, closer in (("{", "}"), ("[", "]")):
                start = candidate.find(opener)
                end = candidate.rfind(closer)
                if start != -1 and end != -1 and end > start:
                    try:
                        return json.loads(candidate[start : end + 1])
                    except json.JSONDecodeError:
                        continue
            salvaged = LLMClient._salvage_partial_json(candidate)
            if salvaged is not None:
                return salvaged
            raise LLMError(f"无法解析 LLM JSON 输出：{text[:300]}")

    @staticmethod
    def _salvage_partial_json(text: str) -> Any | None:
        """Recover complete items from a truncated top-level JSON list field.

        Long structured generations often fail by cutting off the tail of
        {"insights": [...]}. Throwing the whole result away makes the pipeline
        look stuck even when several insight objects are already complete.
        """
        m = re.search(r'"(?P<key>insights|queries|support_queries|debate_queries)"\s*:\s*\[', text)
        if not m:
            return None
        key = m.group("key")
        pos = m.end()
        items = []
        i = pos
        while i < len(text):
            while i < len(text) and text[i] not in "{[\"":
                i += 1
            if i >= len(text):
                break
            start = i
            opener = text[i]
            closer = "}" if opener == "{" else "]" if opener == "[" else None
            if opener == '"':
                i += 1
                escaped = False
                while i < len(text):
                    ch = text[i]
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        raw = text[start:i + 1]
                        try:
                            items.append(json.loads(raw))
                        except json.JSONDecodeError:
                            return {key: items} if items else None
                        i += 1
                        break
                    i += 1
                else:
                    break
                continue

            depth = 0
            in_string = False
            escaped = False
            while i < len(text):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == opener:
                        depth += 1
                    elif ch == closer:
                        depth -= 1
                        if depth == 0:
                            raw = text[start:i + 1]
                            try:
                                items.append(json.loads(raw))
                            except json.JSONDecodeError:
                                return {key: items} if items else None
                            i += 1
                            break
                i += 1
            else:
                break

        if not items:
            return None
        return {key: items}


# 单例
_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
