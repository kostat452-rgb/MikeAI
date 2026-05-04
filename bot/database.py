import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from core.guards import make_cache_key


class Database:
    def __init__(self):
        self.db_path = Path("./data/conversations.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_column(self, conn, table: str, column: str, definition: str):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def init_db(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                company_name TEXT,
                bot_token TEXT,
                owner_telegram_id INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                plan_id INTEGER DEFAULT 1,
                daily_request_limit INTEGER DEFAULT 500,
                monthly_token_limit INTEGER DEFAULT 500000,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                user_id INTEGER, username TEXT, first_name TEXT,
                question TEXT, answer TEXT, sources TEXT,
                intent TEXT DEFAULT 'QUESTION', confidence REAL DEFAULT 0,
                tokens_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                user_id INTEGER, username TEXT,
                first_name TEXT, last_name TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_count INTEGER DEFAULT 0,
                UNIQUE(tenant_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS usage_stats (
                tenant_id TEXT DEFAULT 'default',
                date TEXT,
                total_requests INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                PRIMARY KEY(tenant_id, date)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT,
                username TEXT,
                name TEXT,
                phone TEXT,
                question TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                conversation_id INTEGER,
                user_id TEXT,
                question TEXT,
                answer TEXT,
                rating INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS missing_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT,
                question TEXT NOT NULL,
                reason TEXT,
                count INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS answer_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                question_hash TEXT NOT NULL,
                question TEXT,
                answer TEXT,
                sources TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                UNIQUE(tenant_id, question_hash)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS uploaded_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                filename TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                chunks_count INTEGER DEFAULT 0,
                uploaded_by TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            # ---- Lead Hunter tables ----
            conn.execute("""CREATE TABLE IF NOT EXISTS hunter_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL,
                target TEXT NOT NULL,
                keywords TEXT NOT NULL,
                message_template TEXT DEFAULT '',
                daily_limit INTEGER DEFAULT 10,
                is_active INTEGER DEFAULT 1,
                last_scan TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS hunter_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                task_id INTEGER,
                platform TEXT NOT NULL,
                source_url TEXT DEFAULT '',
                author_id TEXT DEFAULT '',
                author_name TEXT DEFAULT '',
                text TEXT DEFAULT '',
                relevance_score REAL DEFAULT 0,
                status TEXT DEFAULT 'new',
                contacted INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES hunter_tasks(id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hunter_leads_tenant ON hunter_leads(tenant_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hunter_tasks_tenant ON hunter_tasks(tenant_id)")
            try:
                self._ensure_column(conn, "hunter_tasks", "city", "TEXT DEFAULT ''")
            except Exception:
                pass

            conn.execute("""CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                max_requests_per_day INTEGER,
                max_tokens_per_month INTEGER,
                max_files INTEGER,
                max_bots INTEGER,
                price_rub INTEGER
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS tenant_settings (
                tenant_id TEXT PRIMARY KEY,
                plan_id INTEGER DEFAULT 1,
                custom_prompt TEXT,
                welcome_message TEXT,
                bot_tone TEXT DEFAULT 'professional',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")

            # Seed default plans
            existing = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            if existing == 0:
                conn.execute(
                    "INSERT INTO plans (name, max_requests_per_day, max_tokens_per_month, max_files, max_bots, price_rub) VALUES (?, ?, ?, ?, ?, ?)",
                    ("Start", 200, 200000, 10, 1, 3900),
                )
                conn.execute(
                    "INSERT INTO plans (name, max_requests_per_day, max_tokens_per_month, max_files, max_bots, price_rub) VALUES (?, ?, ?, ?, ?, ?)",
                    ("Pro", 500, 500000, 20, 3, 9900),
                )
                conn.execute(
                    "INSERT INTO plans (name, max_requests_per_day, max_tokens_per_month, max_files, max_bots, price_rub) VALUES (?, ?, ?, ?, ?, ?)",
                    ("Business", 2000, 2000000, 50, 10, 19900),
                )

            # Auto-register current tenant if not exists
            try:
                from config.settings import settings as _s
                if _s.TENANT_ID and _s.TENANT_ID != "default":
                    exists = conn.execute("SELECT 1 FROM tenants WHERE id = ?", (_s.TENANT_ID,)).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO tenants (id, company_name, owner_telegram_id, is_active, plan_id) VALUES (?, ?, ?, 1, 2)",
                            (_s.TENANT_ID, _s.COMPANY_NAME, _s.OWNER_TELEGRAM_ID),
                        )
            except Exception:
                pass

            # Migrations for old DBs
            for table in ["conversations", "users", "usage_stats"]:
                try:
                    self._ensure_column(conn, table, "tenant_id", "TEXT DEFAULT 'default'")
                except Exception:
                    pass
            for col, defn in [
                ("intent", "TEXT DEFAULT 'QUESTION'"),
                ("confidence", "REAL DEFAULT 0"),
            ]:
                try:
                    self._ensure_column(conn, "conversations", col, defn)
                except Exception:
                    pass
            try:
                self._ensure_column(conn, "missing_questions", "count", "INTEGER DEFAULT 1")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "missing_questions", "updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "feedback", "question", "TEXT")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "feedback", "answer", "TEXT")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "answer_cache", "expires_at", "TEXT")
            except Exception:
                pass

            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_tenant_user ON conversations(tenant_id, user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missing_tenant ON missing_questions(tenant_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_tenant ON answer_cache(tenant_id, question_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_tenant ON uploaded_documents(tenant_id, created_at)")

            # Agent system: source & temperature for leads
            for col, defn in [
                ("source", "TEXT DEFAULT 'manual'"),
                ("temperature", "TEXT DEFAULT 'unknown'"),
            ]:
                try:
                    self._ensure_column(conn, "leads", col, defn)
                except Exception:
                    pass

    # ---- Conversations ----

    def save_conversation(
        self, user_id, username, first_name, question, answer,
        sources="", tokens=0, tenant_id="default", intent="QUESTION", confidence=0,
    ):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (tenant_id, user_id, username, first_name, question, answer, sources, tokens_used, intent, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, user_id, username, first_name, question, answer, sources, tokens, intent, confidence),
            )
            today = date.today().isoformat()
            conn.execute(
                "INSERT INTO usage_stats (tenant_id, date, total_requests, total_tokens) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(tenant_id, date) DO UPDATE SET total_requests = total_requests + 1, total_tokens = total_tokens + ?",
                (tenant_id, today, tokens, tokens),
            )
            return cur.lastrowid

    # ---- Users ----

    def upsert_user(self, user_id, username, first_name, last_name="", tenant_id="default"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (tenant_id, user_id, username, first_name, last_name, message_count) VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(tenant_id, user_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name, last_name = excluded.last_name, last_seen = CURRENT_TIMESTAMP, message_count = message_count + 1",
                (tenant_id, user_id, username, first_name, last_name),
            )

    def get_user_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]

    def get_message_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM conversations WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]

    def is_new_user(self, user_id, tenant_id="default"):
        with self._connect() as conn:
            r = conn.execute("SELECT message_count FROM users WHERE tenant_id = ? AND user_id = ?", (tenant_id, user_id)).fetchone()
            return r is None or r[0] == 0

    # ---- Stats ----

    def get_today_stats(self, tenant_id="default"):
        today = date.today().isoformat()
        with self._connect() as conn:
            r = conn.execute("SELECT total_requests, total_tokens FROM usage_stats WHERE tenant_id = ? AND date = ?", (tenant_id, today)).fetchone()
            return {"requests": r[0], "tokens": r[1]} if r else {"requests": 0, "tokens": 0}

    def get_month_stats(self, tenant_id="default"):
        month = date.today().strftime("%Y-%m")
        with self._connect() as conn:
            r = conn.execute("SELECT SUM(total_requests), SUM(total_tokens) FROM usage_stats WHERE tenant_id = ? AND date LIKE ?", (tenant_id, f"{month}%")).fetchone()
            return {"requests": r[0] or 0, "tokens": r[1] or 0}

    # ---- Leads ----

    def create_lead(self, tenant_id, user_id, username, name, phone="", question="",
                     source="manual", temperature="unknown"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO leads (tenant_id, user_id, username, name, phone, question, source, temperature) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, str(user_id), username, name, phone, question, source, temperature),
            )

    def save_lead(self, tenant_id, user_id, username, first_name, question, phone="",
                  source="manual", temperature="unknown"):
        self.create_lead(tenant_id, user_id, username, first_name, phone, question, source, temperature)

    def list_leads(self, tenant_id="default", limit=100, offset=0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_leads_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]

    def get_leads_today_count(self, tenant_id="default"):
        today = date.today().isoformat()
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM leads WHERE tenant_id = ? AND created_at >= ?",
                (tenant_id, today),
            ).fetchone()[0]

    def update_lead_status(self, lead_id: int, status: str, tenant_id: str = "default"):
        with self._connect() as conn:
            conn.execute(
                "UPDATE leads SET status = ? WHERE id = ? AND tenant_id = ?",
                (status, lead_id, tenant_id),
            )

    # ---- Missing Questions ----

    def save_missing_question(self, tenant_id, user_id, question, reason="low_confidence"):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, count FROM missing_questions WHERE tenant_id = ? AND question = ?",
                (tenant_id, question),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE missing_questions SET count = count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (existing[0],),
                )
            else:
                conn.execute(
                    "INSERT INTO missing_questions (tenant_id, user_id, question, reason) VALUES (?, ?, ?, ?)",
                    (tenant_id, str(user_id), question, reason),
                )

    def list_missing_questions(self, tenant_id="default", limit=100, offset=0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM missing_questions WHERE tenant_id = ? ORDER BY count DESC, updated_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_missing_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM missing_questions WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]

    # ---- Feedback ----

    def add_feedback(self, tenant_id, conversation_id, user_id, rating, question="", answer=""):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO feedback (tenant_id, conversation_id, user_id, rating, question, answer) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, conversation_id, str(user_id), rating, question, answer),
            )

    def list_feedback(self, tenant_id="default", limit=100, offset=0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_negative_feedback(self, tenant_id="default", limit=50):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.*, c.question, c.answer FROM feedback f LEFT JOIN conversations c ON f.conversation_id = c.id WHERE f.tenant_id = ? AND f.rating < 0 ORDER BY f.created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_feedback_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM feedback WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]

    def get_negative_feedback_count(self, tenant_id="default"):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM feedback WHERE tenant_id = ? AND rating < 0", (tenant_id,)).fetchone()[0]

    # ---- Cache ----

    def get_cached_answer(self, tenant_id, question, ttl_hours=24):
        key = make_cache_key(tenant_id, question)
        with self._connect() as conn:
            r = conn.execute(
                "SELECT answer, sources, created_at, expires_at FROM answer_cache WHERE tenant_id = ? AND question_hash = ?",
                (tenant_id, key),
            ).fetchone()
            if not r:
                return None
            expires = r["expires_at"]
            if expires:
                try:
                    if datetime.fromisoformat(expires) < datetime.now():
                        conn.execute("DELETE FROM answer_cache WHERE tenant_id = ? AND question_hash = ?", (tenant_id, key))
                        return None
                except Exception:
                    pass
            return {"answer": r["answer"], "sources": r["sources"]}

    def set_cached_answer(self, tenant_id, question, answer, sources="", ttl_hours=24):
        key = make_cache_key(tenant_id, question)
        expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO answer_cache (tenant_id, question_hash, question, answer, sources, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, key, question, answer, sources, expires),
            )

    def clear_expired_cache(self, tenant_id="default"):
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM answer_cache WHERE tenant_id = ? AND expires_at IS NOT NULL AND expires_at < ?",
                (tenant_id, now),
            )

    def clear_all_cache(self, tenant_id="default"):
        with self._connect() as conn:
            conn.execute("DELETE FROM answer_cache WHERE tenant_id = ?", (tenant_id,))

    # ---- Analytics ----

    def get_dashboard_stats(self, tenant_id="default"):
        with self._connect() as conn:
            leads = conn.execute("SELECT COUNT(*) FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM missing_questions WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            neg_fb = conn.execute("SELECT COUNT(*) FROM feedback WHERE tenant_id = ? AND rating < 0", (tenant_id,)).fetchone()[0]
            return {
                "users": self.get_user_count(tenant_id),
                "messages": self.get_message_count(tenant_id),
                "leads": leads,
                "leads_today": self.get_leads_today_count(tenant_id),
                "missing_questions": missing,
                "negative_feedback": neg_fb,
            }

    def get_top_questions(self, tenant_id="default", limit=20):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT question, COUNT(*) as cnt FROM conversations WHERE tenant_id = ? GROUP BY question ORDER BY cnt DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_active_users(self, tenant_id="default", days=7):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users WHERE tenant_id = ? AND last_seen >= ?",
                (tenant_id, cutoff),
            ).fetchone()[0]

    def get_lead_conversion_rate(self, tenant_id="default"):
        with self._connect() as conn:
            users = self.get_user_count(tenant_id)
            leads = conn.execute("SELECT COUNT(DISTINCT user_id) FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            return round(leads / max(users, 1) * 100, 1)

    # ---- Rate / Quota checks ----

    def check_daily_limit(self, tenant_id: str, limit: int) -> bool:
        """Return True if tenant is under daily request limit."""
        today = self.get_today_stats(tenant_id)
        return today["requests"] < limit

    def check_monthly_token_limit(self, tenant_id: str, limit: int) -> bool:
        """Return True if tenant is under monthly token limit."""
        month = self.get_month_stats(tenant_id)
        return month["tokens"] < limit

    def get_usage_vs_limits(self, tenant_id: str, daily_limit: int, monthly_token_limit: int) -> dict:
        today = self.get_today_stats(tenant_id)
        month = self.get_month_stats(tenant_id)
        return {
            "today_requests": today["requests"],
            "daily_limit": daily_limit,
            "daily_pct": min(round(today["requests"] / max(daily_limit, 1) * 100, 1), 100),
            "month_tokens": month["tokens"],
            "monthly_limit": monthly_token_limit,
            "monthly_pct": min(round(month["tokens"] / max(monthly_token_limit, 1) * 100, 1), 100),
        }

    # ---- Plans ----

    def list_plans(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM plans ORDER BY price_rub").fetchall()
            return [dict(r) for r in rows]

    def get_plan(self, plan_id: int):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            return dict(r) if r else None

    # ---- Conversations list for admin ----

    def list_conversations(self, tenant_id="default", limit=50, offset=0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- Users list for admin ----

    def list_users(self, tenant_id="default", limit=100, offset=0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- Uploaded Documents ----

    def save_uploaded_document(self, tenant_id, filename, file_size=0, chunks_count=0, uploaded_by="admin"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO uploaded_documents (tenant_id, filename, file_size, chunks_count, uploaded_by) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, filename, file_size, chunks_count, uploaded_by),
            )

    def list_uploaded_documents(self, tenant_id="default"):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM uploaded_documents WHERE tenant_id = ? ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_uploaded_document(self, tenant_id, filename):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM uploaded_documents WHERE tenant_id = ? AND filename = ?",
                (tenant_id, filename),
            )

    # ---- Conversation by id (for feedback enrichment) ----

    def get_conversation(self, conv_id: int, tenant_id: str = "default"):
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM conversations WHERE id = ? AND tenant_id = ?",
                (conv_id, tenant_id),
            ).fetchone()
            return dict(r) if r else None

    # ---- Owner / Superadmin ----

    def list_tenants(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_tenant(self, tenant_id: str):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            return dict(r) if r else None

    def upsert_tenant(self, tenant_id: str, company_name: str = "", bot_token: str = "",
                      owner_telegram_id: int = 0, is_active: int = 1, plan_id: int = 1):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO tenants (id, company_name, bot_token, owner_telegram_id, is_active, plan_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    company_name=excluded.company_name,
                    bot_token=excluded.bot_token,
                    owner_telegram_id=excluded.owner_telegram_id,
                    is_active=excluded.is_active,
                    plan_id=excluded.plan_id
            """, (tenant_id, company_name, bot_token, owner_telegram_id, is_active, plan_id))

    def toggle_tenant(self, tenant_id: str, is_active: int):
        with self._connect() as conn:
            conn.execute("UPDATE tenants SET is_active = ? WHERE id = ?", (is_active, tenant_id))

    def get_all_tenants_stats(self):
        """Get aggregated stats for all tenants (for owner dashboard)."""
        with self._connect() as conn:
            tenants = conn.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
            result = []
            for t in tenants:
                tid = t["id"]
                users = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tid,)).fetchone()[0]
                messages = conn.execute("SELECT COUNT(*) FROM conversations WHERE tenant_id = ?", (tid,)).fetchone()[0]
                leads = conn.execute("SELECT COUNT(*) FROM leads WHERE tenant_id = ?", (tid,)).fetchone()[0]
                today_msgs = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE tenant_id = ? AND date(created_at) = date('now')",
                    (tid,),
                ).fetchone()[0]
                month_tokens = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_stats WHERE tenant_id = ? AND date >= ?",
                    (tid, date.today().replace(day=1).isoformat()),
                ).fetchone()[0]
                result.append({
                    **dict(t),
                    "users": users,
                    "messages": messages,
                    "leads": leads,
                    "today_msgs": today_msgs,
                    "month_tokens": month_tokens,
                })
            return result

    def get_owner_summary(self):
        """Global summary for owner dashboard."""
        with self._connect() as conn:
            total_tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            active_tenants = conn.execute("SELECT COUNT(*) FROM tenants WHERE is_active = 1").fetchone()[0]
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_messages = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            today_messages = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE date(created_at) = date('now')"
            ).fetchone()[0]
            return {
                "total_tenants": total_tenants,
                "active_tenants": active_tenants,
                "total_users": total_users,
                "total_messages": total_messages,
                "total_leads": total_leads,
                "today_messages": today_messages,
            }

    # ---- Lead Hunter ----

    def create_hunter_task(self, tenant_id, platform, target, keywords,
                           message_template="", daily_limit=10, city=""):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO hunter_tasks (tenant_id, platform, target, keywords, message_template, daily_limit, city) VALUES (?,?,?,?,?,?,?)",
                (tenant_id, platform, target, keywords, message_template, daily_limit, city),
            )

    def list_hunter_tasks(self, tenant_id="default"):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hunter_tasks WHERE tenant_id = ? ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_hunter_task(self, task_id: int):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM hunter_tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(r) if r else None

    def toggle_hunter_task(self, task_id: int, is_active: int):
        with self._connect() as conn:
            conn.execute("UPDATE hunter_tasks SET is_active = ? WHERE id = ?", (is_active, task_id))

    def delete_hunter_task(self, task_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM hunter_leads WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM hunter_tasks WHERE id = ?", (task_id,))

    def update_hunter_task_scan(self, task_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE hunter_tasks SET last_scan = ? WHERE id = ?",
                (datetime.now().isoformat(), task_id),
            )

    def save_hunter_lead(self, tenant_id, task_id, platform, source_url="",
                         author_id="", author_name="", text="", relevance_score=0):
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM hunter_leads WHERE tenant_id = ? AND platform = ? AND author_id = ? AND task_id = ?",
                (tenant_id, platform, author_id, task_id),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO hunter_leads (tenant_id, task_id, platform, source_url, author_id, author_name, text, relevance_score) VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, task_id, platform, source_url, author_id, author_name, text, relevance_score),
            )
            return True

    def list_hunter_leads(self, tenant_id="default", limit=50, status=None):
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT hl.*, ht.target as task_target FROM hunter_leads hl LEFT JOIN hunter_tasks ht ON hl.task_id=ht.id WHERE hl.tenant_id = ? AND hl.status = ? ORDER BY hl.created_at DESC LIMIT ?",
                    (tenant_id, status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT hl.*, ht.target as task_target FROM hunter_leads hl LEFT JOIN hunter_tasks ht ON hl.task_id=ht.id WHERE hl.tenant_id = ? ORDER BY hl.created_at DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def update_hunter_lead_status(self, lead_id: int, status: str):
        with self._connect() as conn:
            conn.execute("UPDATE hunter_leads SET status = ? WHERE id = ?", (status, lead_id))

    def get_hunter_stats(self, tenant_id="default"):
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM hunter_leads WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            new = conn.execute("SELECT COUNT(*) FROM hunter_leads WHERE tenant_id = ? AND status = 'new'", (tenant_id,)).fetchone()[0]
            contacted = conn.execute("SELECT COUNT(*) FROM hunter_leads WHERE tenant_id = ? AND status = 'contacted'", (tenant_id,)).fetchone()[0]
            tasks = conn.execute("SELECT COUNT(*) FROM hunter_tasks WHERE tenant_id = ? AND is_active = 1", (tenant_id,)).fetchone()[0]
            return {"total": total, "new": new, "contacted": contacted, "active_tasks": tasks}
