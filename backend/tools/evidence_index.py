"""证据向量检索 —— 嵌入语义检索（首选）+ TF-IDF（降级），零硬依赖。

P1.4 升级：优先使用 sentence-transformers 嵌入向量（跨表述/跨语言语义检索），
不可用时降级到 TF-IDF（纯 numpy，零外部依赖）。

设计原则：
- 渐进增强：有 sentence-transformers → 嵌入检索；无 → TF-IDF 降级
- 透明：可 inspect 检索模式（embedding vs tfidf）
- 中文友好：嵌入模型选 multilingual；TF-IDF 用字符级 n-gram
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

_EMBEDDER = None
_EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
_HAS_EMBEDDER = False


def _semantic_index_enabled() -> bool:
    return os.getenv("ENABLE_SEMANTIC_INDEX", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_embedder():
    """Load the optional embedding model only when explicitly enabled."""
    global _EMBEDDER, _HAS_EMBEDDER
    if not _semantic_index_enabled():
        _HAS_EMBEDDER = False
        return None
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from sentence_transformers import SentenceTransformer

        _EMBEDDER = SentenceTransformer(_EMBED_MODEL)
        _HAS_EMBEDDER = True
        return _EMBEDDER
    except Exception:
        _HAS_EMBEDDER = False
        return None


def _tokenize(text: str) -> list[str]:
    """中文+英文混合分词：英文按空格/标点分，中文按 2-gram 切。"""
    text = text.lower().strip()
    # 英文词
    en_tokens = re.findall(r'[a-z0-9]{2,}', text)
    # 中文字符级 2-gram
    cn_chars = re.findall(r'[\u4e00-\u9fff]', text)
    cn_grams = ["".join(cn_chars[i:i+2]) for i in range(len(cn_chars) - 1)]
    # 中文 3-gram（捕获更完整的术语）
    cn_3grams = ["".join(cn_chars[i:i+3]) for i in range(len(cn_chars) - 2)]
    return en_tokens + cn_grams + cn_3grams


class EvidenceIndex:
    """证据语义检索索引。

    P1.4: 优先嵌入检索（sentence-transformers），降级 TF-IDF（numpy），最终回退关键词匹配。

    1. add(idx, text)  — 添加证据
    2. build()         — 构建索引（嵌入矩阵或 TF-IDF 矩阵）
    3. search(query, top_k) — 返回 [(evidence_idx, score), ...] 按相关性降序
    """

    def __init__(self):
        self._docs: dict[int, str] = {}  # idx → raw text（嵌入模式需要原文）
        self._token_docs: dict[int, list[str]] = {}  # idx → tokens（TF-IDF 模式）
        self._idf: dict[str, float] = {}
        self._tfidf_matrix: Optional[object] = None
        self._embed_matrix: Optional[object] = None  # 嵌入矩阵
        self._indices: list[int] = []
        self._vocab: list[str] = []

    @property
    def mode(self) -> str:
        """当前检索模式：embedding / tfidf / keyword。"""
        if _HAS_EMBEDDER and self._embed_matrix is not None:
            return "embedding"
        if _HAS_NUMPY and self._tfidf_matrix is not None:
            return "tfidf"
        return "keyword"

    def add(self, idx: int, text: str):
        """添加一条证据到索引。"""
        if text:
            self._docs[idx] = text
            tokens = _tokenize(text)
            if tokens:
                self._token_docs[idx] = tokens

    def build(self):
        """构建索引。优先嵌入，降级 TF-IDF。"""
        if not self._docs:
            return

        # P1.4: 嵌入模式
        embedder = _get_embedder()
        if embedder is not None and _HAS_NUMPY:
            texts = []
            self._indices = list(self._docs.keys())
            for idx in self._indices:
                texts.append(self._docs[idx][:500])  # 截断防止超长
            try:
                embeddings = embedder.encode(texts, normalize_embeddings=True,
                                             show_progress_bar=False)
                self._embed_matrix = np.array(embeddings, dtype=np.float32)
                return
            except Exception:
                pass  # 嵌入失败，降级 TF-IDF

        # TF-IDF 模式（原有逻辑）
        if not self._token_docs or not _HAS_NUMPY:
            return

        # 构建词表
        all_tokens = set()
        for tokens in self._token_docs.values():
            all_tokens.update(tokens)
        self._vocab = sorted(all_tokens)
        vocab_size = len(self._vocab)
        n_docs = len(self._token_docs)
        if vocab_size == 0 or n_docs == 0:
            return

        # 计算 IDF
        df = Counter()
        for tokens in self._token_docs.values():
            for t in set(tokens):
                df[t] += 1
        self._idf = {t: math.log((n_docs + 1) / (df[t] + 1)) + 1 for t in self._vocab}

        # 构建 TF-IDF 矩阵
        self._indices = list(self._token_docs.keys())
        self._tfidf_matrix = np.zeros((n_docs, vocab_size), dtype=np.float32)
        vocab_index = {t: i for i, t in enumerate(self._vocab)}
        for i, idx in enumerate(self._indices):
            tokens = self._token_docs[idx]
            tf = Counter(tokens)
            total = len(tokens) if tokens else 1
            for t, count in tf.items():
                j = vocab_index[t]
                self._tfidf_matrix[i, j] = (count / total) * self._idf.get(t, 1)

        # L2 归一化
        norms = np.linalg.norm(self._tfidf_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self._tfidf_matrix /= norms

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """检索与 query 最相关的 top_k 条证据。返回 [(evidence_idx, score), ...]。

        P1.4: 嵌入模式 → 语义相似度；TF-IDF 模式 → 关键词匹配。
        """
        if not self._docs:
            return []

        # P1.4: 嵌入模式
        embedder = _get_embedder()
        if embedder is not None and _HAS_NUMPY and self._embed_matrix is not None:
            try:
                q_emb = embedder.encode([query[:500]], normalize_embeddings=True,
                                        show_progress_bar=False)
                q_vec = np.array(q_emb[0], dtype=np.float32)
                scores = self._embed_matrix @ q_vec
                top_indices = np.argsort(scores)[::-1][:top_k]
                results = []
                for i in top_indices:
                    if scores[i] > 0.05:
                        results.append((self._indices[i], float(scores[i])))
                return results
            except Exception:
                pass  # 嵌入检索失败，降级

        # TF-IDF 模式
        if _HAS_NUMPY and self._tfidf_matrix is not None:
            query_tokens = _tokenize(query)
            if not query_tokens:
                return []
            # 构建 query 向量
            query_vec = np.zeros(len(self._vocab), dtype=np.float32)
            vocab_index = {t: i for i, t in enumerate(self._vocab)}
            tf = Counter(query_tokens)
            total = len(query_tokens)
            for t, count in tf.items():
                if t in vocab_index:
                    query_vec[vocab_index[t]] = (count / total) * self._idf.get(t, 1)
            # 归一化 + cosine
            norm = np.linalg.norm(query_vec)
            if norm > 0:
                query_vec /= norm
            scores = self._tfidf_matrix @ query_vec
            # 取 top_k
            top_indices = np.argsort(scores)[::-1][:top_k]
            results = []
            for i in top_indices:
                if scores[i] > 0.01:  # 过滤极低分
                    results.append((self._indices[i], float(scores[i])))
            return results

        # 回退：关键词匹配
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []
        results = []
        for idx, tokens in self._token_docs.items():
            overlap = len(query_tokens & set(tokens))
            if overlap > 0:
                score = overlap / len(query_tokens)
                results.append((idx, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def search_multi(self, queries: list[str], top_k: int = 10) -> list[tuple[int, float]]:
        """多查询合并检索：对每个 query 检索后取 max score 合并。"""
        best: dict[int, float] = {}
        for q in queries:
            for idx, score in self.search(q, top_k):
                if idx not in best or score > best[idx]:
                    best[idx] = score
        return sorted(best.items(), key=lambda x: x[1], reverse=True)[:top_k]

    @property
    def size(self) -> int:
        return len(self._docs)

    @property
    def has_numpy(self) -> bool:
        return _HAS_NUMPY and (self._tfidf_matrix is not None or self._embed_matrix is not None)

    @property
    def has_embedder(self) -> bool:
        """P1.4: 是否使用嵌入检索模式。"""
        return _HAS_EMBEDDER and self._embed_matrix is not None
