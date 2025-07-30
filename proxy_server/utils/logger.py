"""
Logging utilities with Windows encoding support
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logger():
    """Setup application logging with proper encoding for Windows"""
    log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper())
    log_file = os.getenv('LOG_FILE', 'proxy.log')

    # Create formatter without emojis
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Setup file handler with UTF-8 encoding
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=10485760, 
        backupCount=5,
        encoding='utf-8'  # ★ Force UTF-8 encoding
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    # Setup console handler with proper encoding
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    
    # Set encoding for console on Windows
    if sys.platform.startswith('win'):
        try:
            console_handler.stream.reconfigure(encoding='utf-8')
        except (AttributeError, OSError):
            # Fallback for older Python versions or if reconfigure fails
            pass

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Reduce noise from external libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('geventwebsocket').setLevel(logging.WARNING)

def get_logger(name):
    """Get logger instance"""
    return logging.getLogger(name)
