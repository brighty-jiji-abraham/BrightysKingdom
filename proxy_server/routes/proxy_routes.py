"""
Fresh Proxy Routes with Flask Socket.IO Support

This module provides clean routing for the Flask Socket.IO proxy implementation.
It integrates with the existing Flask app structure while providing a fresh
Socket.IO proxy experience with comprehensive debugging and error handling.
"""

from flask import Blueprint, request, current_app, Response
from proxy_server.middleware.security import security_headers
from proxy_server.middleware.auth import optional_auth, require_auth
from proxy_server.utils.logger import get_logger
from proxy_server.core.proxy import proxy_core
import time
import json
import traceback

logger = get_logger(__name__)

proxy_bp = Blueprint('proxy', __name__)

# ============================================================================
# Fresh Socket.IO Routes for Flask Backends (COMPREHENSIVE DEBUG VERSION)
# ============================================================================

@proxy_bp.route("/<app>/socket.io/", defaults={"path": ""}, methods=["GET", "POST", "OPTIONS"])
@proxy_bp.route("/<app>/socket.io/<path:path>", methods=["GET", "POST", "OPTIONS"])
def fresh_socketio_proxy(app: str, path: str):
    """
    Fresh Socket.IO proxy route with comprehensive debugging and error handling
    
    This route handles both HTTP polling and WebSocket upgrade requests
    for Socket.IO connections, providing a clean proxy between clients
    and Flask Socket.IO backend servers.
    
    URL Format: /app1/socket.io/?EIO=4&transport=websocket
    Where 'app1' routes to your Flask Socket.IO backend at http://127.0.0.1:3000
    """
    
    logger.info(f"🔥 === FRESH SOCKET.IO DEBUG START ===")
    logger.info(f"🔥 App: {app}, Path: '{path}'")
    logger.info(f"🔥 Method: {request.method}")
    logger.info(f"🔥 URL: {request.url}")
    logger.info(f"🔥 Transport: {request.args.get('transport', 'unknown')}")
    logger.info(f"🔥 Query String: {request.query_string.decode()}")
    logger.info(f"🔥 Headers: {dict(request.headers)}")
    
    try:
        # Check backend routes first
        backend_routes = current_app.config.get("BACKEND_ROUTES", {})
        app_route = f"/{app}"
        
        logger.info(f"🔥 Looking for route: {app_route}")
        logger.info(f"🔥 Available routes: {list(backend_routes.keys())}")
        logger.info(f"🔥 Route exists: {app_route in backend_routes}")
        
        if app_route not in backend_routes:
            logger.error(f"❌ ROUTE NOT FOUND - RETURNING 404")
            return {"error": f"Unknown app: {app}"}, 404
        
        logger.info(f"✅ Route found: {backend_routes[app_route]}")
        
        # Check WebSocket upgrade
        is_websocket = (
            request.environ.get('HTTP_UPGRADE', '').lower() == 'websocket' and
            'upgrade' in request.environ.get('HTTP_CONNECTION', '').lower()
        )
        
        logger.info(f"🔥 Is WebSocket: {is_websocket}")
        logger.info(f"🔥 HTTP_UPGRADE: {request.environ.get('HTTP_UPGRADE')}")
        logger.info(f"🔥 HTTP_CONNECTION: {request.environ.get('HTTP_CONNECTION')}")
        
        if is_websocket:
            logger.info(f"🔥 WebSocket upgrade detected")
            
            # Check wsgi.websocket
            websocket = request.environ.get('wsgi.websocket')
            logger.info(f"🔥 wsgi.websocket exists: {websocket is not None}")
            logger.info(f"🔥 wsgi.websocket type: {type(websocket)}")
            
            # Log all WebSocket-related environ keys
            ws_keys = [k for k in request.environ.keys() if 'websocket' in k.lower() or 'ws' in k.lower()]
            logger.info(f"🔥 WebSocket environ keys: {ws_keys}")
            for key in ws_keys:
                logger.info(f"🔥   {key}: {request.environ[key]}")
            
            if not websocket:
                logger.error(f"❌ NO WEBSOCKET OBJECT - RETURNING 400")
                logger.error(f"❌ Available environ keys: {[k for k in request.environ.keys() if 'websocket' in k.lower()]}")
                logger.error(f"❌ Full environ keys: {list(request.environ.keys())}")
                return {"error": "WebSocket upgrade failed - no wsgi.websocket object"}, 400
            
            # Check WebSocket headers
            ws_key = request.headers.get('Sec-WebSocket-Key')
            ws_version = request.headers.get('Sec-WebSocket-Version')
            ws_extensions = request.headers.get('Sec-WebSocket-Extensions', '')
            
            logger.info(f"🔥 WebSocket Key: {ws_key}")
            logger.info(f"🔥 WebSocket Version: {ws_version}")
            logger.info(f"🔥 WebSocket Extensions: {ws_extensions}")
            
            if not ws_key:
                logger.error(f"❌ MISSING WEBSOCKET KEY - RETURNING 400")
                return {"error": "Missing WebSocket key"}, 400
                
            if ws_version != '13':
                logger.error(f"❌ INVALID WEBSOCKET VERSION - RETURNING 400")
                return {"error": "Invalid WebSocket version"}, 400
            
            logger.info(f"🔥 WebSocket validation passed")
            
            # Try to handle WebSocket
            try:
                logger.info(f"🔥 Attempting WebSocket handling...")
                result = _handle_websocket_connection_fixed(websocket, app, path)
                logger.info(f"🔥 WebSocket handling result: {type(result)}")
                return result
                
            except Exception as e:
                logger.error(f"❌ WEBSOCKET HANDLING ERROR: {e}")
                logger.error(f"❌ Stack trace: {traceback.format_exc()}")
                return {"error": f"WebSocket handling failed: {e}"}, 502
        
        else:
            logger.info(f"🔥 HTTP polling request")
            try:
                result = _handle_http_polling_fixed(app, path)
                logger.info(f"🔥 HTTP polling result: {type(result)}")
                return result
                
            except Exception as e:
                logger.error(f"❌ HTTP POLLING ERROR: {e}")
                logger.error(f"❌ Stack trace: {traceback.format_exc()}")
                return {"error": f"HTTP polling failed: {e}"}, 502
    
    except Exception as e:
        logger.error(f"❌ ROUTE HANDLER ERROR: {e}")
        logger.error(f"❌ Stack trace: {traceback.format_exc()}")
        return {"error": f"Route handler failed: {e}"}, 500
    
    finally:
        logger.info(f"🔥 === FRESH SOCKET.IO DEBUG END ===")


def _handle_websocket_connection_fixed(websocket, app: str, path: str):
    """
    Fixed WebSocket connection handler using existing websocket_tunnel
    """
    logger.info(f"🔌 Setting up WebSocket for {app}")
    
    try:
        # Import the existing websocket tunnel
        from proxy_server.core.websocket_tunnel import websocket_tunnel, calculate_accept
        
        # Validate WebSocket key and create accept header
        ws_key = request.headers.get('Sec-WebSocket-Key')
        if not ws_key:
            logger.error("❌ Missing Sec-WebSocket-Key")
            return {"error": "Missing WebSocket key"}, 400
            
        ws_accept = calculate_accept(ws_key)
        logger.info(f"🔌 Calculated WebSocket accept: {ws_accept}")
        
        # Build full path for tunnel
        full_path = f"/{app}/socket.io/{path}" if path else f"/{app}/socket.io/"
        if request.query_string:
            full_path += "?" + request.query_string.decode()
        
        logger.info(f"🔌 Starting WebSocket tunnel for: {full_path}")
        
        # Create proper WebSocket upgrade response
        response = Response("", status=101)
        response.headers.update({
            'Upgrade': 'websocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Accept': ws_accept,
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Credentials': 'true'
        })
        
        # Handle WebSocket extensions carefully to avoid compression issues
        ws_extensions = request.headers.get('Sec-WebSocket-Extensions', '')
        if 'permessage-deflate' in ws_extensions:
            # Skip compression to avoid gevent-websocket compression issues
            logger.info("🔌 Client requested permessage-deflate - skipping to avoid compression issues")
            # Don't add Sec-WebSocket-Extensions header to avoid compression
        
        logger.info(f"🔌 WebSocket response headers: {dict(response.headers)}")
        
        # Start the WebSocket tunnel using existing infrastructure
        try:
            # Use existing websocket tunnel - this will handle the bidirectional communication
            websocket_tunnel.handle_client_connection(websocket, full_path)
            logger.info(f"✅ WebSocket tunnel started successfully")
            return response
            
        except Exception as tunnel_error:
            logger.error(f"❌ WebSocket tunnel failed: {tunnel_error}")
            logger.error(f"❌ Tunnel stack trace: {traceback.format_exc()}")
            try:
                websocket.close()
            except:
                pass
            return {"error": f"WebSocket tunnel failed: {tunnel_error}"}, 502
        
    except Exception as e:
        logger.error(f"❌ WebSocket setup failed: {e}")
        logger.error(f"❌ Setup stack trace: {traceback.format_exc()}")
        try:
            websocket.close()
        except:
            pass
        return {"error": f"WebSocket setup failed: {e}"}, 502


def _handle_http_polling_fixed(app: str, path: str):
    """
    Fixed HTTP polling handler using existing proxy infrastructure
    """
    logger.info(f"📡 HTTP polling for {app}")
    
    try:
        # Build the full path for the proxy
        full_path = f"/{app}/socket.io/{path}" if path else f"/{app}/socket.io/"
        logger.info(f"📡 Forwarding to path: {full_path}")
        
        # Use existing proxy core which handles load balancing, health checks, etc.
        response = proxy_core.forward_request(full_path)
        logger.info(f"📡 Proxy core response: {type(response)}")
        
        # Add CORS headers if response is a Flask Response object
        if hasattr(response, 'headers'):
            response.headers.update({
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization"
            })
        
        logger.info(f"✅ HTTP polling completed successfully")
        return response
        
    except Exception as e:
        logger.error(f"❌ HTTP polling failed: {e}")
        logger.error(f"❌ Polling stack trace: {traceback.format_exc()}")
        return {"error": f"HTTP polling failed: {e}"}, 502


# ============================================================================
# Proxy Statistics and Monitoring (ENHANCED)
# ============================================================================

@proxy_bp.route('/proxy-stats')
def proxy_stats():
    """Get Fresh Socket.IO proxy statistics"""
    try:
        # Get stats from existing components
        stats = {
            "proxy_type": "fresh_flask_socketio_proxy",
            "version": "3.1.0",
            "status": "running",
            "timestamp": int(time.time())
        }
        
        # Try to get load balancer stats
        try:
            lb_stats = proxy_core.load_balancer.get_backend_stats()
            stats["load_balancer"] = lb_stats
        except Exception as e:
            stats["load_balancer_error"] = str(e)
        
        # Try to get health service stats
        try:
            if hasattr(current_app, 'health_service'):
                health_stats = current_app.health_service.get_summary()
                stats["backend_health"] = health_stats
        except Exception as e:
            stats["health_service_error"] = str(e)
        
        # Try to get monitoring stats
        try:
            if hasattr(current_app, 'monitoring_service'):
                monitoring_stats = current_app.monitoring_service.get_metrics()
                stats["monitoring"] = monitoring_stats
        except Exception as e:
            stats["monitoring_error"] = str(e)
        
        # Get WebSocket tunnel stats
        try:
            from proxy_server.core.websocket_tunnel import websocket_tunnel
            if hasattr(websocket_tunnel, 'backend_connections'):
                stats["websocket_connections"] = len(websocket_tunnel.backend_connections)
        except Exception as e:
            stats["websocket_error"] = str(e)
        
        return {
            "status": "success",
            "statistics": stats
        }
        
    except Exception as e:
        logger.error(f"❌ Error getting proxy stats: {e}")
        return {"error": "Failed to get stats"}, 500


# ============================================================================
# WebSocket Connection Debugging Endpoint
# ============================================================================

@proxy_bp.route('/debug-websocket')
def debug_websocket():
    """Debug WebSocket configuration and environment"""
    try:
        debug_info = {
            "gevent_websocket_available": False,
            "wsgi_websocket_support": False,
            "backend_routes": current_app.config.get("BACKEND_ROUTES", {}),
            "websocket_config": {
                "cors_enabled": current_app.config.get("SOCKETIO_CORS_ENABLED", False),
                "cors_origins": current_app.config.get("SOCKETIO_CORS_ORIGINS", "*"),
                "backend_timeout": current_app.config.get("WS_BACKEND_CONNECT_TIMEOUT", 30)
            }
        }
        
        # Check gevent-websocket availability
        try:
            from geventwebsocket.websocket import WebSocket
            debug_info["gevent_websocket_available"] = True
        except ImportError:
            pass
        
        # Check if WebSocket object would be available
        if 'wsgi.websocket' in request.environ:
            debug_info["wsgi_websocket_support"] = True
        
        return debug_info
        
    except Exception as e:
        return {"error": f"Debug failed: {e}"}, 500


# ============================================================================
# Existing Routes (maintained for compatibility)
# ============================================================================

@proxy_bp.route('/<app>/webhooks/<webhook_service>/webhook/<user>', 
                methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
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
    try:
        # Check if WebSocket support is working
        websocket_support = False
        try:
            from proxy_server.core.websocket_tunnel import websocket_tunnel
            websocket_support = True
        except:
            pass
        
        return {
            'status': 'healthy',
            'service': 'fresh-flask-socketio-proxy',
            'version': '3.1.0',
            'websocket_support': websocket_support,
            'features': [
                'fresh_socketio_proxy', 
                'flask_backend_support',
                'load_balancing',
                'sticky_sessions',
                'websocket_support',
                'comprehensive_debugging',
                'error_recovery'
            ]
        }, 200
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }, 500


@proxy_bp.route('/')
def root():
    """Root endpoint"""
    try:
        # Get basic server info
        server_info = {
            'message': 'Fresh Flask Socket.IO Proxy Server',
            'version': '3.1.0',
            'status': 'running',
            'endpoints': [
                '/<app>/socket.io/* - Fresh Socket.IO proxy for Flask backends',
                '/proxy-stats - Get proxy statistics',
                '/debug-websocket - WebSocket debugging info',
                '/health - Health check',
                '/<app>/webhooks/* - Webhook proxy',
                '/<path:path> - Generic proxy (requires auth)'
            ]
        }
        
        # Try to add backend routes info
        try:
            backend_routes = current_app.config.get("BACKEND_ROUTES", {})
            server_info['configured_routes'] = list(backend_routes.keys())
        except:
            server_info['configured_routes'] = "unavailable"
        
        # Add WebSocket status
        try:
            from proxy_server.core.websocket_tunnel import websocket_tunnel
            server_info['websocket_tunnel_available'] = True
        except:
            server_info['websocket_tunnel_available'] = False
        
        return server_info, 200
        
    except Exception as e:
        logger.error(f"❌ Root endpoint error: {e}")
        return {
            'message': 'Fresh Flask Socket.IO Proxy Server',
            'version': '3.1.0',
            'status': 'error',
            'error': str(e)
        }, 500


# ============================================================================
# Enhanced Error Handlers for Better Debugging
# ============================================================================

@proxy_bp.errorhandler(400)
def handle_400_error(error):
    """Debug 400 errors specifically for Socket.IO"""
    logger.error(f"🚨 400 ERROR in fresh proxy routes!")
    logger.error(f"🚨 Error: {error}")
    logger.error(f"🚨 Request: {request.method} {request.url}")
    logger.error(f"🚨 Path: {request.path}")
    logger.error(f"🚨 Headers: {dict(request.headers)}")
    logger.error(f"🚨 Is WebSocket: {request.headers.get('Upgrade', '').lower() == 'websocket'}")
    logger.error(f"🚨 Stack trace: {traceback.format_exc()}")
    
    return {
        "error": "Bad Request in Fresh Socket.IO Proxy", 
        "debug": "Check logs for detailed error information",
        "request_path": request.path,
        "request_method": request.method,
        "is_websocket": request.headers.get('Upgrade', '').lower() == 'websocket'
    }, 400


@proxy_bp.errorhandler(500)
def handle_500_error(error):
    """Debug 500 errors"""
    logger.error(f"🚨 500 ERROR in fresh proxy routes!")
    logger.error(f"🚨 Error: {error}")
    logger.error(f"🚨 Stack trace: {traceback.format_exc()}")
    
    return {
        "error": "Internal Server Error in Fresh Socket.IO Proxy", 
        "debug": "Check logs for detailed error information"
    }, 500


@proxy_bp.errorhandler(502)
def handle_502_error(error):
    """Debug 502 errors (Backend issues)"""
    logger.error(f"🚨 502 ERROR in fresh proxy routes!")
    logger.error(f"🚨 Error: {error}")
    logger.error(f"🚨 Stack trace: {traceback.format_exc()}")
    
    return {
        "error": "Bad Gateway - Backend connection failed", 
        "debug": "Check backend server availability"
    }, 502
