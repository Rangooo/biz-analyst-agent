"""
可插拔金融数据源适配层 —— 财报只是来源之一。

统一接口：每个 adapter 提供 search(query, kind) -> list[EvidenceLike]
适配器：
  - TavilySerperAdapter : 通用搜索（新闻/媒体/研报入口）
  - SecEdgarAdapter     : SEC EDGAR 官方财报（美股）
  - NeoDataAdapter      : NeoData 金融搜索（A股/港股/美股行情财报+公告研报）
  - IfindAdapter        : 同花顺 iFinD（股票/基金/宏观/新闻公告）

启用哪些由配置决定（环境变量 / providers 配置）。单源可用即降级运行，不崩溃。
NeoData / iFinD 通过其 skill 脚本调用（脚本路径可配），外部用户也可改为直接 HTTP。
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import httpx

from schemas import Evidence, SourceTier

# 扩展脚本路径：优先读环境变量，默认探测 ~/.workbuddy/skills/ 下的标准位置。
# 独立部署用户可在 .env 中设置 NEODATA_SCRIPT / IFIND_SCRIPT 覆盖。
_SKILLS_DIR = os.path.expanduser("~/.workbuddy/skills")
_NEODATA_SCRIPT = os.getenv(
    "NEODATA_SCRIPT",
    os.path.join(_SKILLS_DIR, "skill_2053082432761950208", "scripts", "query.py"),
)
_IFIND_SCRIPT = os.getenv(
    "IFIND_SCRIPT",
    os.path.join(_SKILLS_DIR, "ifind-finance-data", "call-node.js"),
)
# iFinD 直跑会被 skill 守卫拦截，需用 wrapper require 后 call()
_IFIND_WRAPPER = os.getenv(
    "IFIND_WRAPPER",
    str(Path(__file__).parent / "ifind_call.js"),
)
# Python 解释器：优先用当前运行的解释器（venv 内），其次找系统 python3
_PYBIN = os.getenv("PYBIN", sys.executable or shutil.which("python3") or "python3")
# Node 解释器：优先环境变量，其次 PATH 中的 node
_NODE = os.getenv("NODEBIN", shutil.which("node") or "node")
# Wind MCP：AIFin Market / Wind 金融数据。具体 CLI/schema 由部署环境决定，
# 因此用命令模板适配；支持 {query}/{kind}/{max_results} 占位。


def _neodata_script() -> str:
    return os.getenv("NEODATA_SCRIPT", _NEODATA_SCRIPT)


def _ifind_script() -> str:
    return os.getenv("IFIND_SCRIPT", _IFIND_SCRIPT)


def _ifind_wrapper() -> str:
    return os.getenv("IFIND_WRAPPER", _IFIND_WRAPPER)


def _pybin() -> str:
    return os.getenv("PYBIN", _PYBIN)


def _nodebin() -> str:
    return os.getenv("NODEBIN", _NODE)


@dataclass
class SourceResult:
    """适配器统一返回。"""

    evidences: list[Evidence] = field(default_factory=list)
    structured: list[dict] = field(default_factory=list)  # 结构化数据（行情/财务指标等）
    meta: dict = field(default_factory=dict)


class BaseAdapter:
    name: str = "base"
    available: bool = False

    def search(self, query: str, kind: str = "general", max_results: int = 5,
               days: int | None = None) -> SourceResult:
        """days: 限定时效（仅返回最近 N 天内的结果），None=不过滤。"""
        raise NotImplementedError


# ---------------- 通用搜索 (Tavily/Serper) ----------------
class TavilySerperAdapter(BaseAdapter):
    name = "general_search"

    def __init__(self):
        self.available = bool(os.getenv("TAVILY_API_KEY") or os.getenv("SERPER_API_KEY"))

    def search(self, query, kind="general", max_results=5, days=None):
        from tools.search import search as _s
        rows = _s(query, max_results, days=days)
        evs = []
        for r in rows:
            evs.append(Evidence(
                content=r.get("content", "")[:400],
                source_url=r.get("url", ""), source_title=r.get("title", ""),
                source_type="web", tier=SourceTier.MEDIA,
            ))
        return SourceResult(evidences=evs)


# ---------------- SEC EDGAR ----------------
class SecEdgarAdapter(BaseAdapter):
    name = "sec_edgar"

    def __init__(self):
        self.available = True  # SEC 全免费，始终可用（美股）

    def search(self, query, kind="filing", max_results=5, days=None):
        # query 期望是 ticker（SEC 财报不受 days 时效过滤）
        from tools.sec_edgar import get_key_financials
        ticker = query.strip().upper()
        if not ticker:
            return SourceResult()
        fin = get_key_financials(ticker)
        cik = fin.get("cik", "")
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?CIK={cik}"
        alias_cn = {"Revenue": "营收", "NetIncome": "净利润",
                    "GrossProfit": "毛利", "OperatingIncome": "经营利润"}
        evs = []
        for alias, rows in (fin.get("concepts") or {}).items():
            if not rows:
                continue
            label = alias_cn.get(alias, alias)
            # 年度与季度分开成两条 evidence——避免单季值与全年值混排误读
            for ptype, ptype_cn in (("annual", "年度"), ("quarter", "季度")):
                seg = [r for r in rows if r.get("period_type") == ptype and r.get("val") is not None]
                if not seg:
                    continue
                parts = []
                for r in seg:
                    val = r.get("val")
                    val_str = f"{val:,}" if isinstance(val, (int, float)) else str(val)
                    fp = r.get("fp", "")
                    parts.append(f"{r.get('fy')}{' '+fp if fp else ''}({r.get('form')}): "
                                 f"{val_str} USD 截至{r.get('end', '')}")
                content = f"{label}（{ptype_cn}, 近{len(seg)}期）: " + " | ".join(parts)
                evs.append(Evidence(
                    content=content, source_url=url,
                    source_title="SEC EDGAR", source_type="filing",
                    tier=SourceTier.FILING, as_of=str(seg[0].get("end") or ""),
                ))
        return SourceResult(evidences=evs, structured=fin.get("concepts") or [])


# ---------------- NeoData 金融搜索 ----------------
class NeoDataAdapter(BaseAdapter):
    """NeoData：A股/港股/美股行情财报 + 公告 + 研报。
    通过 skill 脚本调用（自动管理凭证）。kind: financial/news/notice/research"""
    name = "neodata"

    def __init__(self):
        self.script = _neodata_script()
        self.available = Path(self.script).exists()

    def search(self, query, kind="financial", max_results=5, days=None):
        if not self.available:
            return SourceResult()
        try:
            proc = subprocess.run(
                [_pybin(), self.script, "--query", query],
                capture_output=True, text=True, timeout=40,
            )
            if proc.returncode != 0:
                return SourceResult(meta={"error": proc.stderr[:200]})
            data = json.loads(proc.stdout)
        except Exception as e:  # noqa: BLE001
            return SourceResult(meta={"error": str(e)})

        evs = []
        d = data.get("data", {}) if isinstance(data, dict) else {}
        # 结构化 API 数据
        for block in (d.get("apiData") or {}).get("apiRecall", []):
            content = block.get("desc", "") + " " + json.dumps(block.get("content", ""), ensure_ascii=False)[:300]
            evs.append(Evidence(
                content=content[:400], source_url="https://copilot.tencent.com/neodata",
                source_title=f"NeoData·{block.get('type','')}", source_type="financial_api",
                tier=SourceTier.THIRD_PARTY,
            ))
        # 文档类（资讯/公告/研报）
        for group in (d.get("docData") or {}).get("docRecall", []):
            for doc in (group.get("docList") or [])[:max_results]:
                t = (doc.get("publishTime") or doc.get("date") or "")[:10]
                evs.append(Evidence(
                    content=(doc.get("title", "") + " " + doc.get("summary", doc.get("content", "")))[:400],
                    source_url=doc.get("url", ""), source_title=doc.get("title", ""),
                    source_type=kind, published_at=t,
                    tier=SourceTier.MEDIA if kind == "news" else SourceTier.COMPANY_PR,
                ))
        return SourceResult(evidences=evs)


# ---------------- iFinD 同花顺 ----------------
class IfindAdapter(BaseAdapter):
    """iFinD：股票/基金/宏观/新闻公告。通过 skill 的 call-node.js（需 wrapper require 后 call）。
    kind: stock/fund/edb/news/notice"""
    name = "ifind"

    def __init__(self):
        self.script = _ifind_script()
        self.wrapper = _ifind_wrapper()
        self.available = Path(self.script).exists() and Path(self.wrapper).exists()

    def search(self, query, kind="news", max_results=5, days=None):
        if not self.available:
            return SourceResult()
        tool = "search_" + kind if kind in ("news", "notice") else "get_stock_summary"
        # iFinD 资讯检索需 time_start/time_end（近 2 年）
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=730)
        params = {"query": query, "max_results": max_results,
                  "time_start": start.strftime("%Y-%m-%d"), "time_end": end.strftime("%Y-%m-%d")}
        try:
            proc = subprocess.run(
                [_nodebin(), self.wrapper, kind, tool, json.dumps(params, ensure_ascii=False)],
                capture_output=True, text=True, timeout=45,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return SourceResult(meta={"error": (proc.stderr or "空输出")[:200]})
            data = json.loads(proc.stdout)
        except Exception as e:  # noqa: BLE001
            return SourceResult(meta={"error": str(e)})
        if isinstance(data, dict) and data.get("ok") is False:
            return SourceResult(meta={"error": str(data.get("error", ""))[:200]})

        # iFinD 嵌套很深：data.result.content[].text(JSON串) → .data.data(JSON串) → [{资讯标题,资讯内容,...}]
        evs = []
        items: list = []
        try:
            d = data.get("data", {}) if isinstance(data, dict) else {}
            content = (d.get("result") or {}).get("content") or []
            for blk in content:
                txt = blk.get("text", "") if isinstance(blk, dict) else str(blk)
                inner = json.loads(txt) if isinstance(txt, str) and txt.strip().startswith("{") else None
                if isinstance(inner, dict):
                    inner_data = inner.get("data", {}).get("data", inner.get("data"))
                    if isinstance(inner_data, str):
                        try:
                            inner_data = json.loads(inner_data)
                        except Exception:  # noqa: BLE001
                            inner_data = []
                    if isinstance(inner_data, list):
                        items.extend(inner_data)
                    elif isinstance(inner_data, dict):
                        items.extend(inner_data.get("list") or inner_data.get("data") or [])
        except Exception:  # noqa: BLE001
            items = []
        for it in (items or [])[:max_results]:
            if not isinstance(it, dict):
                continue
            title = it.get("资讯标题") or it.get("title") or ""
            body = it.get("资讯内容") or it.get("summary") or it.get("content") or ""
            t = (it.get("资讯发布时间") or it.get("publishTime") or it.get("date") or "")[:10]
            url = it.get("资讯链接") or it.get("url") or it.get("urlWexin") or ""
            evs.append(Evidence(
                content=(title + " " + body)[:400], source_url=url,
                source_title=title, source_type=kind, published_at=t,
                tier=SourceTier.COMPANY_PR if kind == "notice" else SourceTier.MEDIA,
            ))
        return SourceResult(evidences=evs)


class WindMcpAdapter(BaseAdapter):
    """Wind MCP / AIFin Market 金融数据源。

    推荐方式：先在 AIFin Market / mcporter 中配置 Wind MCP server，然后通过
    WIND_MCP_SERVER + WIND_MCP_TOOL 指定要调用的工具。

    示例：
      WIND_MCP_SERVER=wind
      WIND_MCP_TOOL=financial_docs.get_financial_news
      WIND_MCP_QUERY_ARG=question
      WIND_MCP_LIMIT_ARG=top_k

    逃生口：如果有一条能直接返回 JSON 的一次性 CLI，可用 WIND_MCP_COMMAND
    命令模板；支持 {query}/{kind}/{max_results} 占位。例如：
      WIND_MCP_COMMAND=wind-mcp search --query "{query}" --kind "{kind}" --limit {max_results}
    """
    name = "wind"

    def __init__(self):
        self.command_template = os.getenv("WIND_MCP_COMMAND", "").strip()
        self._mcporter = shutil.which("mcporter") or ""
        self.server = os.getenv("WIND_MCP_SERVER", "wind").strip()
        self.tool = os.getenv("WIND_MCP_TOOL", "").strip()
        self.query_arg = os.getenv("WIND_MCP_QUERY_ARG", "query").strip() or "query"
        self.kind_arg = os.getenv("WIND_MCP_KIND_ARG", "").strip()
        self.limit_arg = os.getenv("WIND_MCP_LIMIT_ARG", "maxResults").strip() or "maxResults"
        self.available = bool(self.command_template or (self._mcporter and self.server and self.tool))

    def search(self, query, kind="financial", max_results=5, days=None):
        if not self.available:
            return SourceResult()
        try:
            cmd = self._build_command(query, kind, max_results)
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return SourceResult(meta={"error": (proc.stderr or "Wind MCP 空输出")[:300]})
            data = json.loads(proc.stdout)
        except Exception as e:  # noqa: BLE001
            return SourceResult(meta={"error": str(e)})
        return self._parse_result(data, kind=kind, max_results=max_results)

    def _build_command(self, query: str, kind: str, max_results: int) -> list[str]:
        repl = {
            "{query}": str(query or ""),
            "{kind}": str(kind or "financial"),
            "{max_results}": str(max_results),
        }
        raw = self.command_template
        if not raw:
            selector = f"{self.server}.{self.tool}"
            cmd = [
                self._mcporter,
                "call",
                selector,
                f"{self.query_arg}={query or ''}",
                f"{self.limit_arg}={max_results}",
                "--output",
                "json",
            ]
            if self.kind_arg:
                cmd.insert(4, f"{self.kind_arg}={kind or 'financial'}")
            return cmd
        if raw.lstrip().startswith("["):
            parts = json.loads(raw)
            if not isinstance(parts, list):
                raise ValueError("WIND_MCP_COMMAND JSON must be an array")
            tokens = [str(p) for p in parts]
        else:
            tokens = shlex.split(raw, posix=(os.name != "nt"))
        return [self._replace_tokens(t, repl) for t in tokens]

    @staticmethod
    def _replace_tokens(text: str, repl: dict[str, str]) -> str:
        for k, v in repl.items():
            text = text.replace(k, v)
        return text

    def _parse_result(self, data, kind="financial", max_results=5):
        items = self._flatten_items(data)
        evs: list[Evidence] = []
        structured: list[dict] = []
        for item in items[:max_results]:
            if isinstance(item, dict):
                structured.append(item)
                title = (
                    item.get("title") or item.get("name") or item.get("资讯标题")
                    or item.get("证券简称") or item.get("指标名称") or "Wind MCP"
                )
                body = (
                    item.get("summary") or item.get("content") or item.get("text")
                    or item.get("资讯内容") or item.get("desc") or json.dumps(item, ensure_ascii=False)
                )
                url = item.get("url") or item.get("link") or item.get("资讯链接") or "https://aifinmarket.wind.com.cn/#/home"
                t = (item.get("publishTime") or item.get("date") or item.get("as_of")
                     or item.get("报告期") or item.get("资讯发布时间") or "")
                source_type = "financial_api" if kind in ("financial", "stock", "edb", "macro") else kind
                evs.append(Evidence(
                    content=str(body)[:400],
                    source_url=str(url),
                    source_title=str(title)[:100],
                    source_type=source_type,
                    tier=SourceTier.THIRD_PARTY,
                    published_at=str(t)[:10] if t else "",
                ))
            elif str(item).strip():
                evs.append(Evidence(
                    content=str(item)[:400],
                    source_url="https://aifinmarket.wind.com.cn/#/home",
                    source_title="Wind MCP",
                    source_type="financial_api",
                    tier=SourceTier.THIRD_PARTY,
                ))
        return SourceResult(evidences=evs, structured=structured, meta={"raw_count": len(items)})

    def _flatten_items(self, data) -> list:
        if data is None:
            return []
        if isinstance(data, list):
            out: list = []
            for x in data:
                out.extend(self._flatten_items(x))
            return out
        if isinstance(data, str):
            s = data.strip()
            if not s:
                return []
            if s.startswith("{") or s.startswith("["):
                try:
                    return self._flatten_items(json.loads(s))
                except Exception:  # noqa: BLE001
                    pass
            return [s]
        if isinstance(data, dict):
            if "content" in data and isinstance(data["content"], list):
                out: list = []
                for block in data["content"]:
                    if isinstance(block, dict) and "text" in block:
                        out.extend(self._flatten_items(block["text"]))
                    else:
                        out.extend(self._flatten_items(block))
                return out
            for key in ("data", "result", "items", "list", "records", "docs", "news", "rows"):
                val = data.get(key)
                if isinstance(val, (list, dict, str)):
                    nested = self._flatten_items(val)
                    if nested:
                        return nested
            return [data]
        return [data]


# ---------------- 东方财富资讯（内置 HTTP 直调，免费无限制） ----------------
class MXFinSearchAdapter(BaseAdapter):
    """东方财富金融资讯检索（新闻/公告）。
    直接调用东方财富公开 JSONP 搜索接口，无需 API Key，无额度限制。
    作为 A 股/中文金融新闻的免费补充数据源。"""
    name = "em_news"

    _SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.eastmoney.com/",
    }

    def __init__(self):
        # 内置 HTTP 调用，始终可用
        self.available = True

    def search(self, query, kind="news", max_results=5, days=None):
        import re
        import time
        cb_name = f"jQuery{''.join([str(x) for x in [3, 5, 1, 0, 8, 7, 5]])}_{ int(time.time() * 1000)}"
        param_obj = {
            "uid": "",
            "keyword": query,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": max(max_results, 10),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        params = {
            "cb": cb_name,
            "param": json.dumps(param_obj, ensure_ascii=False),
            "_": str(int(time.time() * 1000)),
        }
        try:
            with httpx.Client(timeout=20, headers=self._HEADERS) as client:
                resp = client.get(self._SEARCH_URL, params=params)
                resp.raise_for_status()
                text = resp.text
        except Exception as e:  # noqa: BLE001
            logger.warning("东方财富搜索请求失败: %s", e, exc_info=True)
            return SourceResult(meta={"error": str(e)})

        # 剥离 JSONP 包装：callback_name({...})
        try:
            json_str = re.sub(r"^[^(]+\(", "", text.rstrip().rstrip(";"))
            json_str = json_str.rsplit(")", 1)[0]
            data = json.loads(json_str)
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning("东方财富 JSONP 解析失败: %s", e)
            return SourceResult(meta={"error": f"JSONP parse: {e}"})

        # 提取文章列表
        items = []
        try:
            result = data.get("result", {})
            articles = result.get("cmsArticleWebOld", [])
            if isinstance(articles, list):
                items = articles
        except Exception:  # noqa: BLE001
            pass

        # 相关性过滤：提取查询核心关键词，过滤掉标题+内容中完全不含关键词的结果
        # 东方财富搜索在无精确匹配时会返回模糊兜底结果（如搜"小红书"返回"ST明德"）
        _query_kw = re.sub(r"\s+\d{4}年?|\s+最新|\s+近期|\s+趋势", "", query).strip()
        # 取查询中最核心的名称词（第一个空格分隔的词，通常是公司/品牌名）
        _core_names = [w for w in _query_kw.split() if len(w) >= 2][:3]

        evs = []
        for it in items[:max_results * 2]:  # 多取一些，过滤后可能不够
            if not isinstance(it, dict):
                continue
            title = re.sub(r"<[^>]+>", "", it.get("title", ""))  # 去 HTML 标签
            content = re.sub(r"<[^>]+>", "", it.get("content", it.get("summary", "")))
            # 相关性检查：标题或内容中必须包含至少一个核心关键词
            if _core_names:
                combined = title + " " + content
                if not any(kw in combined for kw in _core_names):
                    logger.debug("em_news 过滤不相关结果: %s (查询: %s)", title[:40], query)
                    continue
            url = it.get("url", it.get("link", ""))
            pub_date = (it.get("date") or it.get("publishTime") or it.get("ctime") or "")[:10]
            evs.append(Evidence(
                content=(title + " " + content)[:400],
                source_url=url,
                source_title=title[:80] or "东方财富资讯",
                source_type=kind, published_at=pub_date,
                tier=SourceTier.MEDIA,
            ))
            if len(evs) >= max_results:
                break
        return SourceResult(evidences=evs)


# ---------------- 港股披露易 (HKEX) ----------------
class HkexDisclosureAdapter(BaseAdapter):
    """港股披露易适配器。

    香港联交所披露易（HKEX disclosure）有公开搜索接口，
    可以搜上市公司公告（财报、业绩预告、IPO等）。
    返回 tier=FILING 级证据。
    """
    name = "hkex_disclosure"
    _SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    def __init__(self):
        # 内置 HTTP 调用，始终可用
        self.available = True

    def search(self, query, kind="news", max_results=5, days=None):
        import re
        # 从 query 中提取股票代码或公司名
        # 港股代码格式：5位数字（如 09988）
        ticker_match = re.search(r"\b(0\d{4})\b", query)
        company_name = query.strip()
        stock_code = ticker_match.group(1) if ticker_match else ""

        # 用东方财富港股公告搜索作为替代（披露易直连不稳定）
        # 东方财富港股公告 API
        import time
        cb_name = f"jQuery{''.join([str(x) for x in [7, 2, 9, 4, 1, 3]])}_{int(time.time() * 1000)}"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.eastmoney.com/",
        }
        param_obj = {
            "uid": "",
            "keyword": f"{company_name} 年报 公告 财报",
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": max(max_results, 10),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        params = {"cb": cb_name, "param": json.dumps(param_obj, ensure_ascii=False)}

        evs = []
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=15.0)
            text = r.text
            # 解析 JSONP
            m = re.search(r"\((\{.*\})\)", text, re.DOTALL)
            if not m:
                return SourceResult(evidences=evs)
            data = json.loads(m.group(1))
            articles = data.get("Data", {}).get("cmsArticleWebOld", {}).get("List", [])
            if not articles:
                articles = data.get("result", {}).get("cmsArticleWebOld", {}).get("List", [])

            # 相关性过滤：标题或内容必须包含公司名或股票代码
            keywords = [k for k in company_name.split() if len(k) >= 2]
            if stock_code:
                keywords.append(stock_code)

            for art in articles[:max_results * 2]:
                title = art.get("Title", "").replace("<em>", "").replace("</em>", "")
                content = art.get("Content", "").replace("<em>", "").replace("</em>", "")
                pub_date = art.get("Date", "") or art.get("PublishDate", "")
                art_url = art.get("Url", "") or art.get("ArticleUrl", "")

                # 相关性过滤
                combined = (title + content).lower()
                if keywords and not any(kw.lower() in combined for kw in keywords):
                    continue

                evs.append(Evidence(
                    content=(title + " " + content)[:400],
                    source_url=art_url,
                    source_title=title[:80] or f"{company_name} 公告",
                    source_type="filing",
                    published_at=pub_date,
                    tier=SourceTier.FILING,
                ))
                if len(evs) >= max_results:
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("HkexDisclosure 搜索失败: %s", e)

        return SourceResult(evidences=evs)


# ---------------- A股财报公告搜索（东方财富）----------------
class AShareFilingAdapter(BaseAdapter):
    """A股财报公告搜索适配器。

    利用东方财富的公告搜索 API（非新闻搜索），搜上市公司财报公告。
    返回 tier=FILING 级证据。
    """
    name = "a_share_filing"
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.eastmoney.com/",
    }

    def __init__(self):
        self.available = True

    def search(self, query, kind="news", max_results=5, days=None):
        import re
        import time
        # 东方财富公告搜索 API
        cb_name = f"jQuery{''.join([str(x) for x in [8, 3, 6, 1, 9, 2]])}_{int(time.time() * 1000)}"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        param_obj = {
            "uid": "",
            "keyword": f"{query} 年报 季报 业绩公告 财报",
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": max(max_results, 10),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        params = {"cb": cb_name, "param": json.dumps(param_obj, ensure_ascii=False)}

        evs = []
        try:
            r = httpx.get(url, params=params, headers=self._HEADERS, timeout=15.0)
            text = r.text
            m = re.search(r"\((\{.*\})\)", text, re.DOTALL)
            if not m:
                return SourceResult(evidences=evs)
            data = json.loads(m.group(1))
            articles = data.get("Data", {}).get("cmsArticleWebOld", {}).get("List", [])
            if not articles:
                articles = data.get("result", {}).get("cmsArticleWebOld", {}).get("List", [])

            # 相关性过滤
            keywords = [k for k in query.split() if len(k) >= 2]

            for art in articles[:max_results * 2]:
                title = art.get("Title", "").replace("<em>", "").replace("</em>", "")
                content = art.get("Content", "").replace("<em>", "").replace("</em>", "")
                pub_date = art.get("Date", "") or art.get("PublishDate", "")
                art_url = art.get("Url", "") or art.get("ArticleUrl", "")

                combined = (title + content).lower()
                if keywords and not any(kw.lower() in combined for kw in keywords):
                    continue

                # 只保留含财报相关关键词的
                fin_keywords = ["年报", "季报", "业绩", "营收", "利润", "财报", "公告"]
                if not any(fk in combined for fk in fin_keywords):
                    continue

                evs.append(Evidence(
                    content=(title + " " + content)[:400],
                    source_url=art_url,
                    source_title=title[:80] or f"{query} 财报公告",
                    source_type="filing",
                    published_at=pub_date,
                    tier=SourceTier.FILING,
                ))
                if len(evs) >= max_results:
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("AShareFiling 搜索失败: %s", e)

        return SourceResult(evidences=evs)


# ---------------- A股年报 PDF 解析 ----------------
class PdfReportAdapter(BaseAdapter):
    """A 股年报 PDF 解析适配器。

    支持用户上传 PDF 年报，自动提取三大报表 + 摘要表中的结构化财务数据。
    返回 structured（list[dict]，与 run.financials 格式一致）和 evidence。
    """
    name = "pdf_report"

    def __init__(self):
        try:
            import pdfplumber  # noqa: F401
            self.available = True
        except ImportError:
            self.available = False

    def search(self, query, kind="filing", max_results=5, days=None):
        """query 应为 PDF 文件路径。"""
        return self.parse_pdf(query)

    def parse_pdf(self, pdf_path: str) -> SourceResult:
        """解析一个 PDF 文件，返回结构化财务数据。"""
        if not self.available:
            return SourceResult(meta={"error": "pdfplumber 未安装"})
        from pathlib import Path
        if not Path(pdf_path).exists():
            return SourceResult(meta={"error": f"文件不存在: {pdf_path}"})

        from tools.pdf_parser import parse_annual_report
        from tools.financial_table_extractor import extract_financials

        parse_result = parse_annual_report(pdf_path)
        if parse_result.errors:
            return SourceResult(meta={"errors": parse_result.errors})

        extracted = extract_financials(parse_result)

        # 构建 evidence
        evs = []
        summary = (
            f"{extracted.company_name}({extracted.stock_code}) "
            f"{extracted.report_year}年报 PDF 解析: "
            f"{len(extracted.financials)}年财务数据, "
            f"指标覆盖 {len(extracted.metrics_found)} 个"
        )
        evs.append(Evidence(
            content=summary,
            source_url=f"file://{pdf_path}",
            source_title=f"{extracted.company_name} {extracted.report_year}年报",
            source_type="filing",
            evidence_type="pdf",
            tier=SourceTier.FILING,
            as_of=f"{extracted.report_year}-12-31" if extracted.report_year else "",
        ))

        return SourceResult(
            evidences=evs,
            structured=extracted.financials,
            meta={
                "company_name": extracted.company_name,
                "stock_code": extracted.stock_code,
                "report_year": extracted.report_year,
                "metrics_found": extracted.metrics_found,
                "metrics_missing": extracted.metrics_missing,
                "source_tables": extracted.source_tables,
            },
        )


# ---------------- 注册表 ----------------
_REGISTRY: dict[str, BaseAdapter] = {}


# ---------------- Exa 语义搜索（API key 或 mcporter / agent-reach） ----------------
class ExaSearchAdapter(BaseAdapter):
    """Exa AI 语义搜索。

    优先使用 EXA_API_KEY 直连 Exa API；未配置 key 时回退到 mcporter MCP。
    语义搜索质量高，特别适合中英文混合查询与行业/公司研究。"""
    name = "exa_search"

    def __init__(self):
        self._api_key = os.getenv("EXA_API_KEY", "").strip()
        self._mcporter = shutil.which("mcporter") or ""
        self.available = bool(self._api_key or self._mcporter)

    def search(self, query, kind="general", max_results=5, days=None):
        if not self.available:
            return SourceResult()
        if self._api_key:
            return self._search_api(query, kind=kind, max_results=max_results, days=days)
        return self._search_mcporter(query, kind=kind, max_results=max_results, days=days)

    def _search_api(self, query, kind="general", max_results=5, days=None):
        payload = {
            "query": query,
            "numResults": max_results,
            "contents": {"highlights": True, "text": True},
        }
        if days:
            from datetime import datetime, timedelta
            payload["startPublishedDate"] = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with httpx.Client(timeout=40) as client:
                resp = client.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("Exa API 搜索异常: %s", e, exc_info=True)
            return SourceResult(meta={"error": str(e)})

        evs = []
        for item in data.get("results", [])[:max_results]:
            title = item.get("title") or ""
            url = item.get("url") or ""
            highlights = item.get("highlights") or []
            if isinstance(highlights, list):
                content = " ".join(str(x).strip() for x in highlights if str(x).strip())
            else:
                content = str(highlights or "")
            if not content:
                content = str(item.get("text") or "")[:400]
            if not content:
                content = title
            published = item.get("publishedDate") or item.get("published_at") or ""
            if title or url:
                evs.append(Evidence(
                    content=content[:400], source_url=url,
                    source_title=title[:80] or "Exa搜索结果",
                    source_type="web", tier=SourceTier.MEDIA,
                    published_at=str(published)[:10] if published else "",
                ))
        return SourceResult(evidences=evs)

    def _search_mcporter(self, query, kind="general", max_results=5, days=None):
        # mcporter 0.9+ 的 Exa MCP schema 只接受 query/numResults；days 通过语义补充给查询词。
        effective_query = str(query or "").strip()
        if days:
            from datetime import datetime, timedelta
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            effective_query = f"{effective_query} published after {start_date}".strip()
        try:
            proc = subprocess.run(
                [self._mcporter, "call", "exa.web_search_exa",
                 f"query={effective_query}",
                 f"numResults={max_results}",
                 "--output", "json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if proc.returncode != 0:
                _err = proc.stderr[:200]
                logger.warning("Exa mcporter 调用失败 (rc=%s): %s", proc.returncode, _err)
                return SourceResult(meta={"error": _err})
            if not proc.stdout:
                _err = (proc.stderr or "mcporter returned empty stdout")[:300]
                logger.warning("Exa mcporter 未返回 JSON: %s", _err)
                return SourceResult(meta={"error": _err})
            data = json.loads(proc.stdout)
            # mcporter 成功退出但 Exa 可能返回业务错误（如 429 限流）
            if "error" in data:
                _biz_err = str(data["error"])[:300]
                logger.warning("Exa 返回业务错误: %s", _biz_err)
                return SourceResult(meta={"error": _biz_err})
        except Exception as e:  # noqa: BLE001
            logger.warning("Exa 搜索异常: %s", e, exc_info=True)
            return SourceResult(meta={"error": str(e)})

        # 解析 mcporter JSON 输出：content[0].text 包含多段 "Title: ...\nURL: ...\n..."
        text = ""
        try:
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return SourceResult()

        evs = []
        for block in text.split("\n---\n"):
            block = block.strip()
            if not block:
                continue
            title = url = published = ""
            highlights = []
            in_highlights = False
            for line in block.split("\n"):
                if line.startswith("Title: "):
                    title = line[7:].strip()
                    in_highlights = False
                elif line.startswith("URL: "):
                    url = line[5:].strip()
                    in_highlights = False
                elif line.startswith("Published: "):
                    published = line[11:].strip()
                    if published == "N/A":
                        published = ""
                    in_highlights = False
                elif line.startswith("Highlights:"):
                    in_highlights = True
                elif in_highlights:
                    highlights.append(line)

            content = " ".join(hl.strip() for hl in highlights if hl.strip())[:400]
            if not content:
                content = title
            if title or url:
                evs.append(Evidence(
                    content=content, source_url=url,
                    source_title=title[:80] or "Exa搜索结果",
                    source_type="web", tier=SourceTier.MEDIA,
                    published_at=published[:10] if published else "",
                ))
        return SourceResult(evidences=evs)


# ---------------- 通用 PDF 适配器 ----------------
class GenericPdfAdapter(BaseAdapter):
    """通用 PDF 解析适配器：上传任意 PDF（研报/招股书/行业报告等）解析入证据池。

    与 PdfReportAdapter（A 股年报专用）的区别：
    - 通用模式：不识别财报结构，只提取全文+所有表格
    - 适用：用户上传研报、招股书等；agent 搜索发现 PDF URL 后自动下载
    """
    name = "generic_pdf"

    def __init__(self):
        # pdfplumber 已装，无需依赖检查
        self.available = True

    def search(self, query, kind="news", max_results=5, days=None):
        """通用 PDF 适配器不通过 search() 调用，API 是 parse_pdf()。"""
        return SourceResult()

    def parse_pdf(
        self,
        pdf_path: str,
        max_pages: int | None = None,
        source_url: str = "",
    ) -> SourceResult:
        """解析本地 PDF 文件 → Evidence 列表。

        Args:
            pdf_path: 本地 PDF 路径
            max_pages: 最多处理多少页
            source_url: 原始下载 URL（用于溯源）

        Returns:
            SourceResult(evidences=[...])
        """
        from tools.generic_pdf import parse_generic_pdf, split_into_evidence

        try:
            result = parse_generic_pdf(pdf_path, max_pages=max_pages)
        except Exception as e:  # noqa: BLE001
            return SourceResult(meta={"error": f"PDF 解析失败: {e}"})

        if result.error:
            return SourceResult(meta={"error": result.error})

        evs = []
        for ev_dict in split_into_evidence(result, source_url=source_url):
            evs.append(Evidence(
                content=ev_dict["content"],
                source_url=ev_dict.get("source_url") or "",
                source_title=ev_dict.get("source_title") or result.title,
                source_type="pdf",
                published_at=ev_dict.get("as_of") or "",
                tier=SourceTier.FILING,  # PDF 一手资料，作为 FILING 级
            ))
        meta = {
            "total_pages": result.total_pages,
            "text_chunks": len(result.text_chunks),
            "table_chunks": len(result.table_chunks),
        }
        return SourceResult(evidences=evs, meta=meta)

    def parse_pdf_url(
        self,
        pdf_url: str,
        max_pages: int | None = None,
        download_dir: str = "",
    ) -> SourceResult:
        """从 URL 下载 PDF 并解析。

        Args:
            pdf_url: PDF 文件 URL
            max_pages: 最多处理多少页
            download_dir: 下载目录（默认 ~/.workbuddy/cache/pdf/）
        """
        import hashlib
        import httpx
        from pathlib import Path as _P

        if not download_dir:
            download_dir = str(_P.home() / ".workbuddy" / "cache" / "pdf")
        _P(download_dir).mkdir(parents=True, exist_ok=True)

        # 用 URL hash 命名避免重复下载
        url_hash = hashlib.md5(pdf_url.encode()).hexdigest()[:16]
        local_path = _P(download_dir) / f"{url_hash}.pdf"

        if not local_path.exists():
            try:
                with httpx.stream("GET", pdf_url, timeout=60.0, follow_redirects=True) as r:
                    r.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
            except Exception as e:  # noqa: BLE001
                return SourceResult(meta={"error": f"下载失败: {e}"})

        return self.parse_pdf(str(local_path), max_pages=max_pages, source_url=pdf_url)


def init_adapters() -> dict[str, BaseAdapter]:
    global _REGISTRY
    if not _REGISTRY:
        for cls in (TavilySerperAdapter, SecEdgarAdapter, WindMcpAdapter, NeoDataAdapter, IfindAdapter, MXFinSearchAdapter, HkexDisclosureAdapter, AShareFilingAdapter, ExaSearchAdapter, PdfReportAdapter, GenericPdfAdapter):
            try:
                inst = cls()
                _REGISTRY[inst.name] = inst
            except Exception:  # noqa: BLE001
                pass
        # Firecrawl 深度抓取适配器（需要 API key）
        try:
            from tools.firecrawl_adapter import FirecrawlAdapter
            fc = FirecrawlAdapter()
            _REGISTRY[fc.name] = fc
        except Exception:  # noqa: BLE001
            pass
    return _REGISTRY


DATA_SOURCE_CATALOG: dict[str, dict] = {
    "exa_search": {
        "label": "Exa 语义搜索",
        "purpose": "核心语义搜索源，用于采集公司、行业、新闻和反证线索。",
        "priority": "required",
        "category": "search",
        "input_type": "api_key",
        "env_vars": ["EXA_API_KEY"],
        "primary_env": "EXA_API_KEY",
        "input_hint": "UUID 格式的 Exa API key",
        "cost": "免费额度约 1000 次/月",
        "url": "https://dashboard.exa.ai/api-keys",
        "note": "已安装 mcporter + Exa MCP 时也可不填 key；没有任何搜索源时 agent 会停止生成低置信报告。",
    },
    "general_search": {
        "label": "Tavily / Serper 通用搜索",
        "purpose": "通用网页搜索兜底，用于 Exa 限流、失败或结果不足时补充证据。",
        "priority": "recommended",
        "category": "search",
        "input_type": "api_key",
        "env_vars": ["TAVILY_API_KEY", "SERPER_API_KEY"],
        "primary_env": "TAVILY_API_KEY",
        "input_hint": "优先填 TAVILY_API_KEY，也可填 SERPER_API_KEY",
        "cost": "Tavily 约 1000 次/月；Serper 约 2500 次",
        "url": "https://tavily.com",
        "note": "两者配置任一即可启用；同时配置时作为冗余搜索源。",
    },
    "sec_edgar": {
        "label": "SEC EDGAR 美股财报",
        "purpose": "免费拉取美股上市公司 SEC XBRL 财务数据，适合营收、利润、资产负债等硬指标。",
        "priority": "recommended",
        "category": "filing",
        "input_type": "user_agent",
        "env_vars": ["SEC_USER_AGENT"],
        "primary_env": "SEC_USER_AGENT",
        "input_hint": "格式：产品名 联系邮箱，如 BizAnalyst admin@example.com",
        "cost": "免费，无 API key",
        "url": "https://www.sec.gov/os/accessing-edgar-data",
        "note": "不填也可用默认 UA，但正式使用建议填写合规 User-Agent。",
    },
    "wind": {
        "label": "Wind / AIFin Market MCP",
        "purpose": "专业金融数据源，适合 A 股/港股的公告、研报、财务和宏观数据补强。",
        "priority": "optional",
        "category": "finance",
        "input_type": "mcp_config",
        "env_vars": [
            "WIND_MCP_TOOL",
            "WIND_MCP_SERVER",
            "WIND_MCP_QUERY_ARG",
            "WIND_MCP_LIMIT_ARG",
            "WIND_MCP_KIND_ARG",
            "WIND_MCP_COMMAND",
        ],
        "primary_env": "WIND_MCP_TOOL",
        "input_hint": "Tool 名，如 financial_docs.get_financial_news",
        "cost": "AIFin Market 当前约 1000 次/日",
        "url": "https://aifinmarket.wind.com.cn/#/home",
        "note": "推荐通过 mcporter 调用 Wind MCP；也可用 WIND_MCP_COMMAND 配一条返回 JSON 的命令模板。",
    },
    "neodata": {
        "label": "NeoData 金融数据",
        "purpose": "通过本地 skill 脚本拉取 A 股/港美股行情、财务、公告和研报。",
        "priority": "optional",
        "category": "finance",
        "input_type": "script_path",
        "env_vars": ["NEODATA_SCRIPT", "PYBIN"],
        "primary_env": "NEODATA_SCRIPT",
        "input_hint": "本地 query.py 路径；通常安装扩展脚本后自动识别",
        "cost": "依赖本机/服务器扩展脚本安装",
        "url": "",
        "note": "不是 HTTP API key。填 token 不会生效，需要填脚本路径或安装对应扩展脚本。",
    },
    "ifind": {
        "label": "同花顺 iFinD",
        "purpose": "通过本地 skill 脚本调用 iFinD，用于股票、基金、宏观、新闻公告数据。",
        "priority": "optional",
        "category": "finance",
        "input_type": "script_path",
        "env_vars": ["IFIND_SCRIPT", "IFIND_WRAPPER", "NODEBIN"],
        "primary_env": "IFIND_SCRIPT",
        "input_hint": "本地 call-node.js 路径；通常安装扩展脚本后自动识别",
        "cost": "依赖本机/服务器扩展脚本安装和账号能力",
        "url": "",
        "note": "不是 HTTP API key。填 token 不会生效，需要填脚本路径或安装对应扩展脚本。",
    },
    "em_news": {
        "label": "东方财富资讯",
        "purpose": "内置东方财富公开资讯检索，用于中文新闻和公告线索补充。",
        "priority": "recommended",
        "category": "news",
        "input_type": "none",
        "env_vars": [],
        "primary_env": "",
        "input_hint": "",
        "cost": "内置，免费，无需配置",
        "url": "https://www.eastmoney.com/",
        "note": "没有东方财富 API key 配置项；如果输入 EASTMONEY_API_KEY 会被忽略。",
    },
    "pdf_report": {
        "label": "本地年报 PDF 解析",
        "purpose": "解析用户上传或本地路径导入的年报 PDF，提取结构化财务指标作为硬证据补充。",
        "priority": "optional",
        "category": "filing",
        "input_type": "none",
        "env_vars": [],
        "primary_env": "",
        "input_hint": "",
        "cost": "本地解析，无需 API key",
        "url": "",
        "note": "当前规则主要面向中文 A 股年报；非标准版式或海外公司 PDF 可能只能部分识别。",
    },
    "generic_pdf": {
        "label": "通用 PDF 解析（研报/招股书）",
        "purpose": "用户上传或搜索发现任意 PDF，自动提取全文+所有表格作为证据。",
        "priority": "optional",
        "category": "filing",
        "input_type": "none",
        "env_vars": [],
        "primary_env": "",
        "input_hint": "",
        "cost": "本地解析，无需 API key",
        "url": "",
        "note": "适用任意 PDF（券商研报、招股书、招股说明书等）。前 N 页用作证据摘要，避免大文件 token 超限。",
    },
    "firecrawl": {
        "label": "Firecrawl 深度抓取",
        "purpose": "JS 渲染网页抓取 + 结构化数据提取，补充 Exa 对动态页面的覆盖短板。",
        "priority": "recommended",
        "category": "search",
        "input_type": "none",
        "env_vars": ["FIRECRAWL_API_KEY"],
        "primary_env": "FIRECRAWL_API_KEY",
        "input_hint": "可选；不填则使用 Keyless 免费层",
        "cost": "Keyless 每月 1000 credits；scrape=1，search=2/最多10条结果",
        "url": "https://www.firecrawl.dev/app/api-keys",
        "note": "默认使用 Keyless v2 并在本地限制每月 1000 credits；配置 API key 后切换到账户额度。失败时自动 fallback 到 httpx/Playwright。",
    },
}


def source_catalog() -> dict[str, dict]:
    return {
        name: {k: (list(v) if isinstance(v, list) else v) for k, v in meta.items()}
        for name, meta in DATA_SOURCE_CATALOG.items()
    }


def runtime_config_envs() -> set[str]:
    envs: set[str] = set()
    for meta in DATA_SOURCE_CATALOG.values():
        envs.update(str(x) for x in meta.get("env_vars", []) if x)
    return envs


def list_adapters() -> list[dict]:
    init_adapters()
    out = []
    catalog = source_catalog()
    for name, adapter in _REGISTRY.items():
        meta = catalog.get(name, {})
        env_vars = [str(x) for x in meta.get("env_vars", []) if x]
        configured = [env for env in env_vars if os.getenv(env, "").strip()]
        item = {
            "name": name,
            "available": adapter.available,
            **meta,
            "configured_envs": configured,
            "missing_envs": [env for env in env_vars if env not in configured],
        }
        if meta.get("input_type") == "none":
            item["status_note"] = "无需配置，后端内置可用。"
        elif adapter.available:
            # 检测到本地脚本，把路径告诉前端（供"✓ 已就绪"展示）
            detected_path = ""
            if name == "neodata":
                detected_path = adapter.script or ""
            elif name == "ifind":
                detected_path = adapter.script or ""
            elif name == "wind":
                detected_path = getattr(adapter, "command_template", "") or (
                    f"mcporter:{getattr(adapter, 'server', '')}/{getattr(adapter, 'tool', '')}"
                    if getattr(adapter, "_mcporter", "") and getattr(adapter, "tool", "") else ""
                )
            item["detected_path"] = detected_path
            item["status_note"] = "已就绪。"
        elif env_vars:
            item["status_note"] = f"未就绪，需配置 {' 或 '.join(env_vars[:2])}。"
        else:
            item["status_note"] = "未就绪。"
        out.append(item)
    return out


def get_adapter(name: str) -> Optional[BaseAdapter]:
    init_adapters()
    return _REGISTRY.get(name)


def available_sources() -> list[str]:
    init_adapters()
    return [n for n, a in _REGISTRY.items() if a.available]
