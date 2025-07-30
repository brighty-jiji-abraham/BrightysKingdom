"""
Monitoring and metrics service
"""
import time
from collections import defaultdict, deque
from threading import Lock
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class MonitoringService:
    """Service for collecting and managing metrics"""
    
    def __init__(self):
        self.metrics = {
            'requests_total': 0,
            'requests_by_status': defaultdict(int),
            'requests_by_path': defaultdict(int),
            'response_times': deque(maxlen=1000),
            'errors_total': 0,
            'uptime_start': time.time()
        }
        self.lock = Lock()
    
    def record_request(self, path, status_code, response_time):
        """Record a request metric"""
        with self.lock:
            self.metrics['requests_total'] += 1
            self.metrics['requests_by_status'][status_code] += 1
            self.metrics['requests_by_path'][path] += 1
            self.metrics['response_times'].append(response_time)
            
            if status_code >= 400:
                self.metrics['errors_total'] += 1
    
    def get_metrics(self):
        """Get current metrics snapshot"""
        with self.lock:
            uptime = time.time() - self.metrics['uptime_start']
            
            response_times = list(self.metrics['response_times'])
            avg_response_time = sum(response_times) / len(response_times) if response_times else 0
            
            return {
                'uptime_seconds': uptime,
                'requests_total': self.metrics['requests_total'],
                'errors_total': self.metrics['errors_total'],
                'error_rate': (self.metrics['errors_total'] / max(1, self.metrics['requests_total'])) * 100,
                'avg_response_time_ms': avg_response_time * 1000,
                'requests_per_minute': (self.metrics['requests_total'] / (uptime / 60)) if uptime > 0 else 0,
                'status_codes': dict(self.metrics['requests_by_status']),
                'popular_paths': dict(sorted(
                    self.metrics['requests_by_path'].items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:10])
            }
    
    def reset_metrics(self):
        """Reset all metrics"""
        with self.lock:
            self.metrics = {
                'requests_total': 0,
                'requests_by_status': defaultdict(int),
                'requests_by_path': defaultdict(int),
                'response_times': deque(maxlen=1000),
                'errors_total': 0,
                'uptime_start': time.time()
            }
        
        logger.info("Metrics reset")
