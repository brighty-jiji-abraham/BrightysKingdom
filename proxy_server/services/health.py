"""
Health monitoring service with multi-backend support
"""
import threading
import time
import requests
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class HealthService:
    """Service for monitoring backend health with load balancing support"""
    
    def __init__(self, config):
        self.config = config
        self.backend_status = {}
        self.monitoring_thread = None
        self.stop_event = threading.Event()
        
        # Create session with default timeouts
        self.session = requests.Session()
        
        # Initialize backend status for all backends
        self._initialize_backend_status()
    
    def _initialize_backend_status(self):
        """Initialize status tracking for all backends"""
        backend_routes = self.config.get('BACKEND_ROUTES', {})
        health_routes = self.config.get('BACKEND_HEALTH_ROUTES', {})
        route_names = self.config.get('BACKEND_ROUTE_NAMES', {})
        
        for route, backends in backend_routes.items():
            custom_health_route = health_routes.get(route)
            route_name = route_names.get(route, route.replace('/', '').replace('_', ' ').title())
            
            if isinstance(backends, str):
                # Single backend
                backend_key = f"{route}:0"
                self.backend_status[backend_key] = {
                    'route': route,
                    'name': f"{route_name} (Primary)",
                    'url': backends,
                    'custom_health_route': custom_health_route,
                    'status': 'unknown',
                    'last_check': None,
                    'response_time': None,
                    'response_data': None,
                    'status_code': None,
                    'error': None,
                    'consecutive_failures': 0,
                    'consecutive_successes': 0
                }
            elif isinstance(backends, list):
                # Multiple backends
                for i, backend_url in enumerate(backends):
                    backend_key = f"{route}:{i}"
                    self.backend_status[backend_key] = {
                        'route': route,
                        'name': f"{route_name} (Instance {i+1})",
                        'url': backend_url,
                        'custom_health_route': custom_health_route,
                        'status': 'unknown',
                        'last_check': None,
                        'response_time': None,
                        'response_data': None,
                        'status_code': None,
                        'error': None,
                        'consecutive_failures': 0,
                        'consecutive_successes': 0
                    }
    
    def start_monitoring(self):
        """Start background health monitoring"""
        if not self.config.get('BACKEND_HEALTH_CHECK', True):
            logger.info("Backend health monitoring is disabled")
            return
        
        self.monitoring_thread = threading.Thread(target=self._monitor_loop)
        self.monitoring_thread.daemon = True
        self.monitoring_thread.start()
        logger.info("Health monitoring started for all backend instances")
    
    def stop_monitoring(self):
        """Stop background health monitoring"""
        if self.monitoring_thread:
            self.stop_event.set()
            self.monitoring_thread.join()
            logger.info("Health monitoring stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        interval = self.config.get('HEALTH_CHECK_INTERVAL', 30)
        
        while not self.stop_event.wait(interval):
            self._check_all_backends()
    
    def _check_all_backends(self):
        """Check health of all backend instances"""
        for backend_key in self.backend_status:
            self._check_backend(backend_key)
    
    def _check_backend(self, backend_key):
        """Check health of a specific backend instance"""
        backend_info = self.backend_status[backend_key]
        url = backend_info['url']
        route = backend_info['route']
        
        # Get timeout settings
        connect_timeout = self.config.get('HEALTH_CHECK_CONNECT_TIMEOUT', 5)
        read_timeout = self.config.get('HEALTH_CHECK_READ_TIMEOUT', 30)
        timeout = (connect_timeout, read_timeout)
        
        # Determine which endpoints to try
        if backend_info['custom_health_route']:
            health_endpoints = [
                backend_info['custom_health_route'],
                '/health',
                '/check-health',
                '/status',
                '/ping'
            ]
        else:
            health_endpoints = ['/health', '/check-health', '/status', '/ping', '/']
        
        start_time = time.time()
        last_error = None
        
        for endpoint in health_endpoints:
            try:
                response = self.session.get(
                    f"{url}{endpoint}",
                    timeout=timeout,
                    headers={'User-Agent': 'ProxyHealthCheck/1.0'}
                )
                
                response_time = (time.time() - start_time) * 1000
                
                # Try to parse JSON response safely
                response_data = None
                try:
                    if response.headers.get('content-type', '').startswith('application/json'):
                        response_data = response.json()
                except (ValueError, TypeError):
                    pass
                
                # Consider healthy if status code is 2xx or 3xx
                if 200 <= response.status_code < 400:
                    self._mark_backend_success(backend_key, response_time, response_data, response.status_code, endpoint)
                    return  # Success, exit early
                else:
                    last_error = f"HTTP {response.status_code} from {endpoint}"
                    
            except requests.exceptions.ConnectTimeout:
                last_error = f"Connection timeout ({connect_timeout}s) to {endpoint}"
                
            except requests.exceptions.ReadTimeout:
                last_error = f"Read timeout ({read_timeout}s) from {endpoint}"
                
            except requests.exceptions.ConnectionError as e:
                last_error = f"Connection error to {endpoint}: {str(e)}"
                
            except Exception as e:
                last_error = f"Unexpected error on {endpoint}: {str(e)}"
        
        # If we get here, all endpoints failed
        self._mark_backend_failure(backend_key, last_error)
    
    def _mark_backend_success(self, backend_key, response_time, response_data, status_code, endpoint):
        """Mark a backend check as successful"""
        backend_info = self.backend_status[backend_key]
        
        backend_info.update({
            'status': 'healthy',
            'last_check': time.time(),
            'response_time': response_time,
            'response_data': response_data,
            'status_code': status_code,
            'error': None,
            'endpoint_used': endpoint,
            'consecutive_failures': 0,
            'consecutive_successes': backend_info.get('consecutive_successes', 0) + 1
        })
        
        # Notify load balancer if this was previously unhealthy
        if hasattr(self, '_load_balancer_ref'):
            self._load_balancer_ref.mark_backend_healthy(backend_info['route'], backend_info['url'])
        
        logger.debug(f"Backend {backend_info['name']} is healthy - {status_code} from {endpoint}")
    
    def _mark_backend_failure(self, backend_key, error):
        """Mark a backend check as failed"""
        backend_info = self.backend_status[backend_key]
        
        consecutive_failures = backend_info.get('consecutive_failures', 0) + 1
        
        backend_info.update({
            'status': 'unhealthy',
            'last_check': time.time(),
            'response_time': None,
            'response_data': None,
            'status_code': None,
            'error': error,
            'endpoint_used': None,
            'consecutive_failures': consecutive_failures,
            'consecutive_successes': 0
        })
        
        # Notify load balancer if threshold reached
        unhealthy_threshold = self.config.get('UNHEALTHY_THRESHOLD', 3)
        if consecutive_failures >= unhealthy_threshold and hasattr(self, '_load_balancer_ref'):
            self._load_balancer_ref.mark_backend_unhealthy(backend_info['route'], backend_info['url'])
        
        logger.warning(f"Backend {backend_info['name']} health check failed ({consecutive_failures} consecutive): {error}")
    
    def set_load_balancer_ref(self, load_balancer):
        """Set reference to load balancer for health notifications"""
        self._load_balancer_ref = load_balancer
    
    def get_status(self):
        """Get current status of all backend instances"""
        return dict(self.backend_status)
    
    def get_summary(self):
        """Get a summary of backend health by route"""
        summary = {}
        
        for backend_key, info in self.backend_status.items():
            route = info['route']
            if route not in summary:
                summary[route] = {
                    'route': route,
                    'name': info['name'].split(' (')[0],  # Remove instance suffix
                    'total_instances': 0,
                    'healthy_instances': 0,
                    'unhealthy_instances': 0,
                    'status': 'unknown'
                }
            
            summary[route]['total_instances'] += 1
            if info['status'] == 'healthy':
                summary[route]['healthy_instances'] += 1
            else:
                summary[route]['unhealthy_instances'] += 1
        
        # Determine overall status for each route
        for route_summary in summary.values():
            if route_summary['healthy_instances'] == route_summary['total_instances']:
                route_summary['status'] = 'healthy'
            elif route_summary['healthy_instances'] > 0:
                route_summary['status'] = 'degraded'
            else:
                route_summary['status'] = 'unhealthy'
        
        return summary
    
    def is_backend_healthy(self, route):
        """Check if at least one backend instance is healthy for a route"""
        for backend_key, info in self.backend_status.items():
            if info['route'] == route and info['status'] == 'healthy':
                return True
        return False
    
    def get_route_backends(self, route):
        """Get all backend instances for a specific route"""
        backends = []
        for backend_key, info in self.backend_status.items():
            if info['route'] == route:
                backends.append(info)
        return backends
    
    def set_load_balancer_ref(self, load_balancer):
        """Set reference to load balancer for health notifications"""
        self._load_balancer_ref = load_balancer
        logger.info("Load balancer reference set in health service")
