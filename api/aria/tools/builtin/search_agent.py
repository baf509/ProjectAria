"""
ARIA - Search Agent Tool (Chroma context-1)

Purpose: Run an agentic observe/reason/act retrieval loop driven by the local
chromadb/context-1 model, over three corpora:
  - ARIA long-term memory (hybrid vector + BM25 search)
  - The web (existing search provider + web fetch)
  - Local files under configured allowed roots

Returns a ranked list of documents for downstream reasoning (e.g. the
research service's learning extraction stage).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from aria.config import settings
from aria.llm.base import Message, Tool, ToolCall
from aria.llm.manager import llm_manager
from aria.memory.long_term import LongTermMemory
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.tools.builtin.web import WebTool

logger = logging.getLogger(__name__)


@dataclass
class Document:
    id: str
    source: str  # "memory" | "web" | "file"
    title: str
    content: str
    url: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "content": self.content[:2000],
            "metadata": self.metadata,
        }


_SYSTEM_PROMPT = """You are a search subagent. Your job is to retrieve the documents most relevant to a user's query using the tools available, then terminate by calling `finalize`.

Procedure:
1. Decompose the query into subqueries if it is complex.
2. Use `memory_search`, `web_search`, and `fs_grep` to gather candidates.
3. Use `web_read` / `fs_read` to fetch full content only when a candidate looks promising.
4. Use `prune` to drop documents you've judged irrelevant, freeing context.
5. Call `finalize` with the ranked list of document ids (most relevant first).

Termination budget — follow this strictly:
- You have at most 6 tool calls total across the whole search.
- After at most 3 search/grep calls, you MUST call `finalize`, even if results are sparse.
- Do NOT reformulate the same query with small wording variations. One query per distinct concept; if two queries return the same results, move on.
- A sparse or empty result set is a valid outcome — `finalize` with whatever you have (even an empty list) and the loop ends.
- Never exit without calling `finalize`. Never keep searching past the budget.

Ranking:
- Prefer precision over recall. Only include documents you would defend as directly relevant.
- Order the `ranked_ids` list by descending relevance.
"""


class SearchAgentTool(BaseTool):
    """Context-1 agentic search tool over memory, web, and local files."""

    def __init__(self, db, long_term_memory: Optional[LongTermMemory] = None):
        super().__init__()
        self.db = db
        self.long_term_memory = long_term_memory or LongTermMemory(db)
        # Imported lazily to avoid a circular import between this tool and
        # aria.research.service (which imports this tool).
        from aria.research.search import get_search_provider
        self.search_provider = get_search_provider()
        self.web_tool = WebTool(timeout_seconds=20, max_response_size=512 * 1024)

    @property
    def name(self) -> str:
        return "search_agent"

    @property
    def description(self) -> str:
        return (
            "Agentic search over ARIA's long-term memory, the web, and local files, "
            "driven by the local chromadb/context-1 model. Decomposes the query, "
            "iteratively searches, prunes irrelevant results, and returns a ranked "
            "list of the most relevant documents. Use this when you need "
            "high-quality retrieval before reasoning or synthesis."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="The information need to retrieve documents for.",
                required=True,
            ),
            ToolParameter(
                name="max_docs",
                type="integer",
                description="Maximum number of documents to return.",
                required=False,
            ),
            ToolParameter(
                name="corpora",
                type="array",
                description='Which corpora to search. Subset of ["memory","web","files"]. Defaults to all three.',
                required=False,
                items={"type": "string"},
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="query is required",
            )
        max_docs = int(arguments.get("max_docs") or settings.context1_max_docs)
        corpora = set(arguments.get("corpora") or ["memory", "web", "files"])

        try:
            adapter = llm_manager.get_adapter("context1", settings.context1_model)
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"context-1 adapter unavailable: {e}",
            )

        session = _SearchSession(
            memory=self.long_term_memory,
            search_provider=self.search_provider,
            web_tool=self.web_tool,
            fs_allowed_roots=settings.context1_fs_allowed_roots,
            corpora=corpora,
        )

        tools = _build_tool_schemas(corpora)
        messages: list[Message] = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=f"Query: {query}\nReturn up to {max_docs} ranked documents."),
        ]

        finalized_ids: list[str] = []
        logger.info(
            "search_agent starting | query=%r max_docs=%d corpora=%s",
            query[:120], max_docs, sorted(corpora),
        )
        max_iter = settings.context1_max_iterations
        for iteration in range(max_iter):
            # On the last allowed iteration, relax tool_choice so the model
            # is free to emit a natural-language wrap-up instead of being
            # forced to call yet another tool.
            tc_mode = "auto" if iteration == max_iter - 1 else "required"
            try:
                content, tool_calls, _usage = await _complete_with_tools(
                    adapter, messages=messages, tools=tools,
                    temperature=0.3, max_tokens=2048, tool_choice=tc_mode,
                )
            except Exception as e:
                logger.warning("context-1 call failed: %s", e)
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error=f"context-1 call failed: {e}",
                )

            logger.info(
                "search_agent iter=%d tool_calls=%s content=%r",
                iteration,
                [tc.name for tc in tool_calls],
                (content or "")[:120],
            )
            if not tool_calls:
                logger.info("search_agent iter=%d ended without tool calls", iteration)
                break

            assistant_tcs = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ]
            messages.append(Message(role="assistant", content=content or "", tool_calls=assistant_tcs))

            done = False
            for tc in tool_calls:
                if tc.name == "finalize":
                    ids = tc.arguments.get("ranked_ids") or []
                    if isinstance(ids, str):
                        try:
                            ids = json.loads(ids)
                        except Exception:
                            ids = [s.strip() for s in ids.split(",") if s.strip()]
                    finalized_ids = [str(i) for i in ids if isinstance(i, (str, int))]
                    messages.append(Message(
                        role="tool", tool_call_id=tc.id, name=tc.name,
                        content=json.dumps({"status": "ok", "count": len(finalized_ids)}),
                    ))
                    done = True
                    continue

                try:
                    result = await session.dispatch(tc.name, tc.arguments or {})
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
                summary = _summarize_tool_result(result)
                logger.info(
                    "search_agent dispatch | tool=%s args=%s -> %s | total_docs=%d",
                    tc.name,
                    _trim(tc.arguments),
                    summary,
                    len(session.docs),
                )
                messages.append(Message(
                    role="tool", tool_call_id=tc.id, name=tc.name,
                    content=json.dumps(result)[:6000],
                ))

            if done:
                break
        else:
            logger.info("search_agent hit iteration cap (%d)", settings.context1_max_iterations)

        ranked = session.rank(finalized_ids, max_docs=max_docs)
        logger.info(
            "search_agent done | finalized=%d discovered=%d returned=%d",
            len(finalized_ids), len(session.docs), len(ranked),
        )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output={
                "query": query,
                "documents": [doc.to_dict() for doc in ranked],
            },
            metadata={
                "iterations": iteration + 1 if finalized_ids or tool_calls else 0,
                "total_discovered": len(session.docs),
                "corpora": sorted(corpora),
            },
        )


# -----------------------------------------------------------------------------
# Session — holds discovered docs, executes tool calls
# -----------------------------------------------------------------------------

class _SearchSession:
    def __init__(
        self,
        *,
        memory: LongTermMemory,
        search_provider,
        web_tool: WebTool,
        fs_allowed_roots: list[str],
        corpora: set[str],
    ):
        self.memory = memory
        self.search_provider = search_provider
        self.web_tool = web_tool
        self.fs_allowed_roots = [os.path.abspath(r) for r in fs_allowed_roots]
        self.corpora = corpora
        self.docs: dict[str, Document] = {}
        self.pruned: set[str] = set()
        self._discovery_order: list[str] = []

    async def dispatch(self, name: str, args: dict) -> dict:
        if name == "memory_search" and "memory" in self.corpora:
            return await self._memory_search(args)
        if name == "web_search" and "web" in self.corpora:
            return await self._web_search(args)
        if name == "web_read" and "web" in self.corpora:
            return await self._web_read(args)
        if name == "fs_grep" and "files" in self.corpora:
            return await self._fs_grep(args)
        if name == "fs_read" and "files" in self.corpora:
            return await self._fs_read(args)
        if name == "prune":
            return self._prune(args)
        return {"error": f"unknown or disabled tool: {name}"}

    def _register(self, doc: Document) -> None:
        if doc.id in self.pruned:
            return
        if doc.id not in self.docs:
            self._discovery_order.append(doc.id)
        self.docs[doc.id] = doc

    async def _memory_search(self, args: dict) -> dict:
        query = (args.get("query") or "").strip()
        limit = min(int(args.get("limit") or 8), 20)
        if not query:
            return {"results": []}
        results = await self.memory.search(query=query, limit=limit)
        out = []
        for m in results:
            doc_id = f"mem:{m.id}"
            self._register(Document(
                id=doc_id,
                source="memory",
                title=(m.content[:80] + "…") if len(m.content) > 80 else m.content,
                content=m.content,
                metadata={"categories": m.categories, "importance": m.importance},
            ))
            out.append({
                "id": doc_id,
                "snippet": m.content[:400],
                "categories": m.categories,
            })
        return {"results": out}

    async def _web_search(self, args: dict) -> dict:
        query = (args.get("query") or "").strip()
        max_results = min(int(args.get("max_results") or 5), 10)
        if not query:
            return {"results": []}
        results = await self.search_provider.search(query, max_results=max_results)
        out = []
        for r in results:
            doc_id = f"web:{hashlib.sha1(r.url.encode()).hexdigest()[:12]}"
            self._register(Document(
                id=doc_id, source="web", title=r.title, content=r.snippet, url=r.url,
                metadata={"snippet_only": True},
            ))
            out.append({"id": doc_id, "title": r.title, "url": r.url, "snippet": r.snippet})
        return {"results": out}

    async def _web_read(self, args: dict) -> dict:
        doc_id = args.get("id") or ""
        doc = self.docs.get(doc_id)
        if not doc or doc.source != "web" or not doc.url:
            return {"error": f"unknown web id: {doc_id}"}
        fetch = await self.web_tool.execute({"url": doc.url, "timeout": 20})
        if fetch.status != ToolStatus.SUCCESS:
            return {"error": fetch.error or "fetch failed"}
        content = str((fetch.output or {}).get("content", ""))[:12000]
        doc.content = _strip_html(content) or doc.content
        doc.metadata["snippet_only"] = False
        return {"id": doc_id, "content": doc.content[:4000]}

    async def _fs_grep(self, args: dict) -> dict:
        pattern = args.get("pattern") or ""
        path = args.get("path") or (self.fs_allowed_roots[0] if self.fs_allowed_roots else ".")
        path = self._resolve_allowed(path)
        if not path:
            return {"error": "path outside allowed roots"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", "--no-heading", "--with-filename", "--line-number",
                "--max-count", "5", "--max-filesize", "2M", "-S",
                pattern, path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            return {"error": "ripgrep (rg) not available on this host"}
        except asyncio.TimeoutError:
            return {"error": "grep timed out"}

        hits = []
        for line in stdout.decode("utf-8", errors="replace").splitlines()[:50]:
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, line_no, snippet = parts
            doc_id = f"file:{hashlib.sha1(file_path.encode()).hexdigest()[:12]}"
            self._register(Document(
                id=doc_id, source="file",
                title=os.path.basename(file_path), content=snippet[:400],
                metadata={"path": file_path, "line": line_no},
            ))
            hits.append({"id": doc_id, "path": file_path, "line": line_no, "snippet": snippet[:200]})
        return {"hits": hits}

    async def _fs_read(self, args: dict) -> dict:
        target = args.get("path") or ""
        resolved = self._resolve_allowed(target)
        if not resolved or not os.path.isfile(resolved):
            return {"error": "path outside allowed roots or not a file"}
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(60000)
        except Exception as e:
            return {"error": str(e)}
        doc_id = f"file:{hashlib.sha1(resolved.encode()).hexdigest()[:12]}"
        self._register(Document(
            id=doc_id, source="file", title=os.path.basename(resolved),
            content=content, metadata={"path": resolved},
        ))
        return {"id": doc_id, "path": resolved, "content": content[:4000]}

    def _prune(self, args: dict) -> dict:
        ids = args.get("doc_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        removed = 0
        for doc_id in ids:
            if doc_id in self.docs:
                del self.docs[doc_id]
                removed += 1
            self.pruned.add(doc_id)
        return {"pruned": removed}

    def _resolve_allowed(self, path: str) -> Optional[str]:
        if not path:
            return None
        resolved = os.path.abspath(os.path.expanduser(path))
        for root in self.fs_allowed_roots:
            if resolved == root or resolved.startswith(root + os.sep):
                return resolved
        return None

    def rank(self, finalized_ids: list[str], max_docs: int) -> list[Document]:
        ordered: list[Document] = []
        seen: set[str] = set()
        for doc_id in finalized_ids:
            if doc_id in self.docs and doc_id not in seen:
                ordered.append(self.docs[doc_id])
                seen.add(doc_id)
        # Fall back to discovery order for anything the model didn't explicitly rank
        for doc_id in self._discovery_order:
            if doc_id in self.docs and doc_id not in seen:
                ordered.append(self.docs[doc_id])
                seen.add(doc_id)
        return ordered[:max_docs]


# -----------------------------------------------------------------------------
# Tool schemas exposed to context-1
# -----------------------------------------------------------------------------

def _build_tool_schemas(corpora: set[str]) -> list[Tool]:
    tools: list[Tool] = []
    if "memory" in corpora:
        tools.append(Tool(
            name="memory_search",
            description="Search ARIA's long-term memory (hybrid vector + BM25). Returns snippets with doc ids prefixed 'mem:'.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "description": "Max results (1-20)."},
                },
                "required": ["query"],
            },
        ))
    if "web" in corpora:
        tools.append(Tool(
            name="web_search",
            description="Search the web. Returns titles, urls, snippets; doc ids prefixed 'web:'. Snippets only — call web_read to fetch full content.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        ))
        tools.append(Tool(
            name="web_read",
            description="Fetch the full content of a web document previously returned by web_search.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "A 'web:' document id."}},
                "required": ["id"],
            },
        ))
    if "files" in corpora:
        tools.append(Tool(
            name="fs_grep",
            description="ripgrep-style search over local files under allowed roots. Returns matches with doc ids prefixed 'file:'.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Optional subdirectory within an allowed root."},
                },
                "required": ["pattern"],
            },
        ))
        tools.append(Tool(
            name="fs_read",
            description="Read a local file under allowed roots. Returns its first 60KB.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ))
    tools.append(Tool(
        name="prune",
        description="Remove documents from the working set to free context capacity.",
        parameters={
            "type": "object",
            "properties": {
                "doc_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["doc_ids"],
        },
    ))
    tools.append(Tool(
        name="finalize",
        description="Return the final ranked list of document ids (most relevant first) and end the search.",
        parameters={
            "type": "object",
            "properties": {
                "ranked_ids": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["ranked_ids"],
        },
    ))
    return tools


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style.*?</style>", re.DOTALL | re.IGNORECASE)


async def _complete_with_tools(
    adapter,
    *,
    messages: list[Message],
    tools: list[Tool],
    temperature: float,
    max_tokens: int,
    tool_choice: str = "required",
) -> tuple[str, list[ToolCall], dict]:
    """Call an OpenAI-compatible adapter with an explicit `tool_choice`.

    The default `adapter.complete()` doesn't expose `tool_choice`, and context-1
    on llama.cpp only emits OpenAI-format tool_calls when the grammar path
    forces a function call. The loop uses 'required' for most iterations (with
    a `finalize` tool always available for clean termination) and switches to
    'auto' on the final iteration to let the model wrap up in natural language.
    """
    openai_messages = adapter._convert_messages(messages)  # type: ignore[attr-defined]
    openai_tools = adapter._convert_tools(tools)  # type: ignore[attr-defined]
    resp = await adapter.client.chat.completions.create(  # type: ignore[attr-defined]
        model=adapter.model,
        messages=openai_messages,
        tools=openai_tools,
        tool_choice=tool_choice,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    choice = resp.choices[0]
    msg = choice.message
    content = msg.content or ""
    tool_calls: list[ToolCall] = []
    for tc in (msg.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except Exception:
            args = {}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
    usage = {}
    if getattr(resp, "usage", None):
        usage = {
            "input_tokens": resp.usage.prompt_tokens,
            "output_tokens": resp.usage.completion_tokens,
        }
    return content, tool_calls, usage


def _summarize_tool_result(result: dict) -> str:
    if not isinstance(result, dict):
        return str(type(result).__name__)
    if "error" in result:
        return f"error={result['error']!s:.80}"
    if "results" in result:
        return f"results={len(result['results'])}"
    if "hits" in result:
        return f"hits={len(result['hits'])}"
    if "pruned" in result:
        return f"pruned={result['pruned']}"
    if "content" in result:
        return f"content={len(result.get('content', ''))}b"
    return "ok"


def _trim(value, limit: int = 120) -> str:
    try:
        s = json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _strip_html(value: str) -> str:
    value = _SCRIPT_RE.sub(" ", value)
    value = _STYLE_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()
