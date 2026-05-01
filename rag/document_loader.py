import os
from pathlib import Path
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain.schema import Document
import docx2txt
from config.settings import settings
from core.guards import validate_document_text, strip_prompt_injection, normalize_spaces

class DocumentLoader:
    def __init__(self):
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "],
        )

    def _read_documents(self, file_path: str):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".pdf":
            return PyPDFLoader(file_path).load()
        if ext == ".docx":
            text = docx2txt.process(file_path) or ""
            return [Document(page_content=text, metadata={"source": file_path})]
        if ext in [".txt", ".md"]:
            return TextLoader(file_path, encoding="utf-8", autodetect_encoding=True).load()
        raise ValueError(f"Формат {ext} не поддерживается")

    def load_file(self, file_path: str, tenant_id: str = "default"):
        filename = os.path.basename(file_path)
        documents = self._read_documents(file_path)
        full_text = "\n\n".join(d.page_content or "" for d in documents)
        quality = validate_document_text(full_text, filename=filename)
        if not quality.ok:
            raise ValueError(quality.reason)

        cleaned_docs = []
        for doc in documents:
            text = strip_prompt_injection(doc.page_content or "")
            text = normalize_spaces(text)
            if text:
                cleaned_docs.append(Document(page_content=text, metadata=doc.metadata))

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
