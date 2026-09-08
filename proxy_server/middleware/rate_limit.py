"""
Rate limiting middleware with API key support
"""
import time
from collections import defaultdict
from flask import current_app, request
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class TokenBucket:
    """Token bucket rate limiter"""
    
    def __init__(self, capacity, refill_rate):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()
    
    def consume(self, tokens=1):
        """Try to consume tokens"""
        now = time.time()
        
        # Refill tokens
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        
        # Check if we can consume
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

def get_rate_limiter():
    """Get or create rate limiter with current app config"""
    if not hasattr(get_rate_limiter, '_limiters'):
        get_rate_limiter._limiters = defaultdict(lambda: TokenBucket(
            current_app.config.get('RATE_LIMIT_REQUESTS', 100),
            current_app.config.get('RATE_LIMIT_REQUESTS', 100) / 
            current_app.config.get('RATE_LIMIT_WINDOW', 3600)
        ))
    return get_rate_limiter._limiters

def get_client_id(request):
    """Get unique client identifier"""
    # Import here to avoid circular import
    from proxy_server.middleware.auth import get_api_key_from_request, api_key_manager
    
    # Try to get API key first for better identification
    api_key = get_api_key_from_request(request)
    if api_key:
        key_info = api_key_manager.validate_api_key(api_key)
        if key_info:
            return f"api_key:{key_info['user_id']}"
    
    # Fall back to IP address
    return f"ip:{request.remote_addr}"

def is_exempt_route(path):
    """True if ``path`` sits under a RATE_LIMIT_EXEMPT_ROUTES prefix.

    Callers hand us the path in both shapes — the /ollama rule builds
    '/ollama/api/embed' while Werkzeug's catch-all yields 'api/embed' with no
    leading slash — so normalise before matching. Matching on the prefix plus
    a '/' boundary keeps '/ollama-admin' from inheriting '/ollama''s exemption.
    """
    if not path:
        return False

    normalised = path if path.startswith('/') else '/' + path
    for prefix in current_app.config.get('RATE_LIMIT_EXEMPT_ROUTES', ()):
        if normalised == prefix or normalised.startswith(prefix + '/'):
            return True
    return False

def check_rate_limit(request, path=None):
    """Check if request is within rate limits.

    ``path`` is optional so the rate_limit_middleware decorator below keeps
    working unchanged; a caller that knows which route it is serving should
    pass it, or the exemption list can never apply.
    """
    if not current_app.config.get('RATE_LIMIT_ENABLED', True):
        return True

    if is_exempt_route(path):
        return True

    client_id = get_client_id(request)
    rate_limiters = get_rate_limiter()
    bucket = rate_limiters[client_id]
    
    allowed = bucket.consume()
    if not allowed:
        logger.warning(f"Rate limit exceeded for client {client_id}")
    
    return allowed

def rate_limit_middleware(f):
    """Rate limiting decorator"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_rate_limit(request):
            return {'error': 'Rate limit exceeded'}, 429
        return f(*args, **kwargs)
    return decorated_function
