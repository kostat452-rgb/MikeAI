import json
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from config.settings import settings

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False


class VectorStore:
    def __init__(self):
        self.base_dir = Path(settings.CHROMA_PERSIST_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.model = SentenceTransformer(settings.EMBEDDING_MODEL)
        self._dim: Optional[int] = None
        self._tenant_data: dict[str, dict] = {}

    def _tenant_dir(self, tenant_id: str) -> Path:
        d = self.base_dir / "tenants" / tenant_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_tenant(self, tenant_id: str) -> dict:
        if tenant_id in self._tenant_data:
            return self._tenant_data[tenant_id]

        tdir = self._tenant_dir(tenant_id)
        chunks_file = tdir / "chunks.json"
        index_file = tdir / "index.faiss"
        legacy_file = self.base_dir / "documents.json"

        chunks = []
        embeddings = []

        if chunks_file.exists():
            with open(chunks_file, "r", encoding="utf-8") as f:
                chunks = json.load(f)
            if HAS_FAISS and index_file.exists():
                index = faiss.read_index(str(index_file))
                data = {"chunks": chunks, "faiss_index": index}
                self._tenant_data[tenant_id] = data
                return data
            else:
                embeddings = [c.get("embedding", []) for c in chunks]
        elif legacy_file.exists():
            with open(legacy_file, "r", encoding="utf-8") as f:
                all_docs = json.load(f)
            chunks = [d for d in all_docs if d.get("tenant_id", "default") == tenant_id]
            embeddings = [c.get("embedding", []) for c in chunks]

        data = {"chunks": chunks, "faiss_index": None}
        if HAS_FAISS and chunks and embeddings and embeddings[0]:
            dim = len(embeddings[0])
            index = faiss.IndexFlatIP(dim)
            vecs = np.array(embeddings, dtype=np.float32)
            faiss.normalize_L2(vecs)
            index.add(vecs)
            data["faiss_index"] = index

        self._tenant_data[tenant_id] = data
        return data

    def _save_tenant(self, tenant_id: str):
        data = self._tenant_data.get(tenant_id)
        if not data:
            return
        tdir = self._tenant_dir(tenant_id)
        chunks_file = tdir / "chunks.json"

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=tdir) as f:
            json.dump(data["chunks"], f, ensure_ascii=False)
            tmp = Path(f.name)
        tmp.replace(chunks_file)

        if HAS_FAISS and data.get("faiss_index") is not None:
            index_file = tdir / "index.faiss"
            faiss.write_index(data["faiss_index"], str(index_file))

    def count(self, tenant_id: Optional[str] = None):
        if tenant_id is None:
            total = 0
            tdir = self.base_dir / "tenants"
            if tdir.exists():
                for d in tdir.iterdir():
                    if d.is_dir():
                        data = self._load_tenant(d.name)
                        total += len(data["chunks"])
            return total
        data = self._load_tenant(tenant_id)
        return len(data["chunks"])

    def source_count(self, tenant_id: str = "default"):
        data = self._load_tenant(tenant_id)
        return len({c.get("source", "") for c in data["chunks"]})

    def list_sources(self, tenant_id: str = "default"):
        data = self._load_tenant(tenant_id)
        sources = {}
        for c in data["chunks"]:
            src = c.get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1
        return [{"name": k, "chunks": v} for k, v in sources.items()]

    def add_documents(self, documents, tenant_id: str = "default"):
        data = self._load_tenant(tenant_id)
        if len(data["chunks"]) + len(documents) > settings.MAX_CHUNKS_PER_TENANT:
            raise ValueError(
                f"Лимит базы знаний превышен: максимум {settings.MAX_CHUNKS_PER_TENANT} фрагментов"
            )

        batch_texts = [doc.page_content for doc in documents]
        embeddings = self.model.encode(batch_texts, normalize_embeddings=True)

        new_chunks = []
        new_vecs = []
        for doc, embedding in zip(documents, embeddings):
            vec = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            chunk = {
                "tenant_id": tenant_id,
                "content": doc.page_content,
                "source": doc.metadata.get("filename", ""),
                "chunk_index": doc.metadata.get("chunk_index", 0),
                "embedding": vec,
            }
            new_chunks.append(chunk)
            new_vecs.append(vec)

        data["chunks"].extend(new_chunks)

        if HAS_FAISS and new_vecs:
            vecs_np = np.array(new_vecs, dtype=np.float32)
            faiss.normalize_L2(vecs_np)
            if data.get("faiss_index") is None:
                dim = len(new_vecs[0])
                data["faiss_index"] = faiss.IndexFlatIP(dim)
            data["faiss_index"].add(vecs_np)

        self._save_tenant(tenant_id)
        return len(documents)

    def search(
        self,
        query: str,
        tenant_id: str = "default",
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ):
        top_k = top_k or settings.TOP_K_RESULTS
        min_score = settings.RAG_CONFIDENCE_THRESHOLD if min_score is None else min_score

        data = self._load_tenant(tenant_id)
        chunks = data["chunks"]
        if not chunks:
            return []

        query_embedding = self.model.encode(query, normalize_embeddings=True)
        query_vec = query_embedding.tolist() if hasattr(query_embedding, "tolist") else list(query_embedding)

        if HAS_FAISS and data.get("faiss_index") is not None:
            q = np.array([query_vec], dtype=np.float32)
            faiss.normalize_L2(q)
            scores, indices = data["faiss_index"].search(q, min(top_k * 2, len(chunks)))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(chunks):
                    continue
                if float(score) >= min_score:
                    c = chunks[idx]
                    results.append({
                        "content": c["content"],
                        "source": c["source"],
                        "score": round(float(score), 4),
                        "distance": round(1 - float(score), 4),
                    })
            return results[:top_k]

        # Fallback: brute-force cosine similarity
        results = []
        for chunk in chunks:
            emb = chunk.get("embedding", [])
            if not emb:
                continue
            score = sum(float(a) * float(b) for a, b in zip(query_vec, emb))
            if score >= min_score:
                results.append({
                    "content": chunk["content"],
                    "source": chunk["source"],
                    "score": round(float(score), 4),
                    "distance": round(1 - float(score), 4),
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def clear(self, tenant_id: Optional[str] = None):
        if tenant_id is None:
            self._tenant_data = {}
            tdir = self.base_dir / "tenants"
            if tdir.exists():
                import shutil
                shutil.rmtree(tdir)
            legacy = self.base_dir / "documents.json"
            if legacy.exists():
                legacy.unlink()
        else:
            self._tenant_data.pop(tenant_id, None)
            tdir = self._tenant_dir(tenant_id)
            for f in tdir.iterdir():
                f.unlink()

    def delete_source(self, tenant_id: str, source_name: str):
        data = self._load_tenant(tenant_id)
        remaining = [c for c in data["chunks"] if c.get("source") != source_name]
        data["chunks"] = remaining
        # Rebuild FAISS index
        if HAS_FAISS and remaining:
            embeddings = [c.get("embedding", []) for c in remaining]
            if embeddings and embeddings[0]:
                dim = len(embeddings[0])
                index = faiss.IndexFlatIP(dim)
                vecs = np.array(embeddings, dtype=np.float32)
                faiss.normalize_L2(vecs)
                index.add(vecs)
                data["faiss_index"] = index
            else:
                data["faiss_index"] = None
        elif HAS_FAISS:
            data["faiss_index"] = None
        self._save_tenant(tenant_id)
