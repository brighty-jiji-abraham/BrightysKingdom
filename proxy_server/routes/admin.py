"""
Admin panel routes with API key management
"""
from flask import Blueprint, current_app, jsonify, request
from proxy_server.middleware.auth import optional_auth, admin_required, api_key_manager
from proxy_server.core.proxy import proxy_core
from proxy_server.services import config_store
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/health')
def admin_health():
    """Admin health endpoint"""
    return {
        'status': 'healthy',
        'component': 'admin-panel',
        'auth_method': 'api_key'
    }, 200

@admin_bp.route('/metrics')
@optional_auth
def get_metrics():
    """Get proxy metrics"""
    if hasattr(current_app, 'monitoring_service'):
        metrics = current_app.monitoring_service.get_metrics()
        return jsonify(metrics)
    return {'error': 'Monitoring service not available'}, 503

@admin_bp.route('/backends')
def get_backends():
    """Get backend service status"""
    if hasattr(current_app, 'health_service'):
        status = current_app.health_service.get_status()
        return jsonify(status)
    return {'error': 'Health service not available'}, 503

@admin_bp.route('/backends/summary')
def get_backends_summary():
    """Get backend health summary by route"""
    if hasattr(current_app, 'health_service'):
        summary = current_app.health_service.get_summary()
        return jsonify(summary)
    return {'error': 'Health service not available'}, 503

@admin_bp.route('/backends/<path:route>')
def get_route_backends(route):
    """Get all backend instances for a specific route"""
    if hasattr(current_app, 'health_service'):
        # Add leading slash if not present
        if not route.startswith('/'):
            route = '/' + route
            
        backends = current_app.health_service.get_route_backends(route)
        if backends:
            return jsonify({'route': route, 'backends': backends})
        else:
            return {'error': 'Route not found'}, 404
    return {'error': 'Health service not available'}, 503

@admin_bp.route('/load-balancer/stats')
@optional_auth
def get_load_balancer_stats():
    """Get load balancer statistics"""
    try:
        from proxy_server.core.proxy import proxy_core
        stats = proxy_core.load_balancer.get_backend_stats()
        return jsonify(stats)
    except Exception as e:
        return {'error': f'Failed to get load balancer stats: {str(e)}'}, 500

@admin_bp.route('/backends/add', methods=['POST'])
@admin_required
def add_backend():
    """Add a backend URL to a route, creating the route if it is new.

    Writes to MongoDB first, then refreshes the live config from it. This
    previously only mutated the in-memory dict, so a backend added through the
    admin panel disappeared on the next restart with nothing to explain why.
    """
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()
    url = (data.get('url') or '').strip()

    if not route or not url:
        return {'error': 'route and url are required'}, 400
    if not route.startswith('/'):
        return {'error': 'route must start with /'}, 400
    if not url.startswith(('http://', 'https://')):
        return {'error': 'url must start with http:// or https://'}, 400

    existing = current_app.config.get('BACKEND_ROUTES', {}).get(route)
    already = url == existing if isinstance(existing, str) else url in (existing or [])
    if already:
        return {'error': 'Backend URL already exists on this route'}, 400

    try:
        config_store.add_backend(
            route, url,
            name=data.get('name'),
            health_route=data.get('health_route'),
        )
        config_store.refresh_app(current_app._get_current_object())
    except config_store.ConfigStoreUnavailable as e:
        # Refusing beats a silent in-memory-only write: the caller would
        # otherwise believe the change was durable.
        logger.error(f"add_backend: config store unavailable: {e}")
        return {
            'error': 'Configuration database unavailable',
            'detail': str(e),
            'hint': 'Backends are stored in MongoDB - check PROXY_MONGO_URL',
        }, 503
    except Exception as e:
        logger.error(f"Error adding backend: {e}")
        return {'error': 'Failed to add backend', 'detail': str(e)}, 500

    return {
        'message': f'Backend {url} added to route {route}',
        'current_backends': current_app.config['BACKEND_ROUTES'].get(route),
        'persisted': True,
    }, 200


@admin_bp.route('/backends/remove', methods=['POST'])
@admin_required
def remove_backend():
    """Remove a backend URL. The route is dropped once its last URL goes."""
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()
    url = (data.get('url') or '').strip()

    if not route or not url:
        return {'error': 'route and url are required'}, 400

    backend_routes = current_app.config.get('BACKEND_ROUTES', {})
    if route not in backend_routes:
        return {'error': 'Route not found'}, 404

    current = backend_routes[route]
    known = [current] if isinstance(current, str) else list(current or [])
    if url not in known:
        return {'error': 'Backend URL not found in route'}, 404

    try:
        config_store.remove_backend(route, url)
        config_store.refresh_app(current_app._get_current_object())
    except config_store.ConfigStoreUnavailable as e:
        logger.error(f"remove_backend: config store unavailable: {e}")
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error removing backend: {e}")
        return {'error': 'Failed to remove backend', 'detail': str(e)}, 500

    remaining = current_app.config['BACKEND_ROUTES'].get(route)
    return {
        'message': (
            f'Backend {url} removed from route {route}' if remaining
            else f'Route {route} removed entirely - it had no backends left'
        ),
        'current_backends': remaining or [],
        'persisted': True,
    }, 200


@admin_bp.route('/backends/route', methods=['POST'])
@admin_required
def upsert_route():
    """Create or update a whole route: display name, health path, URL list."""
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()

    if not route or not route.startswith('/'):
        return {'error': 'route is required and must start with /'}, 400

    urls = data.get('urls')
    if urls is not None:
        if not isinstance(urls, list):
            return {'error': 'urls must be a list'}, 400
        urls = [u.strip() for u in urls if u and u.strip()]
        bad = [u for u in urls if not u.startswith(('http://', 'https://'))]
        if bad:
            return {'error': 'invalid url(s): ' + ', '.join(bad)}, 400

    try:
        config_store.upsert_route(
            route,
            name=data.get('name'),
            health_route=data.get('health_route'),
            urls=urls,
        )
        config_store.refresh_app(current_app._get_current_object())
    except config_store.ConfigStoreUnavailable as e:
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error upserting route: {e}")
        return {'error': 'Failed to save route', 'detail': str(e)}, 500

    return {
        'message': f'Route {route} saved',
        'current_backends': current_app.config['BACKEND_ROUTES'].get(route),
        'persisted': True,
    }, 200


@admin_bp.route('/backends/route/delete', methods=['POST'])
@admin_required
def delete_route():
    """Delete an entire route and all of its backends."""
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()
    if not route:
        return {'error': 'route is required'}, 400

    try:
        config_store.remove_route(route)
        config_store.refresh_app(current_app._get_current_object())
    except config_store.ConfigStoreUnavailable as e:
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error deleting route: {e}")
        return {'error': 'Failed to delete route', 'detail': str(e)}, 500

    return {'message': f'Route {route} deleted', 'persisted': True}, 200


@admin_bp.route('/backends/update', methods=['POST'])
@admin_required
def update_backend():
    """Change one backend URL in place, keeping its position on the route.

    Editing via remove-then-add would move the backend to the end of the list,
    quietly changing round-robin order, and would leave the route one backend
    short in between.
    """
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()
    old_url = (data.get('url') or '').strip()
    new_url = (data.get('new_url') or '').strip()

    if not route or not old_url or not new_url:
        return {'error': 'route, url and new_url are required'}, 400
    if not new_url.startswith(('http://', 'https://')):
        return {'error': 'new_url must start with http:// or https://'}, 400
    if old_url == new_url:
        return {'error': 'new_url is the same as the current url'}, 400

    try:
        updated = config_store.update_backend_url(route, old_url, new_url)
        if updated is None:
            return {'error': f'{old_url} is not a backend of {route}'}, 404
        config_store.refresh_app(current_app._get_current_object())
    except config_store.DuplicateBackend as e:
        return {'error': str(e)}, 400
    except config_store.ConfigStoreUnavailable as e:
        logger.error(f"update_backend: config store unavailable: {e}")
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error updating backend: {e}")
        return {'error': 'Failed to update backend', 'detail': str(e)}, 500

    return {
        'message': f'{old_url} changed to {new_url} on {route}',
        'current_backends': current_app.config['BACKEND_ROUTES'].get(route),
        'persisted': True,
    }, 200


@admin_bp.route('/backends/route/enabled', methods=['POST'])
@admin_required
def set_route_enabled():
    """Take a route in or out of rotation without losing its configuration."""
    data = request.get_json() or {}
    route = (data.get('route') or '').strip()
    if not route:
        return {'error': 'route is required'}, 400
    if 'enabled' not in data:
        return {'error': 'enabled is required'}, 400

    enabled = bool(data.get('enabled'))
    try:
        config_store.set_enabled(route, enabled)
        config_store.refresh_app(current_app._get_current_object())
    except config_store.ConfigStoreUnavailable as e:
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error toggling route: {e}")
        return {'error': 'Failed to update route', 'detail': str(e)}, 500

    return {
        'message': f'Route {route} {"enabled" if enabled else "disabled"}',
        'enabled': enabled,
        'persisted': True,
    }, 200


@admin_bp.route('/backends/store', methods=['GET'])
def list_store_routes():
    """Raw store documents, including disabled routes.

    /admin/backends reports live health and therefore only knows about routes
    the proxy is serving. The panel also has to show a disabled route in order
    to offer re-enabling it, which is why this exists alongside it.
    """
    try:
        return jsonify({'routes': config_store.list_routes(include_disabled=True),
                        'source': config_store.describe()})
    except config_store.ConfigStoreUnavailable as e:
        return {'error': 'Configuration database unavailable', 'detail': str(e)}, 503
    except Exception as e:
        logger.error(f"Error listing store routes: {e}")
        return {'error': 'Failed to list routes', 'detail': str(e)}, 500


@admin_bp.route('/config/source', methods=['GET'])
def config_source():
    """Whether routes came from MongoDB or from .env, and why."""
    return jsonify(config_store.describe())


@admin_bp.route('/config')
@admin_required
def get_config():
    """Get current configuration (sanitized)"""
    config = {
        'backend_routes': current_app.config.get('BACKEND_ROUTES', {}),
        'rate_limit_enabled': current_app.config.get('RATE_LIMIT_ENABLED', False),
        'rate_limit_requests': current_app.config.get('RATE_LIMIT_REQUESTS', 0),
        'request_timeout': current_app.config.get('REQUEST_TIMEOUT', 30),
        'load_balancer_strategy': current_app.config.get('LOAD_BALANCER_STRATEGY', 'round_robin'),
        'api_key_header': current_app.config.get('API_KEY_HEADER', 'X-API-Key'),
        'api_key_prefix': current_app.config.get('API_KEY_PREFIX', 'pk_')
    }
    return jsonify(config)

@admin_bp.route('/routes')
def list_routes():
    """List all available routes"""
    routes = {}
    for rule in current_app.url_map.iter_rules():
        if rule.endpoint != 'static':
            routes[rule.rule] = {
                'methods': list(rule.methods),
                'endpoint': rule.endpoint
            }
    return jsonify(routes)

# API Key Management Endpoints

@admin_bp.route('/api-keys', methods=['GET'])
@admin_required
def list_api_keys():
    """List all API keys"""
    keys = api_key_manager.list_api_keys()
    return jsonify({'api_keys': keys})

@admin_bp.route('/api-keys', methods=['POST'])
@admin_required
def create_api_key():
    """Create a new API key"""
    data = request.get_json() or {}
    
    user_id = data.get('user_id')
    name = data.get('name', 'Generated Key')
    
    if not user_id:
        return {'error': 'user_id is required'}, 400
    
    try:
        api_key = api_key_manager.generate_api_key(user_id, name)
        return {
            'api_key': api_key,
            'user_id': user_id,
            'name': name,
            'message': 'API key created successfully. Store it securely - it won\'t be shown again.'
        }, 201
    except Exception as e:
        logger.error(f"Error creating API key: {str(e)}")
        return {'error': 'Failed to create API key'}, 500

@admin_bp.route('/api-keys/revoke', methods=['POST'])
@admin_required
def revoke_api_key():
    """Revoke an API key"""
    data = request.get_json() or {}
    api_key = data.get('api_key')
    
    if not api_key:
        return {'error': 'api_key is required'}, 400
    
    if api_key_manager.revoke_api_key(api_key):
        return {'message': 'API key revoked successfully'}, 200
    else:
        return {'error': 'API key not found'}, 404

@admin_bp.route('/api-keys/validate', methods=['POST'])
def validate_api_key():
    """Validate an API key"""
    data = request.get_json() or {}
    api_key = data.get('api_key')
    
    if not api_key:
        return {'error': 'api_key is required'}, 400
    
    key_info = api_key_manager.validate_api_key(api_key)
    if key_info:
        return {
            'valid': True,
            'user_id': key_info['user_id'],
            'name': key_info['name'],
            'created_at': key_info['created_at'],
            'last_used': key_info['last_used']
        }, 200
    else:
        return {'valid': False}, 200
    
@admin_bp.route('/websocket-sessions', methods=['GET'])
def websocket_sessions():
    """Get WebSocket session statistics.

    Previously imported `dual_connection_proxy` from `websocket_proxy`, a
    module that does not exist in this codebase - the endpoint raised
    ImportError on every call. The real objects are in `websocket_tunnel`.
    """
    try:
        from proxy_server.core.websocket_tunnel import (
            flask_socketio_proxy,
            websocket_tunnel,
        )

        stats = dict(flask_socketio_proxy.get_stats())
        stats['backend_connections'] = len(
            getattr(websocket_tunnel, 'backend_connections', {}) or {}
        )
        stats['sticky_sessions'] = len(
            getattr(proxy_core.load_balancer, 'socketio_sessions', {}) or {}
        )
        return jsonify({'status': 'success', 'data': stats})
    except Exception as e:
        logger.error(f"websocket-sessions failed: {e}")
        return {'status': 'error', 'error': str(e)}, 500


@admin_bp.route('/tunnel/requests', methods=['GET'])
def tunnel_requests():
    """What has actually crossed the tunnel, plus a per-minute traffic series.

    Held in memory by the tunnel client, so it resets when the proxy restarts.
    The durable record is the proxy log; this exists so the panel can show
    activity without anyone reading log files.
    """
    try:
        from proxy_server.core.tunnel_client import get_activity

        limit = request.args.get('limit', 100)
        return jsonify({'status': 'success', 'data': get_activity(limit)})
    except ValueError:
        return {'error': 'limit must be a number'}, 400
    except Exception as e:
        logger.error(f"tunnel requests read failed: {e}")
        return {'error': 'Failed to read tunnel activity', 'detail': str(e)}, 500


@admin_bp.route('/tunnel/config', methods=['GET'])
@admin_required
def get_tunnel_config():
    """Tunnel settings as stored, with the token withheld.

    Master-key gated even though it is a read: it reports where this machine
    publishes itself, and whether a credential is present.
    """
    try:
        return jsonify({'status': 'success', 'data': config_store.describe_tunnel()})
    except Exception as e:
        logger.error(f"tunnel config read failed: {e}")
        return {'error': 'Failed to read tunnel settings', 'detail': str(e)}, 500


@admin_bp.route('/tunnel/config', methods=['POST'])
@admin_required
def set_tunnel_config():
    """Write tunnel settings, then re-dial so they take effect immediately.

    Fields left out are untouched. Sending an empty string clears a field, so
    the .env value takes over again - that is how you hand the token back to
    the host without deleting the document.
    """
    data = request.get_json() or {}

    url = data.get('server_url')
    if url is not None and url.strip() and not url.strip().startswith(('ws://', 'wss://')):
        return {'error': 'server_url must start with ws:// or wss://'}, 400

    target = data.get('local_target')
    if target is not None and target.strip() and not target.strip().startswith(('http://', 'https://')):
        return {'error': 'local_target must start with http:// or https://'}, 400

    try:
        config_store.save_tunnel(
            server_url=url,
            token=data.get('token'),
            client_name=data.get('client_name'),
            local_target=target,
        )
    except config_store.ConfigStoreUnavailable as e:
        logger.error(f"tunnel config write failed: {e}")
        return {
            'error': 'Configuration database unavailable',
            'detail': str(e),
            'hint': 'Tunnel settings are stored in MongoDB - check PROXY_MONGO_URL',
        }, 503
    except Exception as e:
        logger.error(f"tunnel config write failed: {e}")
        return {'error': 'Failed to save tunnel settings', 'detail': str(e)}, 500

    # Closing the socket is enough: run_forever re-resolves settings on every
    # pass, so the next reconnect uses the new values.
    redialled = False
    try:
        from proxy_server.core.tunnel_client import reconnect

        redialled = reconnect()
    except Exception as e:
        logger.warning(f"tunnel re-dial failed: {e}")

    return {
        'message': 'Tunnel settings saved',
        'reconnecting': redialled,
        'data': config_store.describe_tunnel(),
    }, 200


@admin_bp.route('/tunnel', methods=['GET'])
def tunnel_status():
    """Outbound tunnel state, as seen from this side.

    The tunnel head's own /_tunnel/* endpoints live on a private port on the
    public server and are firewalled off from the admin UI's browser. The
    client running inside this process knows its own health just as well, so
    the UI reads it from here.
    """
    try:
        from proxy_server.core.tunnel_client import get_status

        return jsonify({'status': 'success', 'data': get_status()})
    except Exception as e:
        logger.error(f"tunnel status failed: {e}")
        return {'status': 'error', 'error': str(e)}, 500
