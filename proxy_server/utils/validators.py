"""
Request validation utilities
"""
import re
from urllib.parse import urlparse

def is_valid_url(url):
    """Validate URL format"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def is_safe_path(path):
    """Check if path is safe (no traversal attacks)"""
    # Check for path traversal patterns
    dangerous_patterns = [
        '../', '..\\', './',
        '%2e%2e%2f', '%2e%2e\\',
        '%2e%2e%5c', '%252e%252e%252f'
    ]
    
    path_lower = path.lower()
    for pattern in dangerous_patterns:
        if pattern in path_lower:
            return False
    
    return True

def validate_json_payload(data, required_fields=None):
    """Validate JSON payload"""
    if not isinstance(data, dict):
        return False, "Payload must be a JSON object"
    
    if required_fields:
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return False, f"Missing required fields: {missing_fields}"
    
    return True, "Valid"

def sanitize_header_value(value):
    """Sanitize header value"""
    # Remove potentially dangerous characters
    if isinstance(value, str):
        # Remove control characters and normalize whitespace
        sanitized = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', value)
        sanitized = ' '.join(sanitized.split())
        return sanitized
    return value
