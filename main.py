"""
Main entry point for the Flask Reverse Proxy Server
"""

import os
from proxy_server.core.app import create_app
from proxy_server.utils.logger import setup_logger

# Setup logging first
setup_logger()

# Create the Flask app instance for WSGI servers
app = create_app()

def main():
    """Main application entry point for development"""
    
    # Get configuration
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', 2000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("🚀 Starting Flask Reverse Proxy Server...")
    print(f"📍 Server: http://{host}:{port}")
    print("📋 Available endpoints:")
    print(" - /app1/* -> Chatbot service")
    print(" - /app2/* -> Disease prediction service") 
    print(" - /api/* -> Admin panel service")
    print(" - /admin/* -> Proxy admin endpoints")
    
    # Run the application
    app.run(host=host, port=port, debug=debug)

if __name__ == '__main__':
    main()
