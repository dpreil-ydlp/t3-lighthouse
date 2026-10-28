import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from lighthouse.db import connection

hasher = PasswordHasher()
_VERIFIED_TOKEN_CACHE_TTL_SECONDS = 60
_VERIFIED_TOKEN_CACHE_MAX_ENTRIES = 256
_verified_token_cache: OrderedDict[str, str] = OrderedDict()
_verified_token_expiry: dict[str, float] = {}
_verified_token_cache_lock = threading.Lock()
_monotonic = time.monotonic


def _cached_principal_id(token_digest: str, now: float) -> str | None:
    with _verified_token_cache_lock:
        principal_id = _verified_token_cache.get(token_digest)
        if principal_id is None:
            return None
        if _verified_token_expiry.get(token_digest, 0) <= now:
            _verified_token_cache.pop(token_digest, None)
            _verified_token_expiry.pop(token_digest, None)
            return None
        _verified_token_cache.move_to_end(token_digest)
        return principal_id


def _cache_verified_token(token_digest: str, principal_id: str, now: float) -> None:
    with _verified_token_cache_lock:
        _verified_token_cache[token_digest] = principal_id
        _verified_token_expiry[token_digest] = now + _VERIFIED_TOKEN_CACHE_TTL_SECONDS
        _verified_token_cache.move_to_end(token_digest)
        while len(_verified_token_cache) > _VERIFIED_TOKEN_CACHE_MAX_ENTRIES:
            expired_digest, _ = _verified_token_cache.popitem(last=False)
            _verified_token_expiry.pop(expired_digest, None)


@dataclass
class Principal:
    id: str
    name: str
    kind: str
    scopes: list[str]


def auth(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    token = authorization[7:]
    lookup = hashlib.sha256(token.encode()).hexdigest()
    cached_id = _cached_principal_id(lookup, _monotonic())
    with connection() as conn:
        if cached_id is not None:
            row = conn.execute("SELECT id,name,kind,permission_scopes FROM principals WHERE id=%s AND enabled=true AND token_lookup_hash=%s", (cached_id, lookup)).fetchone()
            if row:
                return Principal(str(row["id"]), row["name"], row["kind"], row["permission_scopes"])
            with _verified_token_cache_lock:
                _verified_token_cache.pop(lookup, None)
                _verified_token_expiry.pop(lookup, None)
        row = conn.execute("SELECT id,name,kind,permission_scopes,token_hash FROM principals WHERE enabled=true AND token_lookup_hash=%s", (lookup,)).fetchone()
    if row:
        try:
            if hasher.verify(row["token_hash"], token):
                _cache_verified_token(lookup, str(row["id"]), _monotonic())
                return Principal(str(row["id"]), row["name"], row["kind"], row["permission_scopes"])
        except VerifyMismatchError:
            pass
    raise HTTPException(401, "Invalid bearer token")


def require(scope: str, user_only: bool = False):
    def dependency(principal: Principal = Depends(auth)):
        if scope not in principal.scopes:
            raise HTTPException(403, "Required scope is missing")
        if user_only and principal.kind != "user":
            raise HTTPException(403, "User principal required")
        return principal
    return dependency


def claim_idempotency(conn, principal: Principal, key: str, payload: Any):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = conn.execute("SELECT request_hash,response FROM idempotency_records WHERE principal_id=%s AND idempotency_key=%s FOR UPDATE", (principal.id, key)).fetchone()
    if existing:
        if existing["request_hash"] != digest:
            raise HTTPException(409, "Idempotency key was used with a different request")
        return existing["response"], True, digest
    conn.execute("INSERT INTO idempotency_records(principal_id,idempotency_key,request_hash) VALUES (%s,%s,%s)", (principal.id, key, digest))
    return None, False, digest


def save_idempotent(conn, principal: Principal, key: str, response: dict):
    saved = jsonable_encoder(response)
    conn.execute("UPDATE idempotency_records SET response=%s WHERE principal_id=%s AND idempotency_key=%s", (Jsonb(saved), principal.id, key))


def create_token_file(path: str, token: str):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(token + "\n")
