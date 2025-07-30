"""
Security middleware
"""
import re
from flask import request, current_app
from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

# Security patterns
SUSPICIOUS_PATTERNS = [
    r'\.\./',           # Path traversal
    r'<script',         # XSS attempts
    r'union\s+select',  # SQL injection
    r'cmd\.exe',        # Command injection
    r'/etc/passwd',     # File access
]

def validate_request(request):
    """Validate incoming request for security issues"""
    
    # Check content length
    max_length = current_app.config.get('MAX_CONTENT_LENGTH', 16777216)
    if request.content_length and request.content_length > max_length:
        logger.warning(f"Request too large: {request.content_length} bytes")
        return False
    
    # Check for suspicious patterns in URL
    full_path = request.full_path
    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, full_path, re.IGNORECASE):
            logger.warning(f"Suspicious pattern detected in URL: {pattern}")
            return False
    
    # Check headers for suspicious content
    for header_name, header_value in request.headers:
        if header_name.lower() in ['user-agent', 'referer']:
            for pattern in SUSPICIOUS_PATTERNS:
                if re.search(pattern, header_value, re.IGNORECASE):
                    logger.warning(f"Suspicious pattern in {header_name}: {pattern}")
                    return False
    
    return True

def security_headers(response):
    """Add security headers to response"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response
