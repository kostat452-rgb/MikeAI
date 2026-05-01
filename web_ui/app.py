from fastapi import FastAPI, File, UploadFile, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os, sys, secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.document_loader import DocumentLoader
from rag.vector_store import VectorStore
from bot.database import Database
from config.settings import settings

app = FastAPI(title="Mike AI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
UPLOAD_DIR = Path("./data/uploads") / settings.TENANT_ID
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

doc_loader = DocumentLoader()
vector_store = VectorStore()
db = Database()
active_sessions = set()


def check_auth(request: Request):
    return request.cookies.get("mike_session") in active_sessions


def safe_filename(filename: str) -> str:
    name = os.path.basename(filename or "file")
    return "".join(c for c in name if c.isalnum() or c in "._-() []а-яА-ЯёЁ").strip()[:120] or "file.txt"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, settings.WEB_PASSWORD):
        sid = secrets.token_hex(32)
        active_sessions.add(sid)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("mike_session", sid, httponly=True, samesite="lax", max_age=60 * 60 * 12)
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("mike_session")
    if sid in active_sessions:
        active_sessions.remove(sid)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("mike_session")
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    dash = db.get_dashboard_stats(settings.TENANT_ID)
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "bot_name": settings.BOT_NAME,
        "company_name": settings.COMPANY_NAME,
        "docs_count": vector_store.count(settings.TENANT_ID),
        "users_count": dash["users"],
        "messages_count": dash["messages"],
        "leads_count": dash["leads"],
        "missing_count": dash["missing_questions"],
        "tenant_id": settings.TENANT_ID,
    })


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    if not check_auth(request):
        return {"success": False, "error": "Не авторизован"}

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {"success": False, "error": f"Формат {ext} не поддерживается"}

    if vector_store.source_count(settings.TENANT_ID) >= settings.MAX_FILES_PER_TENANT:
        return {"success": False, "error": f"Лимит файлов: максимум {settings.MAX_FILES_PER_TENANT}"}

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        return {"success": False, "error": f"Файл слишком большой: максимум {settings.MAX_UPLOAD_MB} МБ"}
    if len(content) < 20:
        return {"success": False, "error": "Файл пустой или слишком маленький"}

    filename = safe_filename(file.filename)
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as f:
        f.write(content)

    try:
        docs = doc_loader.load_file(str(file_path), tenant_id=settings.TENANT_ID)
        count = vector_store.add_documents(docs, tenant_id=settings.TENANT_ID)
        return {"success": True, "filename": filename, "chunks": count}
    except Exception as e:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"success": False, "error": str(e)}


@app.get("/stats")
async def stats(request: Request):
    if not check_auth(request):
        return {"error": "Не авторизован"}
    dash = db.get_dashboard_stats(settings.TENANT_ID)
    today = db.get_today_stats(settings.TENANT_ID)
    month = db.get_month_stats(settings.TENANT_ID)
    return {
        "tenant_id": settings.TENANT_ID,
        "docs_in_rag": vector_store.count(settings.TENANT_ID),
        "total_users": dash["users"],
        "total_messages": dash["messages"],
        "leads": dash["leads"],
        "missing_questions": dash["missing_questions"],
        "today": today,
        "month": month,
    }


@app.get("/clear")
async def clear_kb(request: Request):
    if not check_auth(request):
        return {"error": "Не авторизован"}
    vector_store.clear(settings.TENANT_ID)
    return {"success": True}
