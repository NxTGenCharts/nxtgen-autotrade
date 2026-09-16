import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import request, jsonify
from functools import wraps

from models import get_db

SESSION_DAYS = 30


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return digest, salt


def verify_password(password, salt, expected_hash):
    digest, _ = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    db = get_db()
    db.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)", (token, user_id, expires))
    db.commit()
    return token


def get_user_from_token(token):
    if not token:
        return None
    db = get_db()
    row = db.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND s.expires_at > ?""",
        (token, datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    return row


def current_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("session_token")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_user_from_token(current_token())
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        if user["is_suspended"]:
            return jsonify({"error": "Account suspended"}), 403
        request.user = user
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_user_from_token(current_token())
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        if user["role"] != "admin":
            return jsonify({"error": "Admin access required"}), 403
        request.user = user
        return fn(*args, **kwargs)
    return wrapper
