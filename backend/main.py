"""FastAPI 入口 —— /analyze 启动任务，/stream SSE 推送轨迹，/report 取报告。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app_config import (  # noqa: E402
    assert_custom_provider_allowed,
    assert_env_persist_allowed,
    assert_runtime_config_allowed,
    load_runtime_config,
)
from llm.client import get_client  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from schemas import AnalysisRun  # noqa: E402
from store import list_runs, load_run, mark_stale_runs_interrupted, save_run  # noqa: E402
from tools.finance_sources import list_adapters, runtime_config_envs  # noqa: E402
from tools.search import has_search_backend, search_status  # noqa: E402


def _firecrawl_budget() -> dict | None:
    """获取 Firecrawl 预算状态；未安装时返回 None。"""
    try:
        from tools.firecrawl_adapter import firecrawl_budget_status
        return firecrawl_budget_status()
    except ImportError:
        return None

_RUNTIME_CONFIG = load_runtime_config()

app = FastAPI(title="商业分析 Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_RUNTIME_CONFIG.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=_RUNTIME_CONFIG.cors_allow_headers,
)

# 内存中正在运行的任务
_RUNS: dict[str, AnalysisRun] = {}
# 正在运行的 Orchestrator（供 Human-in-the-loop 发信号）
_ORCHESTRATORS: dict[str, Orchestrator] = {}


class AnalyzeReq(BaseModel):
    query: str
    provider: str | None = None
    red_team_provider: str | None = None
    reviewer_provider: str | None = None


class ConfigReq(BaseModel):
    """前端动态配置：把用户填的 key 存入进程环境变量（不持久化到磁盘）。"""
    keys: dict[str, str] = {}


def _runtime_config_key_allowlist() -> set[str]:
    """Keys that the settings panel may set in the current process."""
    allowed = set(runtime_config_envs())
    client = get_client()
    for provider_cfg in client.providers.values():
        api_key_env = provider_cfg.get("api_key_env")
        if api_key_env:
            allowed.add(str(api_key_env))
        for envvar in (provider_cfg.get("extra_headers") or {}).values():
            if envvar:
                allowed.add(str(envvar))
    return allowed


def _apply_runtime_keys(keys: dict[str, str]) -> tuple[list[str], list[str]]:
    allowed = _runtime_config_key_allowlist()
    applied: list[str] = []
    ignored: list[str] = []
    for key, value in keys.items():
        k = str(key or "").strip()
        v = str(value or "").strip()
        if not k or not v:
            continue
        if k not in allowed:
            ignored.append(k)
            continue
        os.environ[k] = v
        applied.append(k)
    return applied, ignored


@app.get("/api/health")
def health(analyst: str | None = None, red_team: str | None = None):
    """analyst/red_team 查询参数：指定主分析/红队 provider，返回该配对下的同源判定。
    不传时按 role_defaults 解析（前端用于展示默认配对）。"""
    providers = get_client().list_available()
    demo = not any(p["available"] for p in providers)
    client = get_client()
    a_eff = client.effective_provider("analyst", analyst) if not demo else None
    r_eff = client.effective_provider("red_team", red_team) if not demo else None
    same_source = False if demo else (not a_eff or not r_eff or a_eff == r_eff)
    return {
        "status": "ok",
        "search_backend": has_search_backend(),
        "search_status": search_status(),
        "providers": providers,
        "demo_mode": demo,
        "same_source_review": same_source,
        "analyst_provider": "demo" if demo else (client.effective_provider("analyst", analyst) or "demo"),
        "red_team_provider": "demo" if demo else (client.effective_provider("red_team", red_team) or "demo"),
        "reviewer_provider": "demo" if demo else (client.effective_provider("reviewer") or "demo"),
        "role_plan": client.role_plan() if not demo else {},
        "data_sources": list_adapters(),
        "firecrawl_budget": _firecrawl_budget(),
        "app_mode": _RUNTIME_CONFIG.app_mode,
        "runtime_config_enabled": _RUNTIME_CONFIG.allow_runtime_config,
        "custom_provider_enabled": _RUNTIME_CONFIG.allow_custom_provider,
    }


@app.post("/api/config")
def set_config(req: ConfigReq):
    """前端提交 API Key，进程内生效，不写盘。"""
    try:
        assert_runtime_config_allowed(_RUNTIME_CONFIG)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc

    applied, ignored = _apply_runtime_keys(req.keys)
    # 重置 client 与适配器单例，重新读取环境
    import llm.client as lc
    lc._client = None
    import tools.finance_sources as fs
    fs._REGISTRY = {}
    client = get_client()
    return {
        "applied": applied,
        "ignored": ignored,
        "providers": client.list_available(),
        "same_source_review": client.is_same_source_review(None),
        "data_sources": list_adapters(),
    }


@app.get("/api/data_sources")
def data_sources():
    return list_adapters()


@app.get("/api/providers")
def providers():
    return get_client().list_available()


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    client = get_client()
    provider = req.provider or client.primary_provider()
    red_team = req.red_team_provider or client.effective_provider("red_team") or ""
    reviewer = req.reviewer_provider or client.effective_provider("reviewer") or ""
    run = AnalysisRun(query=req.query.strip(), provider=provider, red_team_provider=red_team, reviewer_provider=reviewer)
    _RUNS[run.id] = run
    save_run(run)
    return {"run_id": run.id}


class ResolveReq(BaseModel):
    query: str


@app.post("/api/resolve")
def resolve(req: ResolveReq):
    """实体消歧：输入模糊名（如"蚂蚁"），返回候选实体 + 是否需用户确认。"""
    import prompts
    q = req.query.strip()
    if not q:
        raise HTTPException(400, "query 不能为空")
    client = get_client()
    providers = client.list_available()
    demo = not any(p["available"] for p in providers)
    if demo:
        return {"ambiguous": False, "default": q, "candidates": [{"name": q, "kind": "company", "is_public": True, "ticker": "", "note": "演示模式：未配置 LLM，跳过消歧"}]}
    try:
        data = client.chat_json(prompts.resolve_prompt(q), role="analyst", temperature=0.0, max_tokens=1024)
    except Exception as e:  # noqa: BLE001
        return {"ambiguous": False, "default": q, "candidates": [{"name": q, "kind": "company", "is_public": True, "ticker": "", "note": f"消歧失败({str(e)[:60]})，按原输入继续"}]}
    if isinstance(data, dict):
        cands = data.get("candidates") or []
        if not cands:
            cands = [{"name": data.get("default") or q, "kind": "company", "is_public": True, "ticker": "", "note": ""}]
        cands = _dedup_candidates(cands)
        ambig = bool(data.get("ambiguous")) and len(cands) > 1
        default = data.get("default") or cands[0].get("name", q)
        return {"ambiguous": ambig, "default": default, "candidates": cands}
    return {"ambiguous": False, "default": q, "candidates": [{"name": q, "kind": "company", "is_public": True, "ticker": "", "note": ""}]}


def _dedup_candidates(cands: list[dict]) -> list[dict]:
    import re as _re
    def _normalize_name(n: str) -> str:
        return _re.sub(r"[()（）\s]", "", n).lower()
    merged: list[dict] = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        tk = (c.get("ticker") or "").strip().upper()
        note = (c.get("note") or "")
        dup_idx = -1
        norm_name = _normalize_name(name)
        for i, m in enumerate(merged):
            mn = (m.get("name") or "").strip()
            mtk = (m.get("ticker") or "").strip().upper()
            mnote = (m.get("note") or "")
            norm_mn = _normalize_name(mn)
            same_ticker = bool(tk and mtk and tk == mtk)
            same_name_sub = bool(name and mn and (name in mn or mn in name))
            same_norm = bool(norm_name and norm_mn and norm_name == norm_mn)
            merge_signals = ("同一主体", "同一实体", "即", "亦称", "同一", "同上", "同前", "相同", "英文版", "英文写法", "英文名常", "英文别名")
            same_note = any(k in (mnote + note) for k in merge_signals)
            if same_ticker or same_name_sub or same_norm or same_note:
                dup_idx = i
                break
        if dup_idx < 0:
            merged.append(c)
        else:
            m = merged[dup_idx]
            alias_hint = ""
            if name not in (m.get("name") or "") and name not in (m.get("note") or ""):
                alias_hint = f"；亦称 {name}"
            existing_note = m.get("note") or ""
            if alias_hint and alias_hint not in existing_note:
                m["note"] = existing_note + alias_hint
            if tk and not m.get("ticker"):
                m["ticker"] = tk
    return merged


class CustomProviderReq(BaseModel):
    label: str
    base_url: str
    model: str
    api_key: str = ""
    extra_headers: dict[str, str] = {}
    family: str = "custom"
    tier: int = 3
    reasoning: bool = False


_LOCAL_YAML = Path(__file__).parent / "llm" / "providers.local.yaml"
_ENV_FILE = Path(__file__).parent.parent / ".env"


@app.post("/api/providers/custom")
def add_custom_provider(req: CustomProviderReq):
    import yaml
    try:
        assert_custom_provider_allowed(_RUNTIME_CONFIG, req.base_url)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(403, str(exc)) from exc
    key = re.sub(r"[^a-z0-9]+", "_", req.label.lower()).strip("_") or "custom"
    api_key_env = f"CUSTOM_{key.upper()}_API_KEY"
    extra = {}
    env_to_set = {}
    for h, v in (req.extra_headers or {}).items():
        envvar = f"CUSTOM_{key.upper()}_{re.sub(r'[^A-Z0-9]+','_',h.upper()).strip('_')}"
        extra[h] = envvar
        env_to_set[envvar] = v
    prov = {
        "label": req.label, "base_url": req.base_url, "model": req.model,
        "api_key_env": api_key_env, "family": req.family, "tier": int(req.tier),
        "reasoning": bool(req.reasoning), "roles": ["analyst", "red_team"],
    }
    if extra:
        prov["extra_headers"] = extra
    local = {}
    if _LOCAL_YAML.exists():
        local = yaml.safe_load(_LOCAL_YAML.read_text(encoding="utf-8")) or {}
    local.setdefault("providers", {})[key] = prov
    local["fallback_order"] = list(dict.fromkeys([key] + (local.get("fallback_order") or [])))
    _LOCAL_YAML.write_text(yaml.safe_dump(local, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if req.api_key:
        os.environ[api_key_env] = req.api_key
        if _RUNTIME_CONFIG.allow_env_persist:
            _append_env(api_key_env, req.api_key)
    for k, v in env_to_set.items():
        os.environ[k] = v
    import llm.client as lc
    lc._client = None
    import tools.finance_sources as fs
    fs._REGISTRY = {}
    client = get_client()
    return {"key": key, "providers": client.list_available(),
            "role_plan": client.role_plan() if not (not any(p["available"] for p in client.list_available())) else {}}


def _append_env(key: str, value: str):
    try:
        assert_env_persist_allowed(_RUNTIME_CONFIG)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    lines = []
    found = False
    if _ENV_FILE.exists():
        for ln in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(ln)
    if not found:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/stream/{run_id}")
async def stream(run_id: str):
    run = _RUNS.get(run_id) or load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _RUNS[run_id] = run

    async def event_gen():
        if run.status in ("done", "error", "partial", "needs_human"):
            for ev in run.trace:
                yield {"event": "trace", "data": ev.model_dump_json()}
            yield {"event": "result", "data": run.model_dump_json()}
            return
        orch = Orchestrator(run)
        _ORCHESTRATORS[run_id] = orch
        try:
            async for ev in orch.run_pipeline():
                yield {"event": "trace", "data": ev.model_dump_json()}
                save_run(run)
            yield {"event": "result", "data": run.model_dump_json()}
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"error": str(exc)})}
        finally:
            _ORCHESTRATORS.pop(run_id, None)
            save_run(run)

    return EventSourceResponse(event_gen())


@app.get("/api/runs")
def runs():
    mark_stale_runs_interrupted()
    return list_runs()


@app.post("/api/resume/{run_id}")
async def resume_run(run_id: str):
    """断点续跑：从最后成功阶段的检查点恢复，跳过已完成的阶段。
    前端用 SSE 连接 /api/stream/{run_id} 接收续跑事件。"""
    run = load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if not run.checkpoint_stage:
        raise HTTPException(400, "该任务无可恢复的检查点（可能已完成或未开始）")
    # 重置状态为 running，保留 checkpoint_stage 让 orchestrator 知道从哪恢复
    run.status = "pending"
    _RUNS[run_id] = run
    return {"run_id": run_id, "resume_from": run.checkpoint_stage}


class ConfirmScopeReq(BaseModel):
    sections: list[str] | None = None  # None = 原样确认，list = 用户调整后的维度


@app.post("/api/confirm_scope/{run_id}")
def confirm_scope(run_id: str, req: ConfirmScopeReq):
    """Human-in-the-loop：用户确认/调整 Scope 维度后释放 pipeline 继续执行。"""
    orch = _ORCHESTRATORS.get(run_id)
    if not orch:
        raise HTTPException(404, "该任务不在运行中或已通过审查点")
    if orch._scope_confirmed.is_set():
        return {"status": "already_confirmed"}
    # 传递用户修改的维度
    if req.sections is not None and len(req.sections) > 0:
        orch._scope_user_sections = req.sections
    orch._scope_confirmed.set()
    return {"status": "confirmed", "sections": req.sections}


@app.get("/api/run/{run_id}")
def get_run(run_id: str):
    run = _RUNS.get(run_id) or load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run.model_dump()


@app.get("/api/report/{run_id}")
def report(run_id: str):
    run = _RUNS.get(run_id) or load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {
        "report_md": run.report_md,
        "narrative_md": run.narrative_md,
        "query": run.query,
        "data_as_of": run.data_as_of,
        "data_sources_used": run.data_sources_used,
    }


@app.get("/api/export/{run_id}")
def export_pdf(run_id: str):
    """Export the narrative report as a downloadable PDF."""
    from fastapi.responses import Response
    from reporting.pdf_export import render_pdf

    run = _RUNS.get(run_id) or load_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    md = run.narrative_md or run.report_md
    if not md:
        raise HTTPException(400, "该任务尚无可导出的报告")
    try:
        pdf_bytes = render_pdf(md, query=run.query, data_as_of=run.data_as_of)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"PDF 渲染失败: {exc}") from exc
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]", "_", run.query or "report")[:40]
    # HTTP headers are latin-1; use RFC 5987 filename* for Unicode names,
    # with an ASCII fallback for older clients.
    from urllib.parse import quote
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", safe_name) or "report"
    encoded_name = quote(safe_name, safe="")
    disposition = (
        f'attachment; filename="{ascii_fallback}.pdf"; '
        f"filename*=UTF-8''{encoded_name}.pdf"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


# --------------- PDF 年报上传解析 ---------------

from fastapi import UploadFile, File as FastAPIFile


class PdfPathReq(BaseModel):
    path: str
    run_id: str = ""


class PdfUrlReq(BaseModel):
    url: str
    run_id: str = ""
    max_pages: int = 50


def _parse_annual_report_pdf(pdf_path: str, run_id: str = "") -> dict:
    path = Path(pdf_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(400, f"文件不存在: {pdf_path}")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(400, "请提供 .pdf 格式的文件")
    size = path.stat().st_size
    if size < 1000:
        raise HTTPException(400, "文件过小，可能不是有效的 PDF")
    if size > 100 * 1024 * 1024:
        raise HTTPException(400, "文件过大（超过 100MB）")

    from tools.finance_sources import get_adapter, init_adapters
    init_adapters()
    adapter = get_adapter("pdf_report")
    if not adapter or not adapter.available:
        raise HTTPException(500, "PDF 解析模块不可用（pdfplumber 未安装）")

    result = adapter.parse_pdf(str(path))
    if result.meta.get("errors"):
        raise HTTPException(422, f"PDF 解析失败: {result.meta['errors']}")
    if result.meta.get("error"):
        raise HTTPException(422, f"PDF 解析失败: {result.meta['error']}")

    if run_id:
        run = _RUNS.get(run_id) or load_run(run_id)
        if run:
            existing_periods = {f.get("period") for f in run.financials}
            for fin in result.structured:
                if fin.get("period") not in existing_periods:
                    run.financials.append(fin)
                    existing_periods.add(fin.get("period"))
                else:
                    existing = next(f for f in run.financials if f.get("period") == fin.get("period"))
                    for k, v in fin.items():
                        if k not in existing or existing[k] is None:
                            existing[k] = v
            run.evidence.extend(result.evidences)
            _RUNS[run.id] = run
            save_run(run)

    return {
        "success": True,
        "company_name": result.meta.get("company_name", ""),
        "stock_code": result.meta.get("stock_code", ""),
        "report_year": result.meta.get("report_year", ""),
        "years_extracted": len(result.structured),
        "metrics_found": result.meta.get("metrics_found", []),
        "metrics_missing": result.meta.get("metrics_missing", []),
        "source_tables": result.meta.get("source_tables", []),
        "financials": result.structured,
    }


@app.post("/api/upload-annual-report")
async def upload_annual_report(file: UploadFile = FastAPIFile(...), run_id: str = ""):
    """上传 A 股年报 PDF，解析结构化财务数据。

    - file: PDF 文件（multipart/form-data）
    - run_id: 可选，如果提供则将数据注入到已有的分析任务中

    返回：公司信息 + 三年财务指标
    """
    import tempfile

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 .pdf 格式的文件")

    # 保存到临时文件
    content = await file.read()
    if len(content) < 1000:
        raise HTTPException(400, "文件过小，可能不是有效的 PDF")
    if len(content) > 100 * 1024 * 1024:  # 100MB limit
        raise HTTPException(400, "文件过大（超过 100MB）")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        return _parse_annual_report_pdf(tmp_path, run_id=run_id)
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/api/import-annual-report-path")
def import_annual_report_path(req: PdfPathReq):
    """从后端本机路径导入 PDF，用于浏览器文件选择/拖拽不可用的场景。"""
    # 路径遍历防护：规范化路径后检查合法性
    raw = req.path.strip()
    if not raw:
        raise HTTPException(400, "路径不能为空")
    resolved = Path(raw).resolve()
    # 禁止 .. 遍历和符号链接指向敏感目录
    _BLOCKED_PREFIXES = ("/etc", "/var", "/usr", "/bin", "/sbin", "/proc", "/sys",
                         "C:\\Windows", "C:\\Program Files", "C:\\ProgramData")
    resolved_str = str(resolved)
    for prefix in _BLOCKED_PREFIXES:
        if resolved_str.lower().startswith(prefix.lower()):
            raise HTTPException(403, f"不允许访问系统目录: {prefix}")
    if ".." in raw:
        raise HTTPException(400, "路径中不允许包含 '..'")
    if resolved.suffix.lower() != ".pdf":
        raise HTTPException(400, "只允许导入 .pdf 文件")
    return _parse_annual_report_pdf(str(resolved), run_id=req.run_id)


@app.post("/api/upload-generic-pdf")
async def upload_generic_pdf(
    file: UploadFile = FastAPIFile(...),
    run_id: str = "",
    max_pages: int = 50,
):
    """上传任意 PDF（研报/招股书/行业报告），提取全文+表格作为证据。

    - file: PDF 文件
    - run_id: 可选，绑定到已有分析任务
    - max_pages: 最多处理多少页（默认 50，控制 token 消耗）

    返回：解析摘要（页数、文本块、表格数）+ 注入到 run.evidence_pool 的 evidence 数
    """
    import tempfile

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 .pdf 格式的文件")
    content = await file.read()
    if len(content) < 1000:
        raise HTTPException(400, "文件过小，可能不是有效的 PDF")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(400, "文件过大（超过 100MB）")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # 调用 GenericPdfAdapter 解析
        from tools.finance_sources import GenericPdfAdapter
        adapter = GenericPdfAdapter()
        result = adapter.parse_pdf(tmp_path, max_pages=max_pages)
        if result.meta.get("error"):
            raise HTTPException(500, f"PDF 解析失败: {result.meta['error']}")

        # 注入到已有 run 的证据池（如果提供了 run_id）
        injected = 0
        if run_id and result.evidences:
            try:
                run = _RUNS.get(run_id) or load_run(run_id)
                if run:
                    _RUNS[run_id] = run
                    orch = _ORCHESTRATORS.get(run_id)
                    if orch:
                        # 复用 _collect 的 _add_evidence 逻辑
                        for ev in result.evidences:
                            idx = orch._add_evidence(ev)
                            if idx is not None:
                                injected += 1
                        # yield 一个 trace 事件
                        from datetime import datetime as _dt
                        await orch._emit_event("collect", "evidence", "PDF 解析完成",
                            f"上传 PDF 已添加 {injected} 条证据")
            except Exception as e:  # noqa: BLE001
                logger.warning("PDF 注入到 run 失败: %s", e)

        return {
            "filename": file.filename,
            "title": result.evidences[0].source_title if result.evidences else "",
            "total_pages": result.meta.get("total_pages", 0),
            "text_chunks": result.meta.get("text_chunks", 0),
            "table_chunks": result.meta.get("table_chunks", 0),
            "evidence_count": len(result.evidences),
            "injected_count": injected,
        }
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/api/parse-pdf-url")
def parse_pdf_url(req: PdfUrlReq):
    """从 URL 下载 PDF 并解析为证据（供 agent 搜索时自动调用）。

    - url: PDF 文件 URL
    - run_id: 绑定到 run
    - max_pages: 最多处理多少页
    """
    if not req.url or not req.url.lower().endswith(".pdf"):
        raise HTTPException(400, "请提供 .pdf 链接")
    from tools.finance_sources import GenericPdfAdapter
    adapter = GenericPdfAdapter()
    result = adapter.parse_pdf_url(req.url, max_pages=req.max_pages or 50)
    if result.meta.get("error"):
        raise HTTPException(500, f"PDF URL 解析失败: {result.meta['error']}")
    return {
        "url": req.url,
        "title": result.evidences[0].source_title if result.evidences else "",
        "total_pages": result.meta.get("total_pages", 0),
        "text_chunks": result.meta.get("text_chunks", 0),
        "table_chunks": result.meta.get("table_chunks", 0),
        "evidence_count": len(result.evidences),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
