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
        """Forward HTTP or WebSocket requests with proper multipart handling"""
        logger.info(f"=== PROXY REQUEST ===")
        logger.info(f"Method: {request.method}, Path: {path}")
        logger.info(f"Content-Type: {request.content_type}")
        
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
            # Check if this is a multipart request
            is_multipart = (request.content_type and 
                        'multipart/form-data' in request.content_type)
            
            if is_multipart:
                logger.info("🔄 Forwarding multipart request with raw streaming")
                return self._forward_multipart_raw(target, start, path)
            else:
                logger.info("📤 Forwarding standard request")
                return self._forward_standard_request(target, start, path)
                
        except Exception as e:
            logger.error(f"HTTP request failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Backend request failed: {str(e)}"}, 502


    def _forward_multipart_raw(self, target: str, start: float, path: str):
        """Forward multipart requests by preserving the original raw data and headers"""
        try:
            # Prepare headers - preserve Content-Type exactly as received
            headers = {}
            for k, v in request.headers:
                k_lower = k.lower()
                # Skip problematic headers but KEEP Content-Type for multipart
                if k_lower not in {"host", "content-encoding", "transfer-encoding", "connection"}:
                    headers[k] = v
            
            # Get content length
            content_length = request.environ.get('CONTENT_LENGTH')
            if content_length:
                content_length = int(content_length)
                
            logger.info(f"Multipart Content-Type: {headers.get('Content-Type', 'Not found')}")
            logger.info(f"Content-Length: {content_length}")
            
            # For multipart data, stream the raw request body to preserve boundaries
            if content_length and content_length > 100 * 1024 * 1024:  # 100MB+
                # Large file streaming
                def generate_chunks():
                    chunk_size = 64 * 1024  # 64KB chunks
                    while True:
                        chunk = request.stream.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
                
                resp = self.session.request(
                    request.method,
                    target,
                    headers=headers,
                    params=request.args,
                    data=generate_chunks(),
                    cookies=request.cookies,
                    allow_redirects=False,
                    timeout=current_app.config.get("LARGE_FILE_TIMEOUT", 1800),
                    stream=True
                )
                
                return self._create_streaming_response(resp, start, path)
            else:
                # Standard multipart handling - read raw data
                raw_data = request.get_data()
                
                resp = self.session.request(
                    request.method,
                    target,
                    headers=headers,
                    params=request.args,
                    data=raw_data,  # Use raw data, not files parameter
                    cookies=request.cookies,
                    allow_redirects=False,
                    timeout=current_app.config.get("REQUEST_TIMEOUT", 300),
                    stream=False
                )
                
                return self._create_response(resp, start, path)
                
        except Exception as e:
            logger.error(f"Multipart forwarding failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Multipart forwarding failed: {str(e)}"}, 502


    def _forward_standard_request(self, target: str, start: float, path: str):
        """Forward non-multipart requests"""
        try:
            # Prepare headers
            headers = {}
            for k, v in request.headers:
                k_lower = k.lower()
                if k_lower not in {"host", "content-encoding", "transfer-encoding", "connection"}:
                    headers[k] = v
            
            # Handle different request types
            if request.method in ['POST', 'PUT', 'PATCH']:
                # For JSON or other data
                data = request.get_data()
            else:
                data = None
                
            resp = self.session.request(
                request.method,
                target,
                headers=headers,
                params=request.args,
                data=data,
                cookies=request.cookies,
                allow_redirects=False,
                timeout=current_app.config.get("REQUEST_TIMEOUT", 300),
                stream=False
            )
            
            return self._create_response(resp, start, path)
            
        except Exception as e:
            logger.error(f"Standard request failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Request failed: {str(e)}"}, 502


    def _create_response(self, resp, start: float, path: str):
        """Create Flask response from requests response"""
        try:
            out_headers = []
            for k, v in resp.headers.items():
                k_lower = k.lower()
                if k_lower not in {"content-encoding", "transfer-encoding", "connection"}:
                    out_headers.append((k, v))
            
            flask_resp = Response(resp.content, resp.status_code, out_headers)
            self._record_metrics(path, resp.status_code, time.time() - start)
            
            logger.info(f"✅ Request completed: {resp.status_code}, Size: {len(resp.content)} bytes")
            return flask_resp
            
        except Exception as e:
            logger.error(f"Response creation failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Response creation failed: {str(e)}"}, 502


    def _create_streaming_response(self, resp, start: float, path: str):
        """Create streaming Flask response"""
        try:
            out_headers = []
            for k, v in resp.headers.items():
                k_lower = k.lower()
                if k_lower not in {"content-encoding", "transfer-encoding", "connection"}:
                    out_headers.append((k, v))
            
            def generate():
                try:
                    for chunk in resp.iter_content(chunk_size=64*1024):
                        if chunk:
                            yield chunk
                except Exception as e:
                    logger.error(f"Streaming response error: {e}")
                finally:
                    resp.close()
            
            flask_resp = Response(generate(), resp.status_code, out_headers)
            self._record_metrics(path, resp.status_code, time.time() - start)
            
            logger.info(f"✅ Streaming request completed: {resp.status_code}")
            return flask_resp
            
        except Exception as e:
            logger.error(f"Streaming response creation failed: {e}")
            self._record_metrics(path, 500, time.time() - start)
            return {"error": f"Streaming response failed: {str(e)}"}, 502


    def _record_metrics(self, path: str, status: int, duration: float):
        """Record request metrics"""
        if hasattr(current_app, "monitoring_service"):
            current_app.monitoring_service.record_request(path, status, duration)


# Global proxy instance
proxy_core = ProxyCore()
