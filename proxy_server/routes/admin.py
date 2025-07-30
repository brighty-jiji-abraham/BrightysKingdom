"""
Admin panel routes with API key management
"""
from flask import Blueprint, current_app, jsonify, request
from proxy_server.middleware.auth import optional_auth, admin_required, api_key_manager
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
    """Add a new backend URL to a route"""
    data = request.get_json() or {}
    route = data.get('route')  # e.g., '/app1'
    url = data.get('url')      # e.g., 'http://localhost:3003'
    
    if not route or not url:
        return {'error': 'route and url are required'}, 400
    
    try:
        # Update in-memory configuration
        backend_routes = current_app.config.get('BACKEND_ROUTES', {})
        
        if route in backend_routes:
            current_backends = backend_routes[route]
            if isinstance(current_backends, str):
                # Convert single URL to list
                backend_routes[route] = [current_backends, url]
            elif isinstance(current_backends, list):
                # Add to existing list
                if url not in current_backends:
                    backend_routes[route].append(url)
                else:
                    return {'error': 'Backend URL already exists'}, 400
        else:
            # New route
            backend_routes[route] = url
        
        # Reinitialize health monitoring for new backend
        if hasattr(current_app, 'health_service'):
            current_app.health_service._initialize_backend_status()
        
        return {'message': f'Backend {url} added to route {route}', 'current_backends': backend_routes[route]}, 200
        
    except Exception as e:
        logger.error(f"Error adding backend: {str(e)}")
        return {'error': 'Failed to add backend'}, 500

@admin_bp.route('/backends/remove', methods=['POST'])
@admin_required
def remove_backend():
    """Remove a backend URL from a route"""
    data = request.get_json() or {}
    route = data.get('route')
    url = data.get('url')
    
    if not route or not url:
        return {'error': 'route and url are required'}, 400
    
    try:
        backend_routes = current_app.config.get('BACKEND_ROUTES', {})
        
        if route not in backend_routes:
            return {'error': 'Route not found'}, 404
        
        current_backends = backend_routes[route]
        
        if isinstance(current_backends, list) and url in current_backends:
            current_backends.remove(url)
            if len(current_backends) == 1:
                # Convert back to single URL
                backend_routes[route] = current_backends[0]
            elif len(current_backends) == 0:
                # Remove route entirely
                del backend_routes[route]
            
            # Reinitialize health monitoring
            if hasattr(current_app, 'health_service'):
                current_app.health_service._initialize_backend_status()
            
            return {'message': f'Backend {url} removed from route {route}', 'current_backends': backend_routes.get(route, [])}, 200
        elif isinstance(current_backends, str) and current_backends == url:
            del backend_routes[route]
            return {'message': f'Route {route} removed entirely'}, 200
        
        return {'error': 'Backend URL not found in route'}, 404
        
    except Exception as e:
        logger.error(f"Error removing backend: {str(e)}")
        return {'error': 'Failed to remove backend'}, 500

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
    """Get WebSocket session statistics"""
    from proxy_server.core.websocket_proxy import dual_connection_proxy
    
    stats = dual_connection_proxy.get_session_stats()
    return jsonify({
        'status': 'success',
        'data': stats
    })
