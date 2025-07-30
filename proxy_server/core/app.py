"""
Flask application factory with debug middleware
"""

from flask import Flask, request, g
from flask_cors import CORS
from proxy_server.config.settings import Config
from proxy_server.routes.proxy_routes import proxy_bp
from proxy_server.routes.admin import admin_bp
from proxy_server.services.monitoring import MonitoringService
from proxy_server.services.health import HealthService
from proxy_server.middleware.auth import api_key_manager
from proxy_server.utils.logger import get_logger
import atexit
import time

logger = get_logger(__name__)

def create_app(config_class=Config):
    """Create and configure Flask application with debug middleware"""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize CORS with more permissive settings for debugging
    CORS(app, 
         origins=['*'],
         allow_headers=['*'],
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
         supports_credentials=True)

    # Add debug middleware
    @app.before_request
    def before_request():
        """Log every incoming request"""
        g.start_time = time.time()
        
        # Log detailed request information
        logger.info('🔍' + '='*50)
        logger.info('🔍 INCOMING REQUEST DEBUG')
        logger.info('🔍' + '='*50)
        logger.info(f'🔍 Method: {request.method}')
        logger.info(f'🔍 URL: {request.url}')
        logger.info(f'🔍 Path: {request.path}')
        logger.info(f'🔍 Remote Address: {request.remote_addr}')
        logger.info(f'🔍 User Agent: {request.headers.get("User-Agent", "Unknown")}')
        logger.info(f'🔍 Headers: {dict(request.headers)}')
        
        # Check for WebSocket upgrade
        is_websocket = (
            request.headers.get('Upgrade', '').lower() == 'websocket' and
            'upgrade' in request.headers.get('Connection', '').lower()
        )
        logger.info(f'🔍 Is WebSocket Upgrade: {is_websocket}')
        
        # Check for Socket.IO specific patterns
        is_socketio = '/socket.io/' in request.path or request.path.endswith('/socket.io')
        logger.info(f'🔍 Is Socket.IO Request: {is_socketio}')
        
        if request.query_string:
            logger.info(f'🔍 Query String: {request.query_string.decode()}')
        
        logger.info('🔍' + '='*50)

    @app.after_request
    def after_request(response):
        """Log response information"""
        duration = time.time() - g.get('start_time', time.time())
        
        logger.info('🔍 RESPONSE DEBUG')
        logger.info(f'🔍 Status: {response.status_code}')
        logger.info(f'🔍 Duration: {duration:.3f}s')
        logger.info(f'🔍 Content-Type: {response.headers.get("Content-Type", "Unknown")}')
        logger.info('🔍' + '='*50)
        
        return response

    # Initialize API key manager
    api_key_manager.initialize_with_app(app)

    # Register blueprints
    app.register_blueprint(proxy_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # Initialize services
    monitoring_service = MonitoringService()
    health_service = HealthService(app.config)

    # Store services in app context
    app.monitoring_service = monitoring_service
    app.health_service = health_service

    # Link health service with load balancer
    with app.app_context():
        from proxy_server.core.proxy import proxy_core
        health_service.set_load_balancer_ref(proxy_core.load_balancer)

    # Start background services
    health_service.start_monitoring()

    # Register cleanup
    atexit.register(cleanup_services, health_service)

    logger.info("Flask application created with debug middleware and enhanced CORS")
    return app

def cleanup_services(health_service):
    """Cleanup background services"""
    health_service.stop_monitoring()
    logger.info("Background services stopped")
