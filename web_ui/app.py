import os
import sys
import secrets as py_secrets
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.document_loader import DocumentLoader, ALLOWED_EXTENSIONS
from rag.vector_store import VectorStore
from bot.database import Database
from config.settings import settings
from core.guards import normalize_spaces

app = FastAPI(title="Mike AI Admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
UPLOAD_DIR = Path("./data/uploads") / settings.TENANT_ID
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

doc_loader = DocumentLoader()
vector_store = VectorStore()
db = Database()
active_sessions: set[str] = set()

TENANT_ID = settings.TENANT_ID


# ---- Auth helpers ----

def check_auth(request: Request) -> bool:
    return request.cookies.get("mike_session") in active_sessions


def check_api_key(request: Request) -> bool:
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not settings.API_KEY:
        return check_auth(request)
    return key == settings.API_KEY


def require_auth(request: Request):
    if not check_auth(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})


def require_api_auth(request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def safe_filename(filename: str) -> str:
    name = os.path.basename(filename or "file")
    return "".join(c for c in name if c.isalnum() or c in "._-() ").strip()[:120] or "file.txt"


# ---- Auth routes ----

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if py_secrets.compare_digest(password, settings.WEB_PASSWORD):
        sid = py_secrets.token_hex(32)
        active_sessions.add(sid)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("mike_session", sid, httponly=True, samesite="lax", max_age=60 * 60 * 12)
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("mike_session")
    if sid in active_sessions:
        active_sessions.discard(sid)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("mike_session")
    return resp


# ---- Dashboard ----

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    dash = db.get_dashboard_stats(TENANT_ID)
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    active = db.get_active_users(TENANT_ID, days=7)
    conversion = db.get_lead_conversion_rate(TENANT_ID)
    top_questions = db.get_top_questions(TENANT_ID, limit=10)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "bot_name": settings.BOT_NAME,
        "tenant_id": TENANT_ID,
        "dash": dash,
        "today": today,
        "month": month,
        "active_users": active,
        "conversion": conversion,
        "top_questions": top_questions,
        "docs_count": vector_store.count(TENANT_ID),
        "sources": vector_store.list_sources(TENANT_ID),
    })


# ---- Documents ----

@app.get("/admin/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    sources = vector_store.list_sources(TENANT_ID)
    return templates.TemplateResponse("documents.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "sources": sources,
        "docs_count": vector_store.count(TENANT_ID),
        "max_files": settings.MAX_FILES_PER_TENANT,
        "max_chunks": settings.MAX_CHUNKS_PER_TENANT,
        "tenant_id": TENANT_ID,
    })


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    if not check_auth(request) and not check_api_key(request):
        return JSONResponse({"success": False, "error": "Не авторизован"}, status_code=401)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {"success": False, "error": f"Формат {ext} не поддерживается. Допустимые: {', '.join(ALLOWED_EXTENSIONS)}"}

    if vector_store.source_count(TENANT_ID) >= settings.MAX_FILES_PER_TENANT:
        return {"success": False, "error": f"Лимит файлов: максимум {settings.MAX_FILES_PER_TENANT}"}

    content = await file.read()
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        return {"success": False, "error": f"Файл слишком большой: максимум {settings.MAX_FILE_SIZE_MB} МБ"}
    if len(content) < 20:
        return {"success": False, "error": "Файл пустой или слишком маленький"}

    filename = safe_filename(file.filename)
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as f:
        f.write(content)

    try:
        docs = doc_loader.load_file(str(file_path), tenant_id=TENANT_ID)
        count = vector_store.add_documents(docs, tenant_id=TENANT_ID)
        return {"success": True, "filename": filename, "chunks": count}
    except Exception as e:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"success": False, "error": str(e)}


@app.post("/admin/documents/delete")
async def delete_document(request: Request, source: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login")
    vector_store.delete_source(TENANT_ID, source)
    return RedirectResponse("/admin/documents", status_code=302)


@app.get("/clear")
async def clear_kb(request: Request):
    if not check_auth(request):
        return {"error": "Не авторизован"}
    vector_store.clear(TENANT_ID)
    return {"success": True}


# ---- Leads ----

@app.get("/admin/leads", response_class=HTMLResponse)
async def leads_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    leads = db.list_leads(TENANT_ID)
    return templates.TemplateResponse("leads.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "leads": leads,
    })


@app.post("/admin/leads/status")
async def update_lead(request: Request, lead_id: int = Form(...), status: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login")
    db.update_lead_status(lead_id, status, TENANT_ID)
    return RedirectResponse("/admin/leads", status_code=302)


# ---- Missing Questions ----

@app.get("/admin/missing-questions", response_class=HTMLResponse)
async def missing_questions_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    questions = db.list_missing_questions(TENANT_ID)
    return templates.TemplateResponse("missing_questions.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "questions": questions,
    })


# ---- Feedback ----

@app.get("/admin/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    feedback = db.list_feedback(TENANT_ID)
    negative = db.get_negative_feedback(TENANT_ID)
    return templates.TemplateResponse("feedback.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "feedback": feedback,
        "negative": negative,
    })


# ---- Analytics ----

@app.get("/admin/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    dash = db.get_dashboard_stats(TENANT_ID)
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    active = db.get_active_users(TENANT_ID, days=7)
    conversion = db.get_lead_conversion_rate(TENANT_ID)
    top_questions = db.get_top_questions(TENANT_ID, limit=20)
    negative_fb = db.get_negative_feedback(TENANT_ID, limit=20)
    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "dash": dash,
        "today": today,
        "month": month,
        "active_users": active,
        "conversion": conversion,
        "top_questions": top_questions,
        "negative_fb": negative_fb,
    })


# ---- Settings ----

@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    plans = db.list_plans()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "tenant_id": TENANT_ID,
        "plans": plans,
        "settings": settings,
    })


# ---- Stats JSON (legacy compat) ----

@app.get("/stats")
async def stats(request: Request):
    if not check_auth(request) and not check_api_key(request):
        return {"error": "Не авторизован"}
    dash = db.get_dashboard_stats(TENANT_ID)
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    return {
        "tenant_id": TENANT_ID,
        "docs_in_rag": vector_store.count(TENANT_ID),
        "total_users": dash["users"],
        "total_messages": dash["messages"],
        "leads": dash["leads"],
        "leads_today": dash["leads_today"],
        "missing_questions": dash["missing_questions"],
        "negative_feedback": dash["negative_feedback"],
        "today": today,
        "month": month,
    }


# ============================================================
# API endpoints (all require API key or session)
# ============================================================

@app.post("/api/ask")
async def api_ask(request: Request):
    require_api_auth(request)
    body = await request.json()
    question = normalize_spaces(body.get("question", ""))
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)

    from services.llm_service import LLMService
    from core.guards import detect_intent, validate_query

    intent = detect_intent(question)
    quality = validate_query(question)
    if not quality.ok:
        return {"answer": "Не понял вопрос.", "intent": intent, "confidence": 0}

    docs = vector_store.search(question, tenant_id=TENANT_ID)
    confidence = docs[0]["score"] if docs else 0

    if not docs or confidence < settings.RAG_CONFIDENCE_THRESHOLD:
        return {
            "answer": "Я не нашёл точного ответа в базе знаний.",
            "intent": intent,
            "confidence": confidence,
            "sources": [],
        }

    llm_svc = LLMService()
    system_prompt = f"Ты ассистент компании {settings.COMPANY_NAME}. Отвечай коротко и по делу."
    context_str = "\n\n".join(d["content"][:1200] for d in docs)
    response = await llm_svc.ask(system_prompt, f"{context_str}\n\nВопрос: {question}")

    return {
        "answer": response,
        "intent": intent,
        "confidence": confidence,
        "sources": [d["source"] for d in docs],
    }


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    require_api_auth(request)
    return await upload_file(request, file)


@app.get("/api/stats")
async def api_stats(request: Request):
    require_api_auth(request)
    dash = db.get_dashboard_stats(TENANT_ID)
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    return {
        "tenant_id": TENANT_ID,
        "docs_in_rag": vector_store.count(TENANT_ID),
        **dash,
        "today": today,
        "month": month,
    }


@app.get("/api/leads")
async def api_leads(request: Request, limit: int = Query(100), offset: int = Query(0)):
    require_api_auth(request)
    return db.list_leads(TENANT_ID, limit=limit, offset=offset)


@app.get("/api/missing-questions")
async def api_missing(request: Request, limit: int = Query(100), offset: int = Query(0)):
    require_api_auth(request)
    return db.list_missing_questions(TENANT_ID, limit=limit, offset=offset)


@app.get("/api/feedback")
async def api_feedback(request: Request, limit: int = Query(100), offset: int = Query(0)):
    require_api_auth(request)
    return db.list_feedback(TENANT_ID, limit=limit, offset=offset)
