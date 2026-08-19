"""
Application configuration with load balancing and WebSocket support
"""

import os
from dotenv import load_dotenv

load_dotenv()

def parse_urls(url_string):
    """Parse comma-separated URLs or return single URL"""
    if not url_string:
        return []
    urls = [url.strip() for url in url_string.split(',') if url.strip()]
    return urls if len(urls) > 1 else urls[0] if urls else None

class Config:
    """Base configuration class"""

    # Flask settings
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')
    DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    # Backend App Name And Route
    BACKEND_ROUTE_NAMES = {
        '/app1': 'Whatsapp ChatBot',
        '/app2': 'Disease Prediction',
        '/api': 'Proxy Router',
        '/ollama': 'Ollama (local GPU)'
    }

    # Backend services with load balancing support
    BACKEND_ROUTES = {
        '/app1': parse_urls(os.getenv('APP1_URLS', os.getenv('APP1_URL', 'http://localhost:3000'))),
        '/app2': parse_urls(os.getenv('APP2_URLS', os.getenv('APP2_URL', 'http://localhost:5000'))),
        '/api': parse_urls(os.getenv('API_URLS', os.getenv('API_URL', 'http://localhost:7000'))),
        # Local Ollama. get_backend strips the /ollama prefix, so
        # /ollama/api/embed -> http://127.0.0.1:11434/api/embed and
        # /ollama/v1/chat/completions -> .../v1/chat/completions.
        '/ollama': parse_urls(os.getenv('OLLAMA_URLS', 'http://127.0.0.1:11434')),
        # '/treasure-hunt': parse_urls('http://192.168.12.49:5050')
    }

    # Check Health Routes
    BACKEND_HEALTH_ROUTES = {
        '/app1': os.getenv('APP1_HEALTH_ROUTE', '/check-health'),
        '/app2': os.getenv('APP2_HEALTH_ROUTE', '/health'),
        '/api': os.getenv('API_HEALTH_ROUTE', '/health'),
        # Ollama answers /api/tags as soon as the daemon is ready to serve.
        '/ollama': os.getenv('OLLAMA_HEALTH_ROUTE', '/api/tags'),
        # '/treasure-hunt': '/api/health'
    }

    # Socket.IO / WebSocket Configuration ★ ENHANCED ★
    SOCKETIO_CORS_ENABLED = os.getenv('SOCKETIO_CORS_ENABLED', 'True').lower() == 'true'
    SOCKETIO_CORS_ORIGINS = os.getenv('SOCKETIO_CORS_ORIGINS', '*')
    SOCKETIO_PATH = os.getenv('SOCKETIO_PATH', '/socket.io/')

    # WebSocket Timeouts and Limits ★ ENHANCED ★
    CLIENT_IDLE_TIMEOUT = int(os.getenv('CLIENT_IDLE_TIMEOUT_SECONDS', 300))
    BACKEND_SOCKET_TIMEOUT = int(os.getenv('BACKEND_SOCKET_TIMEOUT_SECONDS', 300))
    WS_CLIENT_CONNECT_TIMEOUT = int(os.getenv('WS_CLIENT_CONNECT_TIMEOUT', 10))
    WS_BACKEND_CONNECT_TIMEOUT = int(os.getenv('WS_BACKEND_CONNECT_TIMEOUT', 30))
    WS_MAX_MESSAGE_SIZE = int(os.getenv('WS_MAX_MESSAGE_SIZE', 16_777_216))  # 16 MB
    WS_PING_INTERVAL = int(os.getenv('WS_PING_INTERVAL', 20))  # Reduced to 20s for better keep-alive

    # Tunnel Configuration ★ NEW ★
    TUNNEL_BUFFER_SIZE = int(os.getenv('TUNNEL_BUFFER_SIZE', 4096))
    TUNNEL_MAX_ERRORS = int(os.getenv('TUNNEL_MAX_ERRORS', 3))
    TUNNEL_RETRY_DELAY = float(os.getenv('TUNNEL_RETRY_DELAY', 0.1))

    # Outbound Tunnel ★ NEW ★
    # Makes this proxy reachable from a public server without exposing this
    # machine — see proxy_server/core/tunnel_client.py and tunnel_server/.
    # Unset TUNNEL_SERVER_URL means a normal local-only run.
    TUNNEL_SERVER_URL = os.getenv('TUNNEL_SERVER_URL', '')
    TUNNEL_TOKEN = os.getenv('TUNNEL_TOKEN', '')
    TUNNEL_LOCAL_TARGET = os.getenv('TUNNEL_LOCAL_TARGET', 'http://127.0.0.1:2000')

    # Routes whose responses must never be buffered. Ollama streams SSE tokens
    # for /v1/chat/completions and NDJSON progress for /api/pull; buffering
    # those turns a live stream into one lump delivered at the end.
    # Matched as path prefixes against the incoming request.
    STREAMING_ROUTES = tuple(
        r.strip() for r in os.getenv('STREAMING_ROUTES', '/ollama').split(',') if r.strip()
    )
    # Response content types that always stream, whatever the route.
    STREAMING_CONTENT_TYPES = ('text/event-stream', 'application/x-ndjson')
    # Read timeout for streaming routes. REQUEST_TIMEOUT is tuned for ordinary
    # web backends; an Ollama /api/pull runs for minutes.
    STREAMING_READ_TIMEOUT = int(os.getenv('STREAMING_READ_TIMEOUT', 600))


    # API Key Authentication
    API_KEY_HEADER = os.getenv('API_KEY_HEADER', 'X-API-Key')
    API_KEY_PREFIX = os.getenv('API_KEY_PREFIX', 'pk_')
    API_KEY_LENGTH = int(os.getenv('API_KEY_LENGTH', 32))
    MASTER_API_KEY = os.getenv('MASTER_API_KEY', 'master-api-key-12345')

    # Security
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*')

    # Rate limiting
    RATE_LIMIT_ENABLED = os.getenv('RATE_LIMIT_ENABLED', 'True').lower() == 'true'
    RATE_LIMIT_REQUESTS = int(os.getenv('RATE_LIMIT_REQUESTS', 100))
    RATE_LIMIT_WINDOW = int(os.getenv('RATE_LIMIT_WINDOW', 3600))

    # Performance ★ ENHANCED ★
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', 90))  # Increased from 30
    HEALTH_CHECK_CONNECT_TIMEOUT = int(os.getenv('HEALTH_CHECK_CONNECT_TIMEOUT', 5))
    HEALTH_CHECK_READ_TIMEOUT = int(os.getenv('HEALTH_CHECK_READ_TIMEOUT', 30))
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', 16777216))

    # Monitoring
    HEALTH_CHECK_INTERVAL = int(os.getenv('HEALTH_CHECK_INTERVAL', 30))
    BACKEND_HEALTH_CHECK = os.getenv('BACKEND_HEALTH_CHECK', 'True').lower() == 'true'

    # Load balancing
    LOAD_BALANCER_STRATEGY = os.getenv('LOAD_BALANCER_STRATEGY', 'round_robin')
    UNHEALTHY_THRESHOLD = int(os.getenv('UNHEALTHY_THRESHOLD', 3))
    RECOVERY_THRESHOLD = int(os.getenv('RECOVERY_THRESHOLD', 2))

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', 'proxy.log')

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    BACKEND_ROUTES = {
        '/app1': ['http://localhost:3001', 'http://localhost:3002'],
        '/app2': ['http://localhost:5001', 'http://localhost:5002'],
        '/api': 'http://localhost:7001',
        # '/treasure-hunt': 'http://192.168.12.49:5050'
    }
