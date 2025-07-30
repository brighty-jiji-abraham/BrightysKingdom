"""
Main proxy routes with full Socket.IO support
"""

from flask import Blueprint, request, current_app, Response
from proxy_server.core.proxy import proxy_core
from proxy_server.middleware.security import security_headers
from proxy_server.middleware.auth import optional_auth, require_auth
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)
proxy_bp = Blueprint('proxy', __name__)

# ============================================================================
# Socket.IO Routes with Enhanced Debug Logging
# ============================================================================

@proxy_bp.route('/<app>/socket.io/', defaults={'path': ''}, methods=['GET', 'POST', 'OPTIONS'])
@proxy_bp.route('/<app>/socket.io/<path:path>', methods=['GET', 'POST', 'OPTIONS'])
def proxy_socketio_dynamic(app, path=''):
    """Enhanced Socket.IO routing with full debug logging"""
    logger.debug(f"=== SOCKET.IO ROUTING DEBUG ===")
    logger.debug(f"App parameter: {app}")
    logger.debug(f"Path parameter: {path}")
    logger.debug(f"Full URL: {request.url}")
    logger.debug(f"Method: {request.method}")
    logger.debug(f"Query params: {dict(request.args)}")
    logger.debug(f"Is WebSocket: {request.headers.get('Upgrade', '').lower() == 'websocket'}")

    # Verify app routing
    backend_routes = current_app.config.get('BACKEND_ROUTES', {})
    app_route = f'/{app}'
    
    logger.debug(f"Looking for route: {app_route}")
    logger.debug(f"Route exists: {app_route in backend_routes}")
    logger.debug(f"Route value: {backend_routes.get(app_route, 'NOT FOUND')}")

    if app_route not in backend_routes:
        logger.error(f"Route {app_route} not found in backend routes")
        return {'error': f'Unknown app: {app}'}, 404

    # Mark WebSocket upgrade for load balancer
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        request.is_websocket_upgrade = True
        logger.debug("Marked as WebSocket upgrade request")

    # Set app context for load balancer
    request.socketio_app = app
    logger.debug(f"Set request.socketio_app = {app}")

    # Build full path
    full_path = f"/{app}/socket.io/{path}" if path else f"/{app}/socket.io/"
    logger.debug(f"Forwarding path: {full_path}")

    # Forward to proxy core
    response = proxy_core.forward_request(full_path)
    logger.debug(f"Response type: {type(response)}")
    
    return response


# ============================================================================
# Webhook Routes 
# ============================================================================

@proxy_bp.route('/<app>/webhooks/<webhook_service>/webhook/<user>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
def proxy_webhooks_request(app, webhook_service, user):
    """Webhook proxy endpoint"""
    if user and not user.startswith('/'):
        path = '/' + app + '/' + webhook_service + '/webhook/' + user
    elif not user or not app or not webhook_service:
        path = '/'
    
    logger.info(f"Proxying webhook {request.method} {path}")
    response = proxy_core.forward_request(path)
    
    if hasattr(response, 'headers'):
        response = security_headers(response)
    
    return response

# ============================================================================
# Generic Proxy Routes
# ============================================================================

@proxy_bp.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
@require_auth
def proxy_request(path):
    """Main proxy endpoint"""
    if path and not path.startswith('/'):
        path = '/' + path
    elif not path:
        path = '/'
    
    logger.info(f"Proxying {request.method} {path}")
    response = proxy_core.forward_request(path)
    
    if hasattr(response, 'headers'):
        response = security_headers(response)
    
    return response

# ============================================================================
# Health Check
# ============================================================================

@proxy_bp.route('/health')
def proxy_health():
    """Proxy health check endpoint"""
    return {
        'status': 'healthy',
        'service': 'flask-reverse-proxy',
        'version': '2.0.0',
        'features': ['http_proxy', 'websocket_proxy', 'socketio_support', 'load_balancing']
    }, 200

# ============================================================================
# Root Route
# ============================================================================

@proxy_bp.route('/')
def root():
    """Root endpoint"""
    return {
        'message': 'Flask Reverse Proxy Server',
        'version': '2.0.0',
        'endpoints': [
            '/<app>/socket.io/* - Socket.IO proxy',
            '/<app>/webhooks/* - Webhook proxy', 
            '/<path:path> - Generic proxy',
            '/health - Health check'
        ]
    }, 200
