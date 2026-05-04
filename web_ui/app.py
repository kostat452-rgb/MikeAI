import csv
import io
import os
import sys
import secrets as py_secrets
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.document_loader import DocumentLoader, ALLOWED_EXTENSIONS
from rag.vector_store import VectorStore
from bot.database import Database
from config.settings import settings
from core.guards import normalize_spaces

app = FastAPI(title="Mike AI Admin")


@app.get("/health")
async def health():
    return {"status": "ok", "tenant": settings.TENANT_ID}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
UPLOAD_DIR = Path("./data/uploads") / settings.TENANT_ID
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

doc_loader = DocumentLoader()
vector_store = VectorStore()
db = Database()

# ---- Persistent sessions in DB ----
_sessions_table_ready = False
def _ensure_sessions_table():
    global _sessions_table_ready
    if _sessions_table_ready:
        return
    with db._connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
            sid TEXT PRIMARY KEY,
            session_type TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        )""")
    _sessions_table_ready = True

def _add_session(sid: str, session_type: str, max_age: int):
    _ensure_sessions_table()
    from datetime import datetime, timedelta
    expires = (datetime.utcnow() + timedelta(seconds=max_age)).isoformat()
    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO web_sessions (sid, session_type, expires_at) VALUES (?,?,?)",
                     (sid, session_type, expires))

def _check_session(sid: str, session_type: str) -> bool:
    if not sid:
        return False
    _ensure_sessions_table()
    from datetime import datetime
    with db._connect() as conn:
        row = conn.execute("SELECT expires_at FROM web_sessions WHERE sid=? AND session_type=?",
                           (sid, session_type)).fetchone()
        if not row:
            return False
        if row["expires_at"] and row["expires_at"] < datetime.utcnow().isoformat():
            conn.execute("DELETE FROM web_sessions WHERE sid=?", (sid,))
            return False
        return True

def _remove_session(sid: str):
    if not sid:
        return
    _ensure_sessions_table()
    with db._connect() as conn:
        conn.execute("DELETE FROM web_sessions WHERE sid=?", (sid,))

TENANT_ID = settings.TENANT_ID
MAIN_DOMAIN = "mike-ai.ru"

# ---- Rate limiting for login ----
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes

def _check_login_rate(ip: str) -> bool:
    """Return True if login is allowed, False if rate-limited."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Remove old attempts outside window
    _login_attempts[ip] = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    return len(_login_attempts[ip]) < LOGIN_MAX_ATTEMPTS

def _record_login_attempt(ip: str):
    _login_attempts[ip].append(time.time())


# ---- Subdomain → tenant_id middleware ----

class SubdomainMiddleware(BaseHTTPMiddleware):
    """Detect tenant from subdomain: salon.mike-ai.ru → tenant_id='salon'."""
    async def dispatch(self, request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host.endswith(f".{MAIN_DOMAIN}"):
            sub = host.replace(f".{MAIN_DOMAIN}", "")
            if sub and sub not in ("www", "api", "owner"):
                request.state.subdomain_tenant = sub
            else:
                request.state.subdomain_tenant = None
        else:
            request.state.subdomain_tenant = None
        return await call_next(request)

app.add_middleware(SubdomainMiddleware)


# ---- Auth helpers ----

def check_auth(request: Request) -> bool:
    return _check_session(request.cookies.get("mike_session"), "client")


def check_owner_auth(request: Request) -> bool:
    return _check_session(request.cookies.get("owner_session"), "owner")


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
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(ip):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Слишком много попыток. Подождите 5 минут."})
    if py_secrets.compare_digest(password, settings.WEB_PASSWORD):
        sid = py_secrets.token_hex(32)
        max_age = 60 * 60 * 12
        _add_session(sid, "client", max_age)
        resp = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie("mike_session", sid, httponly=True, samesite="lax", secure=True, max_age=max_age)
        return resp
    _record_login_attempt(ip)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("mike_session")
    _remove_session(sid)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("mike_session")
    return resp


# ---- Landing page (public) ----

@app.get("/", response_class=HTMLResponse)
async def landing_or_dashboard(request: Request):
    if check_auth(request):
        return RedirectResponse("/dashboard")
    resp = templates.TemplateResponse("landing.html", {
        "request": request,
        "bot_username": settings.BOT_NAME or "mike_ai_bot",
        "api_key": settings.API_KEY or "",
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ---- Dashboard ----

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    dash = db.get_dashboard_stats(TENANT_ID)
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    active = db.get_active_users(TENANT_ID, days=7)
    conversion = db.get_lead_conversion_rate(TENANT_ID)
    top_questions = db.get_top_questions(TENANT_ID, limit=10)
    usage = db.get_usage_vs_limits(TENANT_ID, settings.DAILY_REQUEST_LIMIT, settings.MONTHLY_TOKEN_LIMIT)
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
        "vk_enabled": settings.VK_ENABLED and bool(settings.VK_GROUP_TOKEN),
        "widget_enabled": settings.WIDGET_ENABLED,
        "usage": usage,
    })


# ---- Documents ----

@app.get("/admin/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    sources = vector_store.list_sources(TENANT_ID)
    uploaded_docs = db.list_uploaded_documents(TENANT_ID)
    return templates.TemplateResponse("documents.html", {
        "request": request,
        "company_name": settings.COMPANY_NAME,
        "sources": sources,
        "uploaded_docs": uploaded_docs,
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
        db.save_uploaded_document(TENANT_ID, filename, file_size=len(content), chunks_count=count)
        db.clear_all_cache(TENANT_ID)
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
    db.delete_uploaded_document(TENANT_ID, source)
    db.clear_all_cache(TENANT_ID)
    return RedirectResponse("/admin/documents", status_code=302)


@app.get("/clear")
async def clear_kb(request: Request):
    if not check_auth(request):
        return {"error": "Не авторизован"}
    vector_store.clear(TENANT_ID)
    db.clear_all_cache(TENANT_ID)
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


@app.get("/admin/leads/export")
async def export_leads_csv(request: Request):
    if not check_auth(request) and not check_api_key(request):
        return JSONResponse({"error": "Не авторизован"}, status_code=401)
    leads = db.list_leads(TENANT_ID, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата", "Имя", "Телефон", "Username", "Вопрос", "Статус"])
    for lead in leads:
        writer.writerow([
            (lead.get("created_at") or "")[:16],
            lead.get("name") or "",
            lead.get("phone") or "",
            lead.get("username") or "",
            lead.get("question") or "",
            lead.get("status") or "",
        ])
    output.seek(0)
    today_str = date.today().isoformat()
    filename = f"leads_{TENANT_ID}_{today_str}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/admin/leads/count")
async def leads_count(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    leads = db.list_leads(TENANT_ID)
    return {"count": len(leads), "newest_id": leads[0]["id"] if leads else 0}


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

    if not db.check_daily_limit(TENANT_ID, settings.DAILY_REQUEST_LIMIT):
        return JSONResponse({"error": "daily request limit reached"}, status_code=429)
    if not db.check_monthly_token_limit(TENANT_ID, settings.MONTHLY_TOKEN_LIMIT):
        return JSONResponse({"error": "monthly token limit reached"}, status_code=429)

    from agents.processor import MessageProcessor
    from services.llm_service import LLMService

    llm_svc = LLMService()
    api_processor = MessageProcessor(db, vector_store, llm_svc)

    result = await api_processor.process(
        user_id=0,
        user_name="клиент",
        username="api",
        text=question,
        platform="api",
    )

    return {
        "answer": result.answer,
        "confidence": result.confidence,
        "sources": result.sources.split(", ") if result.sources else [],
        "agent": result.agent_name,
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


# ============================================================
# Landing lead form
# ============================================================

@app.post("/api/landing-lead")
async def landing_lead(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    phone = body.get("phone", "").strip()
    business = body.get("business", "").strip()
    tariff = body.get("tariff", "").strip()
    comment = body.get("comment", "").strip()

    if not name or not phone:
        return JSONResponse({"error": "Заполните имя и телефон"}, status_code=400)

    tariff_names = {"start": "Старт (7 900)", "business": "Бизнес (14 900)", "premium": "Премиум (29 900)", "unknown": "Не определился"}
    tariff_display = tariff_names.get(tariff, tariff)

    # Send to owner via Telegram
    if settings.TELEGRAM_BOT_TOKEN and settings.OWNER_TELEGRAM_ID:
        import aiohttp
        text = (
            f"🔔 Новая заявка с сайта Mike AI\n\n"
            f"👤 Имя: {name}\n"
            f"📱 Контакт: {phone}\n"
            f"🏢 Бизнес: {business}\n"
            f"💰 Тариф: {tariff_display}\n"
        )
        if comment:
            text += f"💬 Комментарий: {comment}\n"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": settings.OWNER_TELEGRAM_ID, "text": text}
                )
        except Exception:
            pass

    # Save to database
    try:
        db.save_lead(TENANT_ID, 0, name, phone, f"Бизнес: {business}, Тариф: {tariff_display}. {comment}".strip())
    except Exception:
        pass

    return {"ok": True}


# ============================================================
# Widget endpoint (public, CORS-enabled, rate-limited by API key)
# ============================================================

@app.post("/widget/ask")
async def widget_ask(request: Request):
    body = await request.json()
    question = normalize_spaces(body.get("question", ""))
    api_key = body.get("api_key", "")

    if settings.API_KEY and api_key != settings.API_KEY:
        return JSONResponse({"error": "Invalid API key"}, status_code=401)

    if not question:
        return {"answer": "Напишите ваш вопрос."}

    if not db.check_daily_limit(TENANT_ID, settings.DAILY_REQUEST_LIMIT):
        return {"answer": "Дневной лимит обращений исчерпан. Попробуйте завтра."}
    if not db.check_monthly_token_limit(TENANT_ID, settings.MONTHLY_TOKEN_LIMIT):
        return {"answer": "Месячный лимит токенов исчерпан. Обратитесь к администратору."}

    from agents.processor import MessageProcessor
    from services.llm_service import LLMService

    llm_svc = LLMService()
    widget_processor = MessageProcessor(db, vector_store, llm_svc)

    result = await widget_processor.process(
        user_id=0,
        user_name="гость",
        username="widget",
        text=question,
        platform="widget",
    )

    resp = {"answer": result.answer}
    if result.confidence:
        resp["confidence"] = result.confidence
    if result.sources:
        resp["sources"] = result.sources
    if result.agent_name:
        resp["agent"] = result.agent_name
    return resp


@app.get("/widget/code", response_class=HTMLResponse)
async def widget_code_page(request: Request):
    """Show embeddable widget code for the client."""
    if not check_auth(request):
        return RedirectResponse("/login")
    host = request.base_url
    code = (
        f'<script src="{host}static/widget.js"\n'
        f'  data-api="{host}"\n'
        f'  data-key="{settings.API_KEY}"\n'
        f'  data-color="#2563eb"\n'
        f'  data-title="Онлайн-помощник {settings.COMPANY_NAME}"\n'
        f'  data-greeting="Привет! Чем могу помочь?">\n'
        f'</script>'
    )
    return f"""
    <html><head><title>Код виджета</title></head>
    <body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px">
    <h2>Код виджета для сайта</h2>
    <p>Вставьте этот код перед закрывающим тегом <code>&lt;/body&gt;</code> на сайте клиента:</p>
    <textarea style="width:100%;height:200px;font-family:monospace;font-size:13px;padding:12px;border:1px solid #ddd;border-radius:8px" readonly>{code}</textarea>
    <p><button onclick="navigator.clipboard.writeText(document.querySelector('textarea').value);this.textContent='Скопировано!'" style="padding:10px 20px;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer">Скопировать</button></p>
    <p><a href="/">Назад в админку</a></p>
    </body></html>
    """


# ==============================================================
#  LEAD HUNTER
# ==============================================================

@app.get("/admin/hunter", response_class=HTMLResponse)
async def hunter_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login")
    tasks = db.list_hunter_tasks(TENANT_ID)
    leads = db.list_hunter_leads(TENANT_ID, limit=50)
    stats = db.get_hunter_stats(TENANT_ID)
    return templates.TemplateResponse("hunter.html", {
        "request": request, "title": "Лид-хантер", "active_page": "hunter",
        "company_name": settings.COMPANY_NAME,
        "tasks": tasks, "leads": leads, "stats": stats,
        "error": "", "success": "",
    })


@app.post("/admin/hunter/add")
async def hunter_add_task(request: Request,
                          platform: str = Form(...),
                          target: str = Form(...),
                          keywords: str = Form(...),
                          daily_limit: int = Form(10),
                          city: str = Form("")):
    if not check_auth(request):
        return RedirectResponse("/login")
    db.create_hunter_task(TENANT_ID, platform, target.strip(), keywords.strip(), daily_limit=daily_limit, city=city.strip())
    tasks = db.list_hunter_tasks(TENANT_ID)
    leads = db.list_hunter_leads(TENANT_ID, limit=50)
    stats = db.get_hunter_stats(TENANT_ID)
    return templates.TemplateResponse("hunter.html", {
        "request": request, "title": "Лид-хантер", "active_page": "hunter",
        "company_name": settings.COMPANY_NAME,
        "tasks": tasks, "leads": leads, "stats": stats,
        "error": "", "success": f"Задача добавлена: {platform} / {target}",
    })


@app.post("/admin/hunter/task/{task_id}/toggle")
async def hunter_toggle_task(request: Request, task_id: int, action: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login")
    db.toggle_hunter_task(task_id, 1 if action == "resume" else 0)
    return RedirectResponse("/admin/hunter", status_code=302)


@app.post("/admin/hunter/task/{task_id}/delete")
async def hunter_delete_task(request: Request, task_id: int):
    if not check_auth(request):
        return RedirectResponse("/login")
    db.delete_hunter_task(task_id)
    return RedirectResponse("/admin/hunter", status_code=302)


@app.post("/admin/hunter/task/{task_id}/run")
async def hunter_run_task(request: Request, task_id: int):
    if not check_auth(request):
        return RedirectResponse("/login")
    task = db.get_hunter_task(task_id)
    if task:
        import asyncio
        from hunter.engine import run_hunter_task
        asyncio.create_task(run_hunter_task(db, task))
    return RedirectResponse("/admin/hunter", status_code=302)


@app.post("/admin/hunter/lead/{lead_id}/status")
async def hunter_update_lead(request: Request, lead_id: int, status: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login")
    db.update_hunter_lead_status(lead_id, status)
    return RedirectResponse("/admin/hunter", status_code=302)


# ==============================================================
#  OWNER / SUPERADMIN PANEL
# ==============================================================

@app.get("/owner/login", response_class=HTMLResponse)
async def owner_login_page(request: Request):
    if check_owner_auth(request):
        return RedirectResponse("/owner")
    return templates.TemplateResponse("owner_login.html", {"request": request, "error": ""})


@app.post("/owner/login")
async def owner_login(request: Request, password: str = Form(...)):
    if not settings.OWNER_PASSWORD:
        return templates.TemplateResponse("owner_login.html", {
            "request": request, "error": "OWNER_PASSWORD не настроен в .env"
        })
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(ip):
        return templates.TemplateResponse("owner_login.html", {"request": request, "error": "Слишком много попыток. Подождите 5 минут."})
    if py_secrets.compare_digest(password, settings.OWNER_PASSWORD):
        sid = py_secrets.token_hex(32)
        max_age = 60 * 60 * 24
        _add_session(sid, "owner", max_age)
        resp = RedirectResponse("/owner", status_code=302)
        resp.set_cookie("owner_session", sid, httponly=True, samesite="lax", secure=True, max_age=max_age)
        return resp
    _record_login_attempt(ip)
    return templates.TemplateResponse("owner_login.html", {"request": request, "error": "Неверный пароль"})


@app.get("/owner/logout")
async def owner_logout(request: Request):
    sid = request.cookies.get("owner_session")
    _remove_session(sid)
    resp = RedirectResponse("/owner/login", status_code=302)
    resp.delete_cookie("owner_session")
    return resp


def _enrich_clients(clients):
    """Add plan_name to each client dict."""
    plans = {p["id"]: p for p in db.list_plans()}
    for c in clients:
        p = plans.get(c.get("plan_id"))
        c["plan_name"] = p["name"] if p else "—"
    return clients


@app.get("/owner", response_class=HTMLResponse)
@app.get("/owner/dashboard", response_class=HTMLResponse)
async def owner_dashboard(request: Request):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    summary = db.get_owner_summary()
    clients = _enrich_clients(db.get_all_tenants_stats())
    return templates.TemplateResponse("owner_dashboard.html", {
        "request": request,
        "title": "Дашборд",
        "active_page": "owner_dashboard",
        "summary": summary,
        "clients": clients,
    })


@app.get("/owner/clients", response_class=HTMLResponse)
async def owner_clients(request: Request):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    clients = _enrich_clients(db.get_all_tenants_stats())
    return templates.TemplateResponse("owner_clients.html", {
        "request": request,
        "title": "Клиенты",
        "active_page": "owner_clients",
        "clients": clients,
    })


@app.get("/owner/clients/add", response_class=HTMLResponse)
async def owner_add_client_page(request: Request):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    return templates.TemplateResponse("owner_add_client.html", {
        "request": request,
        "title": "Добавить клиента",
        "active_page": "owner_add",
        "plans": db.list_plans(),
        "error": "",
        "success": "",
    })


@app.post("/owner/clients/add")
async def owner_add_client(request: Request,
                           tenant_id: str = Form(...),
                           company_name: str = Form(...),
                           bot_token: str = Form(""),
                           owner_telegram_id: int = Form(0),
                           plan_id: int = Form(1)):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    existing = db.get_tenant(tenant_id)
    if existing:
        return templates.TemplateResponse("owner_add_client.html", {
            "request": request, "title": "Добавить клиента", "active_page": "owner_add",
            "plans": db.list_plans(), "error": f"Клиент '{tenant_id}' уже существует", "success": "",
        })
    plan = db.get_plan(plan_id)
    db.upsert_tenant(
        tenant_id=tenant_id,
        company_name=company_name,
        bot_token=bot_token,
        owner_telegram_id=owner_telegram_id,
        is_active=1,
        plan_id=plan_id,
    )
    # Auto-create client directory and .env
    base_dir = Path("/opt/mikeai")
    client_dir = base_dir / tenant_id
    main_dir = base_dir / "MikeAI"
    extra_info = ""
    try:
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / "data" / f"uploads/{tenant_id}").mkdir(parents=True, exist_ok=True)
        (client_dir / "data" / "chroma_db").mkdir(parents=True, exist_ok=True)
        web_pass = py_secrets.token_hex(8)
        api_key = f"mikeai_{tenant_id}_{py_secrets.token_hex(6)}"
        limits = {"max_req": 200, "max_tok": 200000, "max_files": 10}
        if plan and plan.get("name") == "Pro":
            limits = {"max_req": 500, "max_tok": 500000, "max_files": 20}
        elif plan and plan.get("name") == "Business":
            limits = {"max_req": 2000, "max_tok": 2000000, "max_files": 50}
        env_content = (
            f"TENANT_ID={tenant_id}\n"
            f"COMPANY_NAME={company_name}\n"
            f"BOT_NAME=Ассистент {company_name}\n"
            f"TELEGRAM_BOT_TOKEN={bot_token or 'REPLACE_ME'}\n"
            f"OWNER_TELEGRAM_ID={owner_telegram_id}\n"
            f"WEB_PASSWORD={web_pass}\n"
            f"API_KEY={api_key}\n"
            f"DAILY_REQUEST_LIMIT={limits['max_req']}\n"
            f"MONTHLY_TOKEN_LIMIT={limits['max_tok']}\n"
            f"MAX_FILES_PER_TENANT={limits['max_files']}\n"
            f"BOT_ROLE=hybrid\nBOT_TONE=friendly\n"
            f"OPENROUTER_API_KEY={settings.OPENROUTER_API_KEY}\n"
            f"OPENROUTER_MODEL=deepseek/deepseek-chat\n"
            f"CHROMA_PERSIST_DIR={client_dir}/data/chroma_db\n"
            f"EMBEDDING_MODEL=intfloat/multilingual-e5-small\n"
            f"VK_ENABLED=false\nVK_GROUP_TOKEN=\nVK_GROUP_ID=0\n"
            f"WIDGET_ENABLED=true\n"
        )
        (client_dir / ".env").write_text(env_content)
        for item in ["bot", "config", "core", "hunter", "rag", "services", "web_ui", "run.py", "requirements.txt"]:
            src = main_dir / item
            dst = client_dir / item
            if src.exists() and not dst.exists():
                dst.symlink_to(src)
        extra_info = f" Папка: {client_dir} | Пароль админки: {web_pass} | API: {api_key}"
    except Exception as e:
        extra_info = f" (Папка не создана: {e})"
    return templates.TemplateResponse("owner_add_client.html", {
        "request": request, "title": "Добавить клиента", "active_page": "owner_add",
        "plans": db.list_plans(), "error": "",
        "success": f"Клиент '{company_name}' ({tenant_id}) создан! Тариф: {plan['name'] if plan else 'Start'}.{extra_info}",
    })


@app.get("/owner/clients/{tenant_id}", response_class=HTMLResponse)
async def owner_client_detail(request: Request, tenant_id: str):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    client = db.get_tenant(tenant_id)
    if not client:
        return RedirectResponse("/owner/clients")
    plan = db.get_plan(client.get("plan_id", 1))
    stats_list = db.get_all_tenants_stats()
    stats = next((s for s in stats_list if s["id"] == tenant_id), {
        "users": 0, "messages": 0, "leads": 0, "today_msgs": 0, "month_tokens": 0,
    })
    conversations = db.list_conversations(tenant_id, limit=20)
    leads = []
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 20",
                (tenant_id,),
            ).fetchall()
            leads = [dict(r) for r in rows]
    except Exception:
        pass
    return templates.TemplateResponse("owner_client_detail.html", {
        "request": request,
        "title": client.get("company_name", tenant_id),
        "active_page": "owner_clients",
        "client": client,
        "plan": plan,
        "stats": stats,
        "conversations": conversations,
        "leads": leads,
    })


@app.post("/owner/clients/{tenant_id}/toggle")
async def owner_toggle_client(request: Request, tenant_id: str, action: str = Form(...)):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    db.toggle_tenant(tenant_id, 1 if action == "enable" else 0)
    referer = request.headers.get("referer", "/owner/clients")
    return RedirectResponse(referer, status_code=302)


@app.get("/owner/plans", response_class=HTMLResponse)
async def owner_plans(request: Request):
    if not check_owner_auth(request):
        return RedirectResponse("/owner/login")
    return templates.TemplateResponse("owner_plans.html", {
        "request": request,
        "title": "Тарифы",
        "active_page": "owner_plans",
        "plans": db.list_plans(),
    })
