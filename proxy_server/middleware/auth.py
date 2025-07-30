"""
API Key authentication middleware
"""
import secrets
import string
import hashlib
import time
from flask import request, current_app
from functools import wraps
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

class APIKeyManager:
    """Manage API keys in memory (you can extend this to use a database)"""
    
    def __init__(self):
        # In-memory storage for API keys
        # Format: {api_key_hash: {'user_id': str, 'name': str, 'created_at': float, 'last_used': float}}
        self.api_keys = {}
        self._initialized = False
    
    def initialize_with_app(self, app):
        """Initialize with Flask app context"""
        if self._initialized:
            return
            
        with app.app_context():
            # Add master API key
            master_key = app.config.get('MASTER_API_KEY')
            if master_key:
                self.add_api_key(master_key, 'master', 'Master API Key')
            
            self._initialized = True
            logger.info("API Key Manager initialized with Flask app")
    
    def generate_api_key(self, user_id, name="Generated Key"):
        """Generate a new API key"""
        prefix = current_app.config.get('API_KEY_PREFIX', 'pk_')
        key_length = current_app.config.get('API_KEY_LENGTH', 32)
        
        # Generate random key
        alphabet = string.ascii_letters + string.digits
        random_part = ''.join(secrets.choice(alphabet) for _ in range(key_length))
        api_key = f"{prefix}{random_part}"
        
        # Store hashed version
        self.add_api_key(api_key, user_id, name)
        
        return api_key
    
    def add_api_key(self, api_key, user_id, name):
        """Add API key to storage"""
        key_hash = self._hash_key(api_key)
        self.api_keys[key_hash] = {
            'api_key': api_key,
            'user_id': user_id,
            'name': name,
            'created_at': time.time(),
            'last_used': None
        }
        logger.info(f"API key added for user {user_id}: {name}")
    
    def validate_api_key(self, api_key):
        """Validate an API key and return user info"""
        if not api_key:
            return None
        
        key_hash = self._hash_key(api_key)
        key_info = self.api_keys.get(key_hash)
        
        if key_info:
            # Update last used timestamp
            key_info['last_used'] = time.time()
            logger.debug(f"Valid API key used by user {key_info['user_id']}")
            return key_info
        
        logger.warning(f"Invalid API key attempted")
        return None
    
    def revoke_api_key(self, api_key):
        """Revoke an API key"""
        key_hash = self._hash_key(api_key)
        if key_hash in self.api_keys:
            user_id = self.api_keys[key_hash]['user_id']
            del self.api_keys[key_hash]
            logger.info(f"API key revoked for user {user_id}")
            return True
        return False
    
    def list_api_keys(self):
        """List all API keys (without the actual keys)"""
        return [
            {
                'user_id': info['user_id'],
                'name': info['name'],
                'key': info['api_key'],
                'created_at': info['created_at'],
                'last_used': info['last_used']
            }
            for info in self.api_keys.values()
        ]
    
    def _hash_key(self, api_key):
        """Hash an API key for storage"""
        return hashlib.sha256(api_key.encode()).hexdigest()

# Global API key manager (will be initialized later)
api_key_manager = APIKeyManager()

def get_api_key_from_request(request):
    """Extract API key from request"""
    # Use a default header name if current_app is not available
    try:
        header_name = current_app.config.get('API_KEY_HEADER', 'X-API-Key')
    except RuntimeError:
        header_name = 'X-API-Key'
    
    # Check header
    api_key = request.headers.get(header_name)
    if api_key:
        return api_key
    
    # Check query parameter
    api_key = request.args.get('api_key')
    if api_key:
        return api_key
    
    # Check Authorization header (Bearer format)
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        return auth_header.split(' ')[1]
    
    return None

def require_auth(f):
    """Decorator to require API key authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = get_api_key_from_request(request)
        
        if not api_key:
            header_name = current_app.config.get('API_KEY_HEADER', 'X-API-Key')
            return {
                'error': 'API key required',
                'message': f'Provide API key in {header_name} header'
            }, 401
        
        # Validate API key
        key_info = api_key_manager.validate_api_key(api_key)
        if not key_info:
            return {'error': 'Invalid API key'}, 401
        
        # Add user info to request
        request.user_id = key_info['user_id']
        request.api_key_name = key_info['name']
        
        return f(*args, **kwargs)
    
    return decorated_function

def optional_auth(f):
    """Decorator for optional API key authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = get_api_key_from_request(request)
        request.user_id = None
        request.api_key_name = None
        
        if api_key:
            key_info = api_key_manager.validate_api_key(api_key)
            if key_info:
                request.user_id = key_info['user_id']
                request.api_key_name = key_info['name']
        
        return f(*args, **kwargs)
    
    return decorated_function

def admin_required(f):
    """Decorator to require admin/master API key"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = get_api_key_from_request(request)
        
        if not api_key:
            return {'error': 'Admin API key required'}, 401
        
        key_info = api_key_manager.validate_api_key(api_key)
        if not key_info or key_info['user_id'] != 'master':
            return {'error': 'Admin privileges required'}, 403
        
        request.user_id = key_info['user_id']
        request.api_key_name = key_info['name']
        
        return f(*args, **kwargs)
    
    return decorated_function
