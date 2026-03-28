#!/usr/bin/env python3
"""
Authentication Service Demo
Implements: Session auth, JWT auth, OAuth2 simulation, API key auth
"""

import os
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
import redis
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

# Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'dev-secret')
SESSION_SECRET = os.environ.get('SESSION_SECRET', 'session-secret')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
JWT_EXPIRY_MINUTES = 15
REFRESH_TOKEN_EXPIRY_DAYS = 7

# Initialize Redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# In-memory "database" of users (in production: use real database)
USERS = {
    'alice': {
        'password_hash': hashlib.sha256('password123'.encode()).hexdigest(),  # Demo only - use bcrypt in prod
        'role': 'admin',
        'user_id': 'user_001'
    },
    'bob': {
        'password_hash': hashlib.sha256('password456'.encode()).hexdigest(),
        'role': 'user',
        'user_id': 'user_002'
    }
}

# API Keys database
API_KEYS = {
    'ak_prod_12345abcdef': {'name': 'Mobile App', 'scopes': ['read:posts', 'write:posts']},
    'ak_prod_67890ghijkl': {'name': 'Partner Integration', 'scopes': ['read:posts']},
}

# OAuth simulation: authorization codes
AUTH_CODES = {}  # code -> {user_id, client_id, expires}


def verify_password(username, password):
    """Verify password against stored hash."""
    if username not in USERS:
        return False
    hashed = hashlib.sha256(password.encode()).hexdigest()
    return USERS[username]['password_hash'] == hashed


def create_jwt_token(user_id, username, role):
    """Create a JWT access token."""
    now = datetime.now(timezone.utc)
    payload = {
        'sub': user_id,
        'username': username,
        'role': role,
        'iat': now,
        'exp': now + timedelta(minutes=JWT_EXPIRY_MINUTES),
        'jti': str(uuid.uuid4()),  # JWT ID for potential revocation
        'type': 'access'
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def create_refresh_token(user_id):
    """Create a refresh token (opaque token stored in Redis)."""
    token = secrets.token_urlsafe(32)
    redis_client.setex(
        f"refresh_token:{token}",
        timedelta(days=REFRESH_TOKEN_EXPIRY_DAYS),
        user_id
    )
    return token


def verify_jwt_token(token):
    """Verify and decode JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_session(session_id):
    """Get session data from Redis."""
    session_key = f"session:{session_id}"
    session_data = redis_client.hgetall(session_key)
    if session_data:
        # Extend session on activity
        redis_client.expire(session_key, timedelta(hours=24))
    return session_data


def create_session(user_id, username, role):
    """Create a new session in Redis."""
    session_id = secrets.token_urlsafe(32)
    session_key = f"session:{session_id}"
    redis_client.hset(session_key, mapping={
        'user_id': user_id,
        'username': username,
        'role': role
    })
    redis_client.expire(session_key, timedelta(hours=24))
    return session_id


def require_api_key(f):
    """Decorator to require valid API key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return jsonify({'error': 'API key required'}), 401
        if api_key not in API_KEYS:
            return jsonify({'error': 'Invalid API key'}), 401
        request.api_key_data = API_KEYS[api_key]
        return f(*args, **kwargs)
    return decorated


# ============================================================================
# Health Check
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})


# ============================================================================
# Session-Based Authentication
# ============================================================================

@app.route('/auth/session/login', methods=['POST'])
def session_login():
    """Login with username/password, create server-side session."""
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not verify_password(username, password):
        return jsonify({'error': 'Invalid credentials'}), 401

    user = USERS[username]
    session_id = create_session(user['user_id'], username, user['role'])

    response = make_response(jsonify({
        'message': 'Login successful',
        'username': username,
        'role': user['role']
    }))
    response.set_cookie(
        'session_id',
        session_id,
        httponly=True,
        secure=False,  # Set True in production with HTTPS
        samesite='Strict',
        max_age=24*60*60  # 24 hours
    )
    return response


@app.route('/auth/session/me', methods=['GET'])
def session_me():
    """Get current user from session cookie."""
    session_id = request.cookies.get('session_id')
    if not session_id:
        return jsonify({'error': 'No session'}), 401

    session = get_session(session_id)
    if not session:
        return jsonify({'error': 'Invalid or expired session'}), 401

    return jsonify({
        'user_id': session['user_id'],
        'username': session['username'],
        'role': session['role'],
        'auth_method': 'session'
    })


@app.route('/auth/session/logout', methods=['POST'])
def session_logout():
    """Logout - invalidate session server-side."""
    session_id = request.cookies.get('session_id')
    if session_id:
        redis_client.delete(f"session:{session_id}")

    response = make_response(jsonify({'message': 'Logged out'}))
    response.delete_cookie('session_id')
    return response


# ============================================================================
# JWT-Based Authentication
# ============================================================================

@app.route('/auth/jwt/login', methods=['POST'])
def jwt_login():
    """Login with username/password, return JWT tokens."""
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not verify_password(username, password):
        return jsonify({'error': 'Invalid credentials'}), 401

    user = USERS[username]
    access_token = create_jwt_token(user['user_id'], username, user['role'])
    refresh_token = create_refresh_token(user['user_id'])

    return jsonify({
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': 'Bearer',
        'expires_in': JWT_EXPIRY_MINUTES * 60
    })


@app.route('/auth/jwt/refresh', methods=['POST'])
def jwt_refresh():
    """Refresh access token using refresh token."""
    data = request.get_json()
    refresh_token = data.get('refresh_token')

    if not refresh_token:
        return jsonify({'error': 'Refresh token required'}), 400

    user_id = redis_client.get(f"refresh_token:{refresh_token}")
    if not user_id:
        return jsonify({'error': 'Invalid or expired refresh token'}), 401

    # Rotate refresh token (single use)
    redis_client.delete(f"refresh_token:{refresh_token}")

    # Find user by ID
    username = None
    role = None
    for uname, udata in USERS.items():
        if udata['user_id'] == user_id:
            username = uname
            role = udata['role']
            break

    if not username:
        return jsonify({'error': 'User not found'}), 404

    new_access_token = create_jwt_token(user_id, username, role)
    new_refresh_token = create_refresh_token(user_id)

    return jsonify({
        'access_token': new_access_token,
        'refresh_token': new_refresh_token,
        'token_type': 'Bearer',
        'expires_in': JWT_EXPIRY_MINUTES * 60
    })


@app.route('/auth/jwt/verify', methods=['POST'])
def jwt_verify():
    """Verify a JWT token (for other services to call)."""
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({'error': 'Token required'}), 400

    payload = verify_jwt_token(token)
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401

    return jsonify({
        'valid': True,
        'sub': payload['sub'],
        'username': payload['username'],
        'role': payload['role'],
        'exp': payload['exp']
    })


# ============================================================================
# OAuth 2.0 Simulation (Authorization Code Flow)
# ============================================================================

@app.route('/auth/oauth/authorize', methods=['GET'])
def oauth_authorize():
    """Simulate authorization endpoint (normally shows login/consent page)."""
    client_id = request.args.get('client_id')
    redirect_uri = request.args.get('redirect_uri')
    state = request.args.get('state', '')

    # In real OAuth, this would render a login/consent page
    # For demo, we auto-approve and return an auth code
    auth_code = secrets.token_urlsafe(32)
    AUTH_CODES[auth_code] = {
        'user_id': 'user_001',  # Simulated logged-in user
        'client_id': client_id,
        'expires': datetime.now() + timedelta(minutes=5)
    }

    # Redirect back to client with auth code
    return jsonify({
        'authorization_code': auth_code,
        'state': state,
        'note': 'In real OAuth, this would be a redirect to ' + redirect_uri
    })


@app.route('/auth/oauth/token', methods=['POST'])
def oauth_token():
    """Exchange authorization code for access token."""
    data = request.get_json()
    grant_type = data.get('grant_type')
    code = data.get('code')
    client_id = data.get('client_id')
    client_secret = data.get('client_secret')

    if grant_type != 'authorization_code':
        return jsonify({'error': 'unsupported_grant_type'}), 400

    auth_data = AUTH_CODES.get(code)
    if not auth_data:
        return jsonify({'error': 'invalid_grant'}), 400

    # Check expiration
    if datetime.now() > auth_data['expires']:
        del AUTH_CODES[code]
        return jsonify({'error': 'invalid_grant'}), 400

    # Generate tokens
    user_id = auth_data['user_id']
    username = 'alice'  # Simplified lookup
    role = USERS[username]['role']

    access_token = create_jwt_token(user_id, username, role)
    refresh_token = create_refresh_token(user_id)

    # Consume the auth code
    del AUTH_CODES[code]

    return jsonify({
        'access_token': access_token,
        'token_type': 'Bearer',
        'expires_in': JWT_EXPIRY_MINUTES * 60,
        'refresh_token': refresh_token
    })


# ============================================================================
# API Key Authentication
# ============================================================================

@app.route('/auth/apikey/verify', methods=['GET'])
@require_api_key
def apikey_verify():
    """Verify API key and return scopes."""
    return jsonify({
        'valid': True,
        'name': request.api_key_data['name'],
        'scopes': request.api_key_data['scopes']
    })


@app.route('/auth/apikey/test-scope', methods=['POST'])
@require_api_key
def apikey_test_scope():
    """Test if API key has required scope."""
    data = request.get_json()
    required_scope = data.get('scope', 'read:posts')

    if required_scope not in request.api_key_data['scopes']:
        return jsonify({
            'error': 'Insufficient scope',
            'required': required_scope,
            'granted': request.api_key_data['scopes']
        }), 403

    return jsonify({
        'message': 'Scope check passed',
        'scope': required_scope
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
