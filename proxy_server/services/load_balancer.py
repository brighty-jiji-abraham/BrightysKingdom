"""
Load balancing service with Socket.IO sticky sessions for dual-connection WebSocket
"""

import random
import time
import hashlib
from itertools import cycle
from flask import current_app, request
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class LoadBalancer:
    """Load balancer with health-aware routing and Socket.IO sticky sessions"""

    def __init__(self):
        self.round_robin_counters = {}
        self.connection_counts = {}
        self.backend_health = {}
        self.failed_backends = {}
        # Socket.IO sticky session mapping: session_id -> backend_url
        self.socketio_sessions = {}

    def get_backend(self, path):
        """Get backend URL with load balancing support and Socket.IO sticky sessions"""
        
        # Handle Socket.IO routes with sticky sessions
        if '/socket.io/' in path or path.endswith('/socket.io'):
            return self._handle_socketio_route(path)

        # Handle webhook routes
        if '/webhooks/' in path:
            return self._handle_webhook_route(path)

        # Find matching route
        route_prefix = self._find_route_prefix(path)
        if not route_prefix:
            return None

        backend_config = current_app.config.get('BACKEND_ROUTES', {})
        backends = backend_config.get(route_prefix)

        if not backends:
            return None

        # Handle single backend vs multiple backends
        if isinstance(backends, str):
            base_url = backends
        elif isinstance(backends, list) and len(backends) > 1:
            healthy_backends = self._get_healthy_backends(route_prefix, backends)
            if not healthy_backends:
                logger.warning(f"No healthy backends for route {route_prefix}")
                return None
            base_url = self.get_backend_with_strategy(route_prefix, healthy_backends)
        elif isinstance(backends, list) and len(backends) == 1:
            base_url = backends[0]
        else:
            return None

        # Remove prefix from path
        remaining_path = path[len(route_prefix):]
        if remaining_path and not remaining_path.startswith('/'):
            remaining_path = '/' + remaining_path

        final_url = base_url + remaining_path
        self.track_connection(base_url, increment=True)
        
        logger.debug(f"Load balancer selected {base_url} for {path}")
        return final_url

    def _handle_socketio_route(self, path):
        """Enhanced Socket.IO routing with sticky sessions"""
        logger.debug(f"Socket.IO routing for path: {path}")

        # Get session ID for sticky session routing
        session_id = request.args.get('sid')
        
        # Determine app route from path or request context
        app_route = None
        if hasattr(request, 'socketio_app'):
            app_route = f'/{request.socketio_app}'
        else:
            app_route = self._determine_app_route(path)

        if not app_route:
            logger.error("Could not determine app route for Socket.IO")
            return None

        backend_config = current_app.config['BACKEND_ROUTES']
        backends = backend_config.get(app_route)

        if not backends:
            logger.error(f"No backends found for {app_route}")
            return None

        # Check for existing sticky session
        if session_id and session_id in self.socketio_sessions:
            backend_url = self.socketio_sessions[session_id]
            logger.debug(f"Using sticky session {session_id[:8]}... -> {backend_url}")
            
            # Strip app prefix for Socket.IO path
            socketio_path = self._strip_app_prefix_from_socketio_path(path, app_route)
            return backend_url + socketio_path

        # New session - choose backend
        if isinstance(backends, list) and len(backends) > 1:
            healthy_backends = self._get_healthy_backends(app_route, backends)
            if healthy_backends:
                base_url = self.get_backend_with_strategy(app_route, healthy_backends)
            else:
                base_url = backends[0]  # fallback
        elif isinstance(backends, list):
            base_url = backends[0]
        else:
            base_url = backends

        # Store sticky session mapping if we have a session ID
        if session_id:
            self.socketio_sessions[session_id] = base_url
            logger.debug(f"Created sticky session {session_id[:8]}... -> {base_url}")
        else:
            # Generate session ID for new connections without one
            client_identifier = f"{request.remote_addr}:{request.headers.get('User-Agent', '')}"
            generated_sid = hashlib.md5(client_identifier.encode()).hexdigest()
            self.socketio_sessions[generated_sid] = base_url
            logger.debug(f"Generated sticky session {generated_sid[:8]}... -> {base_url}")

        # Strip app prefix for Socket.IO path
        socketio_path = self._strip_app_prefix_from_socketio_path(path, app_route)
        final_url = base_url + socketio_path
        
        logger.debug(f"Returning Socket.IO URL: {final_url}")
        return final_url

    def _determine_app_route(self, path):
        """Determine app route from Socket.IO path"""
        parts = path.split('/')
        if len(parts) >= 2 and parts[1]:
            return f'/{parts[1]}'
        return None

    def _strip_app_prefix_from_socketio_path(self, full_path, app_route):
        """Strip app prefix from Socket.IO path"""
        if full_path.startswith(app_route):
            stripped_path = full_path[len(app_route):]
            if not stripped_path.startswith('/'):
                stripped_path = '/' + stripped_path
            logger.debug(f"Stripped Socket.IO path: {full_path} -> {stripped_path}")
            return stripped_path
        return full_path

    def _handle_webhook_route(self, path):
        """Handle webhook routing with load balancing"""
        parts = path.split('/')
        if len(parts) >= 2 and parts[1]:
            app = parts[1]
            app_route = f'/{app}'
            backend_config = current_app.config.get('BACKEND_ROUTES', {})
            backends = backend_config.get(app_route)

            if backends:
                if isinstance(backends, list) and len(backends) > 1:
                    healthy_backends = self._get_healthy_backends(app_route, backends)
                    if healthy_backends:
                        base_url = self.get_backend_with_strategy(app_route, healthy_backends)
                    else:
                        base_url = backends[0]
                elif isinstance(backends, list):
                    base_url = backends[0]
                else:
                    base_url = backends

                logger.info(f"Routing webhook {path} to {app} ({base_url})")
                return base_url + path

        return None

    def _get_healthy_backends(self, route_prefix, backends):
        """Get list of healthy backends for a route"""
        healthy = []
        current_time = time.time()

        for backend in backends:
            backend_key = f"{route_prefix}:{backend}"
            
            if backend_key in self.failed_backends:
                failed_time = self.failed_backends[backend_key]
                recovery_window = 30
                if current_time - failed_time < recovery_window:
                    continue
                else:
                    del self.failed_backends[backend_key]

            health_status = self.backend_health.get(backend_key, 'unknown')
            if health_status == 'healthy' or health_status == 'unknown':
                healthy.append(backend)

        if not healthy:
            logger.warning(f"No healthy backends for {route_prefix}, allowing all")
            return backends

        return healthy

    def _find_route_prefix(self, path):
        """Find the longest matching route prefix"""
        backend_routes = current_app.config.get('BACKEND_ROUTES', {})
        matches = [prefix for prefix in backend_routes.keys() if path.startswith(prefix)]
        
        if not matches:
            return None
        
        return max(matches, key=len)

    def get_backend_with_strategy(self, route_prefix, backends):
        """Get backend using configured load balancing strategy"""
        strategy = current_app.config.get('LOAD_BALANCER_STRATEGY', 'round_robin')
        
        if strategy == 'round_robin':
            return self._round_robin(route_prefix, backends)
        elif strategy == 'random':
            return self._random(backends)
        elif strategy == 'least_connections':
            return self._least_connections(backends)
        else:
            return self._round_robin(route_prefix, backends)

    def _round_robin(self, route_prefix, backends):
        """Round robin selection"""
        if route_prefix not in self.round_robin_counters:
            self.round_robin_counters[route_prefix] = cycle(backends)
        
        try:
            return next(self.round_robin_counters[route_prefix])
        except StopIteration:
            self.round_robin_counters[route_prefix] = cycle(backends)
            return next(self.round_robin_counters[route_prefix])

    def _random(self, backends):
        """Random selection"""
        return random.choice(backends)

    def _least_connections(self, backends):
        """Least connections selection"""
        return min(backends, key=lambda b: self.connection_counts.get(b, 0))

    def track_connection(self, backend, increment=True):
        """Track connection count for backend"""
        if backend not in self.connection_counts:
            self.connection_counts[backend] = 0
        
        if increment:
            self.connection_counts[backend] += 1
        else:
            self.connection_counts[backend] = max(0, self.connection_counts[backend] - 1)

    def mark_backend_unhealthy(self, route_prefix, backend_url):
        """Mark a backend as unhealthy"""
        backend_key = f"{route_prefix}:{backend_url}"
        self.backend_health[backend_key] = 'unhealthy'
        self.failed_backends[backend_key] = time.time()
        logger.warning(f"Marked backend {backend_url} as unhealthy for route {route_prefix}")

    def mark_backend_healthy(self, route_prefix, backend_url):
        """Mark a backend as healthy"""
        backend_key = f"{route_prefix}:{backend_url}"
        self.backend_health[backend_key] = 'healthy'
        if backend_key in self.failed_backends:
            del self.failed_backends[backend_key]
        logger.info(f"Marked backend {backend_url} as healthy for route {route_prefix}")

    def get_backend_stats(self):
        """Get current backend statistics"""
        return {
            'connection_counts': dict(self.connection_counts),
            'backend_health': dict(self.backend_health),
            'failed_backends': dict(self.failed_backends),
            'socketio_sessions': len(self.socketio_sessions),
            'round_robin_state': {k: str(v) for k, v in self.round_robin_counters.items()}
        }

    def cleanup_expired_sessions(self, max_age_hours=24):
        """Clean up expired Socket.IO sessions"""
        if len(self.socketio_sessions) > 10000:
            logger.info("Clearing Socket.IO session cache due to size limit")
            self.socketio_sessions.clear()
