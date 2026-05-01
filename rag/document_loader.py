import os
from pathlib import Path

from langchain.schema import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, TextLoader

from config.settings import settings
from core.guards import normalize_spaces, strip_prompt_injection, validate_document_text

ALLOWED_EXTENSIONS = {".txt", ".pdf"}


class DocumentLoader:
    def __init__(self):
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "],
        )

    def _read_documents(self, file_path: str):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Формат {ext} не поддерживается. Допустимые: {', '.join(ALLOWED_EXTENSIONS)}"
            )
        if ext == ".pdf":
            return PyPDFLoader(file_path).load()
        if ext in (".txt", ".md"):
            return TextLoader(file_path, encoding="utf-8", autodetect_encoding=True).load()
        raise ValueError(f"Формат {ext} не поддерживается")

    def validate_file(self, file_path: str) -> tuple[bool, str]:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return False, f"Формат {ext} не поддерживается. Допустимые: {', '.join(ALLOWED_EXTENSIONS)}"
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > settings.MAX_FILE_SIZE_MB:
            return False, f"Файл слишком большой: {size_mb:.1f} МБ (максимум {settings.MAX_FILE_SIZE_MB} МБ)"
        if os.path.getsize(file_path) < 20:
            return False, "Файл пустой или слишком маленький"
        return True, ""

    def load_file(self, file_path: str, tenant_id: str = "default"):
        ok, error = self.validate_file(file_path)
        if not ok:
            raise ValueError(error)

        filename = os.path.basename(file_path)
        documents = self._read_documents(file_path)
        full_text = "\n\n".join(d.page_content or "" for d in documents)

        quality = validate_document_text(
            full_text, filename=filename, min_length=settings.MIN_TEXT_LENGTH
        )
        if not quality.ok:
            raise ValueError(quality.reason)

        cleaned_docs = []
        for doc in documents:
            text = strip_prompt_injection(doc.page_content or "")
            text = normalize_spaces(text)
            if text:
                cleaned_docs.append(
                    Document(page_content=text, metadata=doc.metadata)
                )

        chunks = self.text_splitter.split_documents(cleaned_docs)
        useful_chunks = []
        for i, chunk in enumerate(chunks):
            content = normalize_spaces(strip_prompt_injection(chunk.page_content))
            if len(content) < 80:
                continue
            chunk.page_content = content
            chunk.metadata["filename"] = filename
            chunk.metadata["tenant_id"] = tenant_id
            chunk.metadata["chunk_index"] = i
            useful_chunks.append(chunk)

        if not useful_chunks:
            raise ValueError("После очистки не осталось полезного текста")
        return useful_chunks
