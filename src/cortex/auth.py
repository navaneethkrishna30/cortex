"""The auth store: UI login, TOTP, tokens, the device-code login flow, the audit log.

auth.sqlite lives outside the vault so secrets never enter git.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
import time
from fnmatch import fnmatchcase

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I
REQUEST_TTL = 300
AUTH_CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
SESSION_TTL = 30 * 86400
UNUSED_CLIENT_TTL = 86400
MAX_PENDING_PER_IP = 3
MAX_PENDING_TOTAL = 20
MAX_UNAPPROVED_CLIENTS = 100
MAX_FAILURES = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS failures(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, scope TEXT NOT NULL,
    kind TEXT NOT NULL, family TEXT, expires REAL, created REAL NOT NULL, revoked INTEGER DEFAULT 0, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS clients(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL, created REAL NOT NULL, approved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_requests(
    id INTEGER PRIMARY KEY, secret_hash TEXT UNIQUE NOT NULL, code TEXT NOT NULL, client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, scope TEXT NOT NULL, state TEXT, ip TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, auth_code_hash TEXT, auth_expires REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL NOT NULL, event TEXT NOT NULL, detail TEXT, ip TEXT);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthError(Exception):
    pass


class AuthStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.hasher = PasswordHasher()
        path.chmod(0o600)

    # Small helpers

    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))

    def log(self, event: str, detail: str = "", ip: str = "") -> None:
        with self.lock:
            self.db.execute("INSERT INTO audit(at, event, detail, ip) VALUES(?,?,?,?)", (time.time(), event, detail, ip))
            self.db.commit()

    def audit(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # First-run setup

    @property
    def is_set_up(self) -> bool:
        return self._get("password_hash") is not None and self._get("totp_secret") is not None

    def new_setup_token(self) -> str:
        token = secrets.token_urlsafe(24)
        with self.lock:
            self._set("setup_token_hash", digest(token))
            self.db.commit()
        return token

    def check_setup_token(self, token: str) -> bool:
        stored = self._get("setup_token_hash")
        return bool(stored) and secrets.compare_digest(stored, digest(token))

    def hash_password(self, password: str) -> str:
        return self.hasher.hash(password)

    def finish_setup(self, password_hash: str, totp_secret: str) -> None:
        with self.lock:
            self._set("password_hash", password_hash)
            self._set("totp_secret", totp_secret)
            self.db.execute("DELETE FROM settings WHERE key='setup_token_hash'")
            self.db.commit()
        self.log("setup_complete")

    def reset(self) -> None:
        """Recovery from the host: the next UI visit runs setup again. Tokens stay valid."""
        with self.lock:
            self.db.execute("DELETE FROM settings")
            self.db.execute("DELETE FROM sessions")
            self.db.execute("DELETE FROM failures")
            self.db.commit()
        self.log("auth_reset")

    # Lockout: exponential backoff after five failures

    def locked_for(self, key: str) -> float:
        row = self.db.execute("SELECT until FROM failures WHERE key=?", (key,)).fetchone()
        return max(0.0, row["until"] - time.time()) if row else 0.0

    def _fail(self, key: str) -> None:
        with self.lock:
            row = self.db.execute("SELECT count FROM failures WHERE key=?", (key,)).fetchone()
            count = (row["count"] if row else 0) + 1
            until = time.time() + 2 ** (count - MAX_FAILURES) * 30 if count >= MAX_FAILURES else 0
            self.db.execute("INSERT OR REPLACE INTO failures VALUES(?,?,?)", (key, count, until))
            self.db.commit()

    def _succeed(self, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM failures WHERE key=?", (key,))
            self.db.commit()

    # Password and TOTP

    def check_password(self, password: str, ip: str = "") -> bool:
        if self.locked_for("password"):
            return False
        try:
            self.hasher.verify(self._get("password_hash") or "", password)
        except (VerificationError, Exception):
            self._fail("password")
            self.log("login_failed", "password", ip)
            return False
        self._succeed("password")
        return True

    def check_totp(self, code: str, ip: str = "") -> bool:
        """Accept each time step once: a code seen on screen can't be replayed."""
        with self.lock:
            if self.locked_for("totp"):
                return False
            secret = self._get("totp_secret")
            totp = pyotp.TOTP(secret) if secret else None
            last = int(self._get("totp_last_step") or 0)
            now = int(time.time()) // 30
            for step in (now - 1, now, now + 1):
                if totp and step > last and secrets.compare_digest(totp.at(step * 30), code.strip()):
                    self._set("totp_last_step", str(step))
                    self.db.commit()
                    self._succeed("totp")
                    return True
            self._fail("totp")
            self.log("totp_failed", "", ip)
            return False

    def change_totp(self, secret: str) -> None:
        with self.lock:
            self._set("totp_secret", secret)
            self.db.execute("DELETE FROM settings WHERE key='totp_last_step'")
            self.db.commit()
        self.log("totp_reenrolled")

    # UI sessions

    def new_session(self) -> str:
        session = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("INSERT INTO sessions VALUES(?,?)", (digest(session), time.time() + SESSION_TTL))
            self.db.commit()
        return session

    def check_session(self, session: str | None) -> bool:
        if not session:
            return False
        row = self.db.execute("SELECT expires FROM sessions WHERE hash=?", (digest(session),)).fetchone()
        return bool(row) and row["expires"] > time.time()

    def end_session(self, session: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE hash=?", (digest(session),))
            self.db.commit()

    # Tokens

    def _new_token(self, name: str, scope: str, kind: str, family: str | None = None, ttl: float | None = None) -> str:
        token = secrets.token_urlsafe(32)  # 256 bits
        expires = time.time() + ttl if ttl else None
        with self.lock:
            self.db.execute(
                "INSERT INTO tokens(name, hash, scope, kind, family, expires, created) VALUES(?,?,?,?,?,?,?)",
                (name, digest(token), scope, kind, family, expires, time.time()),
            )
            self.db.commit()
        return token

    def create_static_token(self, name: str, scope: str) -> str:
        if scope not in ("read", "write"):
            raise AuthError("scope must be read or write")
        token = self._new_token(name, scope, "static")
        self.log("token_created", f"{name} ({scope})")
        return token

    def check_bearer(self, token: str) -> sqlite3.Row | None:
        row = self.db.execute(
            "SELECT * FROM tokens WHERE hash=? AND kind IN ('static','access') AND revoked=0", (digest(token),)
        ).fetchone()
        if row and (row["expires"] is None or row["expires"] > time.time()):
            return row
        return None

    def list_tokens(self) -> list[dict]:
        """One row per credential: static tokens, and OAuth clients as a single family."""
        rows = self.db.execute(
            "SELECT id, name, scope, kind, family, created FROM tokens WHERE revoked=0 AND kind IN ('static','refresh') "
            "AND (expires IS NULL OR expires > ?) ORDER BY created DESC", (time.time(),)
        ).fetchall()
        return [dict(row) for row in rows if row["kind"] == "static" or not row["family"] or self._live_refresh(row)]

    def _live_refresh(self, row) -> bool:
        used = self.db.execute("SELECT used FROM tokens WHERE id=?", (row["id"],)).fetchone()
        return not used["used"]

    def revoke(self, token_id: int, ip: str = "") -> None:
        with self.lock:
            row = self.db.execute("SELECT name, family FROM tokens WHERE id=?", (token_id,)).fetchone()
            if not row:
                return
            if row["family"]:
                self.db.execute("UPDATE tokens SET revoked=1 WHERE family=?", (row["family"],))
            else:
                self.db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (token_id,))
            self.db.commit()
        self.log("token_revoked", row["name"], ip)

    # OAuth clients

    def redirect_allowed(self, uri: str, allowlist: list[str]) -> bool:
        return any(fnmatchcase(uri, pattern) for pattern in allowlist)

    def register_client(self, name: str, redirect_uris: list[str], allowlist: list[str]) -> str:
        if not redirect_uris or not all(self.redirect_allowed(u, allowlist) for u in redirect_uris):
            raise AuthError("redirect_uri not allowed")
        with self.lock:
            self.db.execute("DELETE FROM clients WHERE approved=0 AND created<?", (time.time() - UNUSED_CLIENT_TTL,))
            unapproved = self.db.execute("SELECT count(*) AS n FROM clients WHERE approved=0").fetchone()["n"]
            if unapproved >= MAX_UNAPPROVED_CLIENTS:
                raise AuthError("too many pending client registrations")
            client_id = secrets.token_urlsafe(16)
            self.db.execute(
                "INSERT INTO clients(id, name, redirect_uris, created) VALUES(?,?,?,?)",
                (client_id, name[:80], "\n".join(redirect_uris), time.time()),
            )
            self.db.commit()
        return client_id

    def client(self, client_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()

    # Device-code login

    def _generate_code(self) -> str:
        """Invariant 1: the server picks codes, and never reuses one that is active."""
        while True:
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            code = f"{raw[:4]}-{raw[4:]}"
            clash = self.db.execute(
                "SELECT 1 FROM login_requests WHERE code=? AND status='pending' AND expires>?", (code, time.time())
            ).fetchone()
            if not clash:
                return code

    def create_login_request(self, client_id: str, redirect_uri: str, challenge: str, scope: str, state: str | None, ip: str) -> tuple[str, str]:
        """Returns (secret, code). The secret stays with the requester's browser."""
        with self.lock:
            now = time.time()
            self.db.execute("UPDATE login_requests SET status='expired' WHERE status='pending' AND expires<=?", (now,))
            # Oldest first, so outsiders filling the queue can never lock the owner out.
            for query, args, ceiling in (
                ("ip=? AND status='pending'", (ip,), MAX_PENDING_PER_IP),
                ("status='pending'", (), MAX_PENDING_TOTAL),
            ):
                rows = self.db.execute(f"SELECT id FROM login_requests WHERE {query} ORDER BY id", args).fetchall()
                for row in rows[: max(0, len(rows) - ceiling + 1)]:
                    self.db.execute("UPDATE login_requests SET status='evicted' WHERE id=?", (row["id"],))
            secret = secrets.token_urlsafe(32)
            code = self._generate_code()
            self.db.execute(
                "INSERT INTO login_requests(secret_hash, code, client_id, redirect_uri, challenge, scope, state, ip, created, expires, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')",
                (digest(secret), code, client_id, redirect_uri, challenge, scope, state, ip, now, now + REQUEST_TTL),
            )
            self.db.commit()
        return secret, code

    def pending_by_code(self, code: str) -> sqlite3.Row | None:
        normal = code.strip().upper().replace(" ", "")
        if len(normal) == 8:
            normal = f"{normal[:4]}-{normal[4:]}"
        return self.db.execute(
            "SELECT r.*, c.name AS client_name FROM login_requests r JOIN clients c ON c.id=r.client_id "
            "WHERE r.code=? AND r.status='pending' AND r.expires>?", (normal, time.time())
        ).fetchone()

    def decide(self, request_id: int, approve: bool, scope: str, ip: str) -> None:
        with self.lock:
            row = self.db.execute(
                "SELECT r.*, c.name AS client_name FROM login_requests r JOIN clients c ON c.id=r.client_id "
                "WHERE r.id=? AND r.status='pending' AND r.expires>?", (request_id, time.time())
            ).fetchone()
            if not row:
                raise AuthError("request is gone or expired")
            status = "approved" if approve else "denied"
            self.db.execute("UPDATE login_requests SET status=?, scope=? WHERE id=?", (status, scope, request_id))
            if approve:
                self.db.execute("UPDATE clients SET approved=1 WHERE id=?", (row["client_id"],))
            self.db.commit()
        self.log(f"login_{status}", f"{row['client_name']} ({scope}) from {row['ip']}", ip)

    def poll(self, secret: str | None) -> dict:
        """Invariant 2: only the holder of the request secret ever receives the auth code."""
        if not secret:
            return {"status": "unknown"}
        with self.lock:
            row = self.db.execute("SELECT * FROM login_requests WHERE secret_hash=?", (digest(secret),)).fetchone()
            if not row:
                return {"status": "unknown"}
            if row["status"] == "pending" and row["expires"] <= time.time():
                return {"status": "expired"}
            if row["status"] != "approved":
                return {"status": row["status"]}
            code = secrets.token_urlsafe(32)
            self.db.execute(
                "UPDATE login_requests SET status='issued', auth_code_hash=?, auth_expires=? WHERE id=?",
                (digest(code), time.time() + AUTH_CODE_TTL, row["id"]),
            )
            self.db.commit()
            return {"status": "approved", "code": code, "redirect_uri": row["redirect_uri"], "state": row["state"]}

    def exchange_code(self, code: str, client_id: str, redirect_uri: str, verifier: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT r.*, c.name AS client_name FROM login_requests r JOIN clients c ON c.id=r.client_id "
                "WHERE r.auth_code_hash=? AND r.status='issued'", (digest(code),)
            ).fetchone()
            if not row or row["auth_expires"] <= time.time():
                raise AuthError("invalid_grant")
            # One attempt per code, whether or not it succeeds.
            self.db.execute("UPDATE login_requests SET status='used' WHERE id=?", (row["id"],))
            self.db.commit()
            computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri or not secrets.compare_digest(computed, row["challenge"]):
                raise AuthError("invalid_grant")
            return self._issue(row["client_name"], row["scope"], secrets.token_urlsafe(16))

    def refresh(self, refresh_token: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tokens WHERE hash=? AND kind='refresh' AND revoked=0", (digest(refresh_token),)
            ).fetchone()
            if not row or (row["expires"] and row["expires"] <= time.time()):
                raise AuthError("invalid_grant")
            if row["used"]:
                # A rotated token came back: it leaked. Cut off the whole family.
                self.db.execute("UPDATE tokens SET revoked=1 WHERE family=?", (row["family"],))
                self.db.commit()
                self.log("refresh_reuse", row["name"])
                raise AuthError("invalid_grant")
            self.db.execute("UPDATE tokens SET used=1 WHERE id=?", (row["id"],))
            self.db.commit()
            return self._issue(row["name"], row["scope"], row["family"])

    def _issue(self, name: str, scope: str, family: str) -> dict:
        return {
            "access_token": self._new_token(name, scope, "access", family, ACCESS_TTL),
            "refresh_token": self._new_token(name, scope, "refresh", family, REFRESH_TTL),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "scope": scope,
        }
