import json
import math
import tempfile
from pathlib import Path
from typing import Iterable
from sentence_transformers import SentenceTransformer
from config.settings import settings

class VectorStore:
    def __init__(self):
        self.data_dir = Path(settings.CHROMA_PERSIST_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "documents.json"
        self.model = SentenceTransformer(settings.EMBEDDING_MODEL)
        self.documents = self._load()

    def _load(self):
        if self.data_file.exists():
            with open(self.data_file, "r", encoding="utf-8") as f:
                docs = json.load(f)
            for d in docs:
                d.setdefault("tenant_id", "default")
            return docs
        return []

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=self.data_dir) as f:
            json.dump(self.documents, f, ensure_ascii=False)
            tmp = Path(f.name)
        tmp.replace(self.data_file)

    @staticmethod
    def _norm(vec):
        s = math.sqrt(sum(float(x) * float(x) for x in vec))
        if not s:
            return vec
        return [float(x) / s for x in vec]

    @staticmethod
    def _score(a, b):
        return sum(float(x) * float(y) for x, y in zip(a, b))

    def count(self, tenant_id: str | None = None):
        if tenant_id is None:
            return len(self.documents)
        return sum(1 for d in self.documents if d.get("tenant_id", "default") == tenant_id)

    def source_count(self, tenant_id: str = "default"):
        return len({d.get("source", "") for d in self.documents if d.get("tenant_id", "default") == tenant_id})

    def add_documents(self, documents, tenant_id: str = "default"):
        if self.count(tenant_id) + len(documents) > settings.MAX_CHUNKS_PER_TENANT:
            raise ValueError(f"Лимит базы знаний превышен: максимум {settings.MAX_CHUNKS_PER_TENANT} фрагментов")
        batch_texts = [doc.page_content for doc in documents]
        embeddings = self.model.encode(batch_texts, normalize_embeddings=True)
        for doc, embedding in zip(documents, embeddings):
            self.documents.append({
                "tenant_id": tenant_id,
                "content": doc.page_content,
                "source": doc.metadata.get("filename", ""),
                "chunk_index": doc.metadata.get("chunk_index", 0),
                "embedding": self._norm(embedding.tolist() if hasattr(embedding, "tolist") else embedding),
            })
        self._save()
        return len(documents)

    def search(self, query: str, tenant_id: str = "default", top_k: int | None = None, min_score: float | None = None):
        top_k = top_k or settings.TOP_K_RESULTS
        min_score = settings.MIN_RAG_SCORE if min_score is None else min_score
        tenant_docs = [d for d in self.documents if d.get("tenant_id", "default") == tenant_id]
        if not tenant_docs:
            return []
        query_embedding = self.model.encode(query, normalize_embeddings=True)
        query_embedding = self._norm(query_embedding.tolist() if hasattr(query_embedding, "tolist") else query_embedding)
        results = []
        for doc in tenant_docs:
            score = self._score(query_embedding, doc["embedding"])
            if score >= min_score:
                results.append({
                    "content": doc["content"],
                    "source": doc["source"],
                    "score": round(float(score), 4),
                    "distance": round(1 - float(score), 4),
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def clear(self, tenant_id: str | None = None):
        if tenant_id is None:
            self.documents = []
        else:
            self.documents = [d for d in self.documents if d.get("tenant_id", "default") != tenant_id]
        self._save()
