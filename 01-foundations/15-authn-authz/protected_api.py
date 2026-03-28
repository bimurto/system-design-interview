#!/usr/bin/env python3
"""
Protected API Service Demo
Demonstrates RBAC authorization and token validation
"""

import os
from functools import wraps

import jwt
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

JWT_SECRET = os.environ.get('JWT_SECRET', 'dev-secret')
AUTH_SERVICE_URL = os.environ.get('AUTH_SERVICE_URL', 'http://localhost:5001')


def get_auth_header():
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def verify_token_locally(token):
    """Verify JWT locally (for microservices that trust the signing key)."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return {'error': 'Token expired'}
    except jwt.InvalidTokenError as e:
        return {'error': f'Invalid token: {str(e)}'}


def verify_token_remote(token):
    """Verify JWT by calling auth service (for services that don't have the key)."""
    try:
        response = requests.post(
            f"{AUTH_SERVICE_URL}/auth/jwt/verify",
            json={'token': token},
            timeout=2
        )
        if response.status_code == 200:
            return response.json()
        return {'error': 'Token validation failed'}
    except requests.RequestException as e:
        return {'error': f'Auth service unavailable: {str(e)}'}


def require_auth(f):
    """Decorator to require valid JWT."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_auth_header()
        if not token:
            return jsonify({'error': 'Authorization header required'}), 401

        # Use local verification for speed
        payload = verify_token_locally(token)
        if 'error' in payload:
            return jsonify(payload), 401

        request.user = payload
        return f(*args, **kwargs)
    return decorated


def require_role(role):
    """Decorator factory to require specific role."""
    def decorator(f):
        @wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            if request.user.get('role') != role:
                return jsonify({
                    'error': 'Forbidden',
                    'message': f'Required role: {role}, your role: {request.user.get("role")}'
                }), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ============================================================================
# Health Check
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})


# ============================================================================
# Protected Endpoints (require authentication)
# ============================================================================

@app.route('/api/me', methods=['GET'])
@require_auth
def get_me():
    """Get current user info from token."""
    return jsonify({
        'user_id': request.user.get('sub'),
        'username': request.user.get('username'),
        'role': request.user.get('role')
    })


@app.route('/api/posts', methods=['GET'])
@require_auth
def list_posts():
    """List posts - any authenticated user."""
    # Simulate RBAC check
    return jsonify({
        'message': 'Posts retrieved successfully',
        'user': request.user.get('username'),
        'posts': [
            {'id': 1, 'title': 'First Post', 'author': 'alice'},
            {'id': 2, 'title': 'Second Post', 'author': 'bob'}
        ]
    })


@app.route('/api/posts', methods=['POST'])
@require_auth
def create_post():
    """Create post - requires authentication (RBAC could restrict further)."""
    data = request.get_json()
    return jsonify({
        'message': 'Post created',
        'author': request.user.get('username'),
        'title': data.get('title')
    })


# ============================================================================
# Admin-only Endpoints (RBAC enforcement)
# ============================================================================

@app.route('/api/admin/users', methods=['GET'])
@require_role('admin')
def list_users():
    """List all users - admin only."""
    return jsonify({
        'message': 'Admin access granted',
        'users': [
            {'id': 'user_001', 'username': 'alice', 'role': 'admin'},
            {'id': 'user_002', 'username': 'bob', 'role': 'user'}
        ]
    })


@app.route('/api/admin/config', methods=['POST'])
@require_role('admin')
def update_config():
    """Update system config - admin only."""
    return jsonify({
        'message': 'Config updated',
        'by': request.user.get('username')
    })


# ============================================================================
# Mixed Authorization Endpoint
# ============================================================================

@app.route('/api/resource/<resource_id>', methods=['GET', 'PUT', 'DELETE'])
@require_auth
def access_resource(resource_id):
    """Demonstrate resource-level authorization."""
    method = request.method
    username = request.user.get('username')
    role = request.user.get('role')

    # Simulate resource ownership check
    # In production: query database to check resource.owner_id == user_id
    resource_owner = 'alice'  # Simulated: resource 123 is owned by alice

    if method == 'GET':
        # Anyone can read
        return jsonify({
            'resource_id': resource_id,
            'owner': resource_owner,
            'data': 'Sensitive resource data'
        })

    elif method in ['PUT', 'DELETE']:
        # Only owner or admin can modify
        if username != resource_owner and role != 'admin':
            return jsonify({
                'error': 'Forbidden',
                'message': f'You do not own this resource. Owner: {resource_owner}'
            }), 403

        return jsonify({
            'message': f'Resource {resource_id} {method.lower()}d successfully',
            'by': username
        })


# ============================================================================
# Token Introspection (for debugging)
# ============================================================================

@app.route('/debug/token', methods=['GET'])
def debug_token():
    """Debug endpoint to inspect token contents."""
    token = get_auth_header()
    if not token:
        return jsonify({'error': 'No token provided'}), 400

    # Decode without verification to show contents
    try:
        import base64
        parts = token.split('.')
        header = base64.urlsafe_b64decode(parts[0] + '==').decode()
        payload = base64.urlsafe_b64decode(parts[1] + '==').decode()

        return jsonify({
            'header': header,
            'payload': payload,
            'note': 'JWT payload is base64-encoded, NOT encrypted. Signature prevents tampering.'
        })
    except Exception as e:
        return jsonify({'error': f'Failed to decode: {str(e)}'}), 400


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
