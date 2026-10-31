"""Role-based access control: users, project roles, API keys and sessions.

Roles (per project; ``*`` = every project):

| role       | can                                                                      |
|------------|--------------------------------------------------------------------------|
| ingest     | send spans and blobs (for SDK keys in production agents)                 |
| viewer     | read runs, payloads, findings, incidents, drift, metrics                 |
| developer  | viewer + run analysis, plan replays, change incident status              |
| admin      | developer + manage hooks, PII policy, API keys and project members      |

Org owners can do everything, including managing users and reading the audit log.

Auth modes: with no users, keys or ``TDN_API_KEYS`` configured and ``TDN_AUTH`` not set
to ``required``, the server runs open (dev). Otherwise every request needs a key
(``Authorization: Bearer tdn_...``) or an SSO session (cookie ``tdn_session``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE,
    name         TEXT,
    org_role     TEXT NOT NULL DEFAULT 'member',
    sso_subject  TEXT,
    disabled     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    last_login   INTEGER
);
CREATE TABLE IF NOT EXISTS memberships (
    user_id  INTEGER NOT NULL,
    project  TEXT NOT NULL,
    role     TEXT NOT NULL,
    PRIMARY KEY (user_id, project)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    prefix        TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    project       TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_by    TEXT,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    revoked_at    INTEGER
);
"""

READ = {"runs:read", "payloads:read", "incidents:read", "drift:read", "metrics:read", "hooks:read"}
ROLES: Dict[str, set] = {
    "ingest": {"ingest"},
    "viewer": READ,
    "developer": READ | {"analysis:run", "replay:plan", "incidents:write"},
    "admin": READ | {"analysis:run", "replay:plan", "incidents:write", "hooks:manage", "policies:manage",
                     "keys:manage", "members:manage", "ingest"},
}
ORG_ONLY = {"users:manage", "audit:read"}


@dataclass
class Principal:
    id: str
    kind: str                      # user | key | static | anonymous
    email: Optional[str] = None
    org_role: str = "member"
    roles: Dict[str, str] = field(default_factory=dict)   # project -> role

    def permissions(self, project: Optional[str]) -> set:
        if self.org_role == "owner":
            return set().union(*ROLES.values()) | ORG_ONLY
        role = self.roles.get(project) if project else None
        role = role or self.roles.get("*")
        if role is None and project is None and self.roles:
            # No project named: a project-scoped principal may only *read*, and every such route
            # filters results to its projects. Anything that writes or configures across projects
            # (global hooks, prices, scans) needs a project, an org owner or a "*" role.
            best = max(self.roles.values(), key=lambda r: len(ROLES[r]))
            return set(ROLES.get(best, set())) & READ
        return set(ROLES.get(role, set()))

    @property
    def unrestricted(self) -> bool:
        return self.projects() is None

    def can(self, permission: str, project: Optional[str] = None) -> bool:
        return permission in self.permissions(project)

    def projects(self) -> Optional[List[str]]:
        """Projects this principal may see; None = all."""
        if self.org_role == "owner" or "*" in self.roles:
            return None
        return sorted(self.roles)

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "email": self.email, "org_role": self.org_role, "roles": self.roles}


ANONYMOUS = Principal("anonymous", "anonymous", org_role="owner")


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ----------------------------------------------------------------------------- sessions (HS256 JWT)

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Sessions:
    def __init__(self, secret: Optional[str] = None, ttl_s: int = 12 * 3600) -> None:
        self.secret = (secret or os.environ.get("TDN_SESSION_SECRET") or secrets.token_hex(32)).encode()
        self.ttl = ttl_s

    def issue(self, user_id: int, email: str) -> str:
        now = int(time.time())
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps({"sub": str(user_id), "email": email, "iat": now, "exp": now + self.ttl}).encode())
        sig = _b64(hmac.new(self.secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
        return f"{head}.{body}.{sig}"

    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            head, body, sig = token.split(".")
        except ValueError:
            return None
        good = _b64(hmac.new(self.secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        claims = json.loads(_unb64(body))
        return claims if claims.get("exp", 0) > time.time() else None


# ----------------------------------------------------------------------------- directory

class Directory:
    def __init__(self, store, static_keys: Optional[List[str]] = None, sessions: Optional[Sessions] = None,
                 require_auth: Optional[bool] = None) -> None:
        self.store = store
        store.ensure_schema(SCHEMA)
        self.static = set(static_keys if static_keys is not None else
                          [k for k in os.environ.get("TDN_API_KEYS", "").split(",") if k])
        self.sessions = sessions or Sessions()
        self.require_auth = require_auth if require_auth is not None else \
            os.environ.get("TDN_AUTH", "").lower() == "required"

    # -------------------------------------------------------------- mode
    def open_mode(self) -> bool:
        if self.require_auth or self.static:
            return False
        has = self.store.query("SELECT (SELECT COUNT(*) FROM users) u, "
                               "(SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL) k")[0]
        return has["u"] == 0 and has["k"] == 0

    # -------------------------------------------------------------- users
    def upsert_user(self, email: str, name: Optional[str] = None, org_role: Optional[str] = None,
                    sso_subject: Optional[str] = None) -> Dict[str, Any]:
        email = email.lower()
        existing = self.store.query("SELECT * FROM users WHERE email=?", (email,))
        if existing:
            sets, args = ["last_login=?"], [time.time_ns()]
            for col, val in (("name", name), ("org_role", org_role), ("sso_subject", sso_subject)):
                if val is not None:
                    sets.append(f"{col}=?")
                    args.append(val)
            self.store.execute(f"UPDATE users SET {', '.join(sets)} WHERE email=?", args + [email])
        else:
            # The first user becomes owner only on a fresh install with no keys configured; otherwise
            # ownership comes from an IdP group mapped to "owner" or from an existing owner.
            bootstrap = self.open_mode()
            self.store.execute("INSERT INTO users(email, name, org_role, sso_subject, created_at) VALUES (?,?,?,?,?)",
                               (email, name, org_role or ("owner" if bootstrap else "member"), sso_subject, time.time_ns()))
        return self.store.query("SELECT * FROM users WHERE email=?", (email,))[0]

    def set_membership(self, user_id: int, project: str, role: Optional[str]) -> None:
        if role is None:
            self.store.execute("DELETE FROM memberships WHERE user_id=? AND project=?", (user_id, project))
            return
        if role not in ROLES or role == "ingest":
            raise ValueError("role must be viewer, developer or admin")
        self.store.execute("INSERT INTO memberships(user_id, project, role) VALUES (?,?,?) "
                           "ON CONFLICT(user_id, project) DO UPDATE SET role=excluded.role", (user_id, project, role))

    def list_users(self) -> List[Dict[str, Any]]:
        users = self.store.query("SELECT id, email, name, org_role, disabled, created_at, last_login FROM users")
        for u in users:
            u["roles"] = {m["project"]: m["role"] for m in
                          self.store.query("SELECT project, role FROM memberships WHERE user_id=?", (u["id"],))}
        return users

    def principal_for_user(self, user: Dict[str, Any]) -> Optional[Principal]:
        if user["disabled"]:
            return None
        roles = {m["project"]: m["role"] for m in
                 self.store.query("SELECT project, role FROM memberships WHERE user_id=?", (user["id"],))}
        return Principal(f"user:{user['id']}", "user", user["email"], user["org_role"], roles)

    # -------------------------------------------------------------- keys
    def create_key(self, project: str, role: str, name: Optional[str] = None, created_by: str = "system") -> Dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        key = "tdn_" + secrets.token_urlsafe(32)
        kid = self.store.execute("INSERT INTO api_keys(name, prefix, key_hash, project, role, created_by, created_at) "
                                 "VALUES (?,?,?,?,?,?,?)",
                                 (name, key[:12], _hash_key(key), project, role, created_by, time.time_ns()))
        return {"id": kid, "key": key, "prefix": key[:12], "project": project, "role": role,
                "note": "Store this key now; it is not shown again."}

    def list_keys(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT id, name, prefix, project, role, created_by, created_at, last_used_at, revoked_at FROM api_keys"
        return self.store.query(q + (" WHERE project=?" if project else ""), (project,) if project else ())

    def revoke_key(self, key_id: int) -> None:
        self.store.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (time.time_ns(), key_id))

    # -------------------------------------------------------------- resolve
    def resolve(self, authorization: Optional[str], session_cookie: Optional[str]) -> Optional[Principal]:
        token = (authorization or "").removeprefix("Bearer ").strip() or None
        if token and token in self.static:
            return Principal("static", "static", org_role="owner")
        if token and token.startswith("tdn_"):
            rows = self.store.query("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (_hash_key(token),))
            if rows:
                k = rows[0]
                self.store.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time_ns(), k["id"]))
                return Principal(f"key:{k['id']}", "key", None, "member", {k["project"]: k["role"]})
            return None
        for candidate in (token, session_cookie):
            if candidate:
                claims = self.sessions.verify(candidate)
                if claims:
                    rows = self.store.query("SELECT * FROM users WHERE id=?", (int(claims["sub"]),))
                    return self.principal_for_user(rows[0]) if rows else None
        return ANONYMOUS if self.open_mode() else None
