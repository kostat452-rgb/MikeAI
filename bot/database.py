import sqlite3
from datetime import date
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
                tenant_id TEXT DEFAULT 'default',
                user_id INTEGER, username TEXT, first_name TEXT,
                phone TEXT, question TEXT, status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                conversation_id INTEGER,
                user_id INTEGER,
                rating INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS missing_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                user_id INTEGER,
                question TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS answer_cache (
                cache_key TEXT PRIMARY KEY,
                tenant_id TEXT DEFAULT 'default',
                question TEXT,
                answer TEXT,
                sources TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            # migrations for old DB
            for table in ["conversations", "users", "usage_stats"]:
                try:
                    self._ensure_column(conn, table, "tenant_id", "TEXT DEFAULT 'default'")
                except Exception:
                    pass
            for col, definition in [("intent", "TEXT DEFAULT 'QUESTION'"), ("confidence", "REAL DEFAULT 0")]:
                try:
                    self._ensure_column(conn, "conversations", col, definition)
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_tenant_user ON conversations(tenant_id, user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missing_tenant ON missing_questions(tenant_id, created_at)")

    def save_conversation(self, user_id, username, first_name, question, answer, sources="", tokens=0, tenant_id="default", intent="QUESTION", confidence=0):
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

    def is_new_user(self, user_id, tenant_id="default"):
        with self._connect() as conn:
            r = conn.execute("SELECT message_count FROM users WHERE tenant_id = ? AND user_id = ?", (tenant_id, user_id)).fetchone()
            return r is None or r[0] == 0

    def save_lead(self, tenant_id, user_id, username, first_name, question, phone=""):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO leads (tenant_id, user_id, username, first_name, phone, question) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, user_id, username, first_name, phone, question),
            )

    def save_missing_question(self, tenant_id, user_id, question, reason="low_confidence"):
        with self._connect() as conn:
            conn.execute("INSERT INTO missing_questions (tenant_id, user_id, question, reason) VALUES (?, ?, ?, ?)", (tenant_id, user_id, question, reason))

    def get_cached_answer(self, tenant_id, question):
        key = make_cache_key(tenant_id, question)
        with self._connect() as conn:
            r = conn.execute("SELECT answer, sources FROM answer_cache WHERE cache_key = ?", (key,)).fetchone()
            return dict(r) if r else None

    def set_cached_answer(self, tenant_id, question, answer, sources=""):
        key = make_cache_key(tenant_id, question)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO answer_cache (cache_key, tenant_id, question, answer, sources) VALUES (?, ?, ?, ?, ?)",
                (key, tenant_id, question, answer, sources),
            )

    def add_feedback(self, tenant_id, conversation_id, user_id, rating):
        with self._connect() as conn:
            conn.execute("INSERT INTO feedback (tenant_id, conversation_id, user_id, rating) VALUES (?, ?, ?, ?)", (tenant_id, conversation_id, user_id, rating))

    def get_dashboard_stats(self, tenant_id="default"):
        with self._connect() as conn:
            leads = conn.execute("SELECT COUNT(*) FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM missing_questions WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
            return {
                "users": self.get_user_count(tenant_id),
                "messages": self.get_message_count(tenant_id),
                "leads": leads,
                "missing_questions": missing,
            }
