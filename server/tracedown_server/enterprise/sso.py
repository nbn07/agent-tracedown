"""Single sign-on with OpenID Connect (Okta, Microsoft Entra ID, Google Workspace, Auth0,
Keycloak, and any other OIDC provider).

Flow (authorization code + PKCE):

1. ``GET /auth/oidc/login`` redirects to the provider with ``state``, ``nonce`` and a
   PKCE challenge.
2. ``GET /auth/oidc/callback`` exchanges the code at the token endpoint, verifies the
   ID token (RS256/ES256 signature against the provider's JWKS, ``iss``, ``aud``,
   ``exp``, ``nonce``), maps the groups claim to TDN roles, upserts the user and sets
   an ``tdn_session`` cookie.

Configuration (environment):

    TDN_OIDC_ISSUER=https://acme.okta.com
    TDN_OIDC_CLIENT_ID=...    TDN_OIDC_CLIENT_SECRET=...
    TDN_OIDC_REDIRECT_URI=https://tdn.acme.com/auth/oidc/callback
    TDN_OIDC_GROUPS_CLAIM=groups
    TDN_OIDC_ROLE_MAP='{"tdn-owners": "owner", "ml-platform": {"*": "admin"}, "support-eng": {"support-bot": "developer"}}'
    TDN_OIDC_ALLOWED_DOMAINS=acme.com

Requires ``pyjwt[crypto]``. HTTP calls go through an injectable ``fetch`` function.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

Fetch = Callable[[str, Optional[Dict[str, str]]], Dict[str, Any]]


def http_fetch(url: str, form: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class SSOError(Exception):
    pass


class OIDC:
    def __init__(self, issuer: str, client_id: str, client_secret: str, redirect_uri: str,
                 groups_claim: str = "groups", role_map: Optional[Dict[str, Any]] = None,
                 allowed_domains: Optional[List[str]] = None, fetch: Fetch = http_fetch,
                 scopes: str = "openid email profile") -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.groups_claim, self.role_map = groups_claim, role_map or {}
        self.allowed_domains = [d.lower() for d in (allowed_domains or [])]
        self.fetch, self.scopes = fetch, scopes
        self._discovery: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._pending: Dict[str, Tuple[str, str, float]] = {}  # state -> (nonce, verifier, expires)

    @classmethod
    def from_env(cls, fetch: Fetch = http_fetch) -> Optional["OIDC"]:
        issuer = os.environ.get("TDN_OIDC_ISSUER")
        if not issuer:
            return None
        return cls(issuer, os.environ["TDN_OIDC_CLIENT_ID"], os.environ.get("TDN_OIDC_CLIENT_SECRET", ""),
                   os.environ["TDN_OIDC_REDIRECT_URI"], os.environ.get("TDN_OIDC_GROUPS_CLAIM", "groups"),
                   json.loads(os.environ.get("TDN_OIDC_ROLE_MAP", "{}")),
                   [d for d in os.environ.get("TDN_OIDC_ALLOWED_DOMAINS", "").split(",") if d], fetch)

    # -------------------------------------------------------------- discovery
    def discovery(self) -> Dict[str, Any]:
        if self._discovery is None:
            self._discovery = self.fetch(f"{self.issuer}/.well-known/openid-configuration", None)
            if self._discovery.get("issuer", "").rstrip("/") != self.issuer:
                raise SSOError("discovery issuer does not match configured issuer")
        return self._discovery

    def _jwk_for(self, kid: Optional[str], refresh: bool = False) -> Dict[str, Any]:
        if self._jwks is None or refresh:
            self._jwks = self.fetch(self.discovery()["jwks_uri"], None)
        for k in self._jwks.get("keys", []):
            if kid is None or k.get("kid") == kid:
                return k
        if not refresh:  # keys rotate: refetch once
            return self._jwk_for(kid, refresh=True)
        raise SSOError(f"signing key {kid!r} not in provider JWKS")

    # -------------------------------------------------------------- step 1
    def login_url(self) -> Tuple[str, str]:
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        now = time.time()
        self._pending = {s: v for s, v in self._pending.items() if v[2] > now}
        self._pending[state] = (nonce, verifier, now + 600)
        q = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "scope": self.scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{self.discovery()['authorization_endpoint']}?{q}", state

    # -------------------------------------------------------------- step 2
    def callback(self, code: str, state: str) -> Dict[str, Any]:
        pending = self._pending.pop(state, None)
        if pending is None or pending[2] < time.time():
            raise SSOError("unknown or expired login state")
        nonce, verifier, _ = pending
        tokens = self.fetch(self.discovery()["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier})
        if "id_token" not in tokens:
            raise SSOError(f"token endpoint returned no id_token: {tokens.get('error', 'unknown error')}")
        claims = self.verify_id_token(tokens["id_token"], nonce)
        return {"claims": claims, **self.map_identity(claims)}

    def verify_id_token(self, token: str, nonce: Optional[str]) -> Dict[str, Any]:
        import jwt  # pyjwt[crypto]

        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256"):
            raise SSOError(f"refusing ID token alg {header.get('alg')!r}")
        key = jwt.PyJWK(self._jwk_for(header.get("kid")))
        try:
            claims = jwt.decode(token, key.key, algorithms=[header["alg"]], audience=self.client_id,
                                issuer=self.discovery()["issuer"], leeway=60,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise SSOError(f"invalid ID token: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise SSOError("ID token nonce mismatch")
        return claims

    def map_identity(self, claims: Dict[str, Any]) -> Dict[str, Any]:
        email = (claims.get("email") or "").lower()
        if not email:
            raise SSOError("ID token has no email claim; add the 'email' scope")
        if claims.get("email_verified") is False:
            raise SSOError("email not verified by the identity provider")
        if self.allowed_domains and email.split("@")[-1] not in self.allowed_domains:
            raise SSOError(f"email domain not allowed: {email.split('@')[-1]}")
        groups = claims.get(self.groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        org_role, roles = "member", {}
        rank = {"viewer": 1, "developer": 2, "admin": 3}
        for g in groups:
            m = self.role_map.get(g)
            if m == "owner":
                org_role = "owner"
            elif isinstance(m, dict):
                for project, role in m.items():
                    if rank.get(role, 0) > rank.get(roles.get(project), 0):
                        roles[project] = role
        # Projects and owner status the role map controls: the IdP is the source of truth for
        # these, so losing a group removes the access it granted at the next sign-in.
        managed = sorted({p for m in self.role_map.values() if isinstance(m, dict) for p in m})
        owner_mapped = any(m == "owner" for m in self.role_map.values())
        return {"email": email, "name": claims.get("name"), "subject": claims["sub"],
                "org_role": org_role, "roles": roles, "groups": groups,
                "managed_projects": managed, "owner_mapped": owner_mapped}
