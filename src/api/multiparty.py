"""
Multi-Party API Endpoints.

Provides REST endpoints for party management, memory operations,
modification tracking, and feedback aggregation.
All endpoints enforce role-based access control.
"""

import json
import uuid
from datetime import datetime
from typing import Optional
from flask import Blueprint, request, jsonify, g
from functools import wraps

from src.database import get_db_connection
from src.memory_orchestrator import MemoryOrchestrator
from src.role_bootstrap import RoleBootstrap

multiparty_bp = Blueprint('multiparty', __name__, url_prefix='/api/v1')
memory_orch = MemoryOrchestrator()
bootstrap = RoleBootstrap()


def get_party_from_request():
    """Extract party identity from request headers or token."""
    # For MVP, use a simple X-Party-ID header
    # Future: JWT or public-key authentication
    party_id = request.headers.get('X-Party-ID')
    if not party_id:
        return None
    return party_id


def require_role(minimum_role: str):
    """Decorator to enforce minimum role level."""
    role_hierarchy = {
        'observer': 0,
        'user': 1,
        'contributor': 2,
        'admin': 3
    }

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            party_id = get_party_from_request()
            if not party_id:
                return jsonify({'error': 'Missing party identification'}), 401

            conn = get_db_connection()
            try:
                party = conn.execute(
                    'SELECT role FROM parties WHERE id = ?',
                    (party_id,)
                ).fetchone()
                if not party:
                    return jsonify({'error': 'Unknown party'}), 403

                if role_hierarchy.get(party['role'], -1) < role_hierarchy.get(minimum_role, 0):
                    return jsonify({'error': 'Insufficient permissions'}), 403
            finally:
                conn.close()

            g.party_id = party_id
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# --- Party Management ---

@multiparty_bp.route('/party/register', methods=['POST'])
@require_role('admin')
def register_party():
    """Register a new party (admin only)."""
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({'error': 'Party name is required'}), 400

    name = data['name']
    role = data.get('role', 'user')
    if role not in ('user', 'contributor', 'admin', 'observer'):
        return jsonify({'error': f'Invalid role: {role}'}), 400

    party_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    public_key = data.get('public_key')

    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO parties (id, name, role, created_at, public_key) VALUES (?, ?, ?, ?, ?)',
            (party_id, name, role, now, public_key)
        )
        conn.commit()
    except Exception as e:
        return jsonify({'error': f'Failed to create party: {str(e)}'}), 400
    finally:
        conn.close()

    return jsonify({'party_id': party_id, 'name': name, 'role': role}), 201


@multiparty_bp.route('/party/<party_id>', methods=['GET'])
@require_role('user')
def get_party(party_id):
    """Get party details and role."""
    conn = get_db_connection()
    try:
        party = conn.execute(
            'SELECT id, name, role, created_at FROM parties WHERE id = ?',
            (party_id,)
        ).fetchone()
        if not party:
            return jsonify({'error': 'Party not found'}), 404
        return jsonify(dict(party))
    finally:
        conn.close()


@multiparty_bp.route('/party/<party_id>/role', methods=['PUT'])
@require_role('admin')
def update_party_role(party_id):
    """Change a party's role (admin only)."""
    data = request.get_json()
    if not data or 'role' not in data:
        return jsonify({'error': 'Role is required'}), 400

    new_role = data['role']
    if new_role not in ('user', 'contributor', 'admin', 'observer'):
        return jsonify({'error': f'Invalid role: {new_role}'}), 400

    conn = get_db_connection()
    try:
        conn.execute(
            'UPDATE parties SET role = ? WHERE id = ?',
            (new_role, party_id)
        )
        conn.commit()
        return jsonify({'message': f'Party {party_id} role updated to {new_role}'})
    finally:
        conn.close()


# --- Memory Operations ---

@multiparty_bp.route('/memory', methods=['POST'])
@require_role('user')
def write_memory():
    """Write memory scoped to the requester's party."""
    data = request.get_json()
    if not data or 'key' not in data:
        return jsonify({'error': 'Memory key is required'}), 400

    key = data['key']
    value = data.get('value')
    namespace = data.get('namespace', 'global')

    memory_id = memory_orch.set_memory(g.party_id, key, value, namespace)
    return jsonify({'memory_id': memory_id}), 201


@multiparty_bp.route('/memory/<key>', methods=['GET'])
@require_role('user')
def read_memory(key):
    """Read memory scoped to the requester's party."""
    namespace = request.args.get('namespace', 'global')
    value = memory_orch.get_memory(g.party_id, key, namespace)
    if value is None:
        return jsonify({'error': 'Memory not found'}), 404
    return jsonify({'key': key, 'value': value, 'namespace': namespace})


# --- Modification Tracking ---

@multiparty_bp.route('/modification', methods=['POST'])
@require_role('contributor')
def initiate_modification():
    """Initiate a modification (contributor or admin)."""
    data = request.get_json()
    if not data or 'feature' not in data or 'diff' not in data:
        return jsonify({'error': 'Feature and diff are required'}), 400

    mod_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    change_type = data.get('change_type', 'modify')
    change_resource = data.get('change_resource', 'code')

    # Determine initial status
    status = 'pending'
    if change_type in ('self_source', 'self_config'):
        status = 'pending_self_review'

    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (mod_id, g.party_id, data['feature'], change_type, change_resource, data['diff'], status, now)
        )
        conn.commit()
    except Exception as e:
        return jsonify({'error': f'Failed to create modification: {str(e)}'}), 400
    finally:
        conn.close()

    return jsonify({'modification_id': mod_id, 'status': status}), 201


@multiparty_bp.route('/modification/<mod_id>/approve', methods=['PUT'])
@require_role('admin')
def approve_modification(mod_id):
    """Approve a modification for deployment (admin only)."""
    now = datetime.utcnow().isoformat()

    conn = get_db_connection()
    try:
        mod = conn.execute(
            'SELECT status, change_type FROM modifications WHERE id = ?',
            (mod_id,)
        ).fetchone()
        if not mod:
            return jsonify({'error': 'Modification not found'}), 404

        # Check if self-modification requires Critic deliberation
        if mod['change_type'] in ('self_source', 'self_config') and mod['status'] == 'pending_self_review':
            # In production, this would trigger the Critic agent
            # For MVP, we simulate the deliberation check
            return jsonify({
                'error': 'Self-modification requires Critic deliberation. '
                         'Please complete autonomous auditing before approval.',
                'status': 'pending_self_review'
            }), 400

        if mod['status'] not in ('pending', 'pending_self_review'):
            return jsonify({'error': f'Cannot approve modification in status: {mod["status"]}'}), 400

        conn.execute(
            'UPDATE modifications SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?',
            ('approved', g.party_id, now, mod_id)
        )
        conn.commit()
        return jsonify({'modification_id': mod_id, 'status': 'approved'})
    finally:
        conn.close()


@multiparty_bp.route('/modification/<mod_id>/deploy', methods=['PUT'])
@require_role('admin')
def deploy_modification(mod_id):
    """Deploy an approved modification (admin only)."""
    now = datetime.utcnow().isoformat()

    conn = get_db_connection()
    try:
        mod = conn.execute(
            'SELECT status FROM modifications WHERE id = ?',
            (mod_id,)
        ).fetchone()
        if not mod:
            return jsonify({'error': 'Modification not found'}), 404
        if mod['status'] != 'approved':
            return jsonify({'error': f'Cannot deploy modification in status: {mod["status"]}'}), 400

        conn.execute(
            'UPDATE modifications SET status = ?, deployed_at = ? WHERE id = ?',
            ('deployed', now, mod_id)
        )
        conn.commit()
        return jsonify({'modification_id': mod_id, 'status': 'deployed'})
    finally:
        conn.close()


@multiparty_bp.route('/modification/<mod_id>/rollback', methods=['PUT'])
@require_role('admin')
def rollback_modification(mod_id):
    """Rollback a deployed modification (admin only)."""
    now = datetime.utcnow().isoformat()

    conn = get_db_connection()
    try:
        mod = conn.execute(
            'SELECT status, diff, change_resource FROM modifications WHERE id = ?',
            (mod_id,)
        ).fetchone()
        if not mod:
            return jsonify({'error': 'Modification not found'}), 404
        if mod['status'] != 'deployed':
            return jsonify({'error': f'Cannot rollback modification in status: {mod["status"]}'}), 400

        # Create rollback record
        rollback_id = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO modifications (id, initiated_by, feature, change_type, change_resource, diff, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (rollback_id, g.party_id, 'rollback', 'rollback', mod['change_resource'], mod['diff'], 'rolled_back', now)
        )

        # Update original modification
        conn.execute(
            'UPDATE modifications SET status = ?, rolled_back_at = ? WHERE id = ?',
            ('rolled_back', now, mod_id)
        )
        conn.commit()
        return jsonify({'modification_id': mod_id, 'status': 'rolled_back', 'rollback_id': rollback_id})
    finally:
        conn.close()


# --- Feedback Aggregation ---

@multiparty_bp.route('/feedback/aggregate', methods=['GET'])
@require_role('user')
def get_feedback_aggregates():
    """Retrieve aggregated feature signals."""
    feature = request.args.get('feature')

    conn = get_db_connection()
    try:
        if feature:
            rows = conn.execute(
                'SELECT * FROM feedback_aggregates WHERE feature = ?',
                (feature,)
            ).fetchall()
        else:
            rows = conn.execute('SELECT * FROM feedback_aggregates').fetchall()
        return jsonify([dict(row) for row in rows])
    finally:
        conn.close()


# --- Bootstrap Status ---

@multiparty_bp.route('/bootstrap/status', methods=['GET'])
def bootstrap_status():
    """Check if bootstrap is required (no authentication needed)."""
    return jsonify(bootstrap.check_web_ui_bootstrap())
