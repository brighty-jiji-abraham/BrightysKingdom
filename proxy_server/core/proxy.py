"""
Core proxy functionality for HTTP and WebSocket requests
"""

import time
import requests
from flask import Response, current_app, request
from geventwebsocket.exceptions import WebSocketError
from proxy_server.middleware.rate_limit import check_rate_limit
from proxy_server.middleware.security import validate_request
from proxy_server.services.load_balancer import LoadBalancer
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class ProxyCore:
    def __init__(self) -> None:
        self.load_balancer = LoadBalancer()
        self.session = requests.Session()

    def forward_request(self, path: str):
        """Forward HTTP or WebSocket requests"""
        logger.info(f"=== PROXY REQUEST ===")
        logger.info(f"Method: {request.method}, Path: {path}")
        
        start = time.time()
        
        # Get target URL
        target = self.load_balancer.get_backend(path)
        if not target:
            return {"error": "Route not found", "path": path}, 404
            
        # Check for WebSocket upgrade
        if (request.environ.get('HTTP_UPGRADE', '').lower() == 'websocket' and
            'upgrade' in request.environ.get('HTTP_CONNECTION', '').lower() and
            'wsgi.websocket' in request.environ):
            
            # Get WebSocket object from environ
            ws = request.environ['wsgi.websocket']
            if not ws:
                logger.error("❌ WebSocket creation failed")
                return {"error": "WebSocket creation failed"}, 500
                
            # Handle Socket.IO WebSocket
            from proxy_server.core.websocket_tunnel import websocket_tunnel
            try:
                logger.info(f"🔌 Starting WebSocket tunnel for path: {path}")
                websocket_tunnel.handle_client_connection(ws, path)
                return ""
            except Exception as e:
                logger.error(f"❌ WebSocket tunnel error: {e}")
                return {"error": "WebSocket tunnel failed"}, 502
        
        # Validate regular HTTP request
        if not validate_request(request):
            return {"error": "Invalid request"}, 400
            
        if not check_rate_limit(request):
            return {"error": "Rate limit exceeded"}, 429
            
        # Handle regular HTTP request
        try:
            headers = {k: v for k, v in request.headers if k.lower() != "host"}
            
            resp = self.session.request(
                request.method, target,
                headers=headers, 
                params=request.args,
                data=request.get_data(), 
                cookies=request.cookies,
                allow_redirects=False, 
                timeout=current_app.config.get("REQUEST_TIMEOUT", 30)
            )

            out_headers = [
                (k, v) for k, v in resp.headers.items()
                if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}
            ]

            flask_resp = Response(resp.content, resp.status_code, out_headers)
            self._record_metrics(path, resp.status_code, time.time() - start)
            return flask_resp

        except Exception as e:
            logger.error(f"HTTP request failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Backend request failed: {e}"}, 502

    def _record_metrics(self, path: str, status: int, duration: float):
        """Record request metrics"""
        if hasattr(current_app, "monitoring_service"):
            current_app.monitoring_service.record_request(path, status, duration)

# Global proxy instance
proxy_core = ProxyCore()
