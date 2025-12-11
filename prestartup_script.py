"""
ComfyUI-Sentinel Prestartup Script
Fixes Unicode encoding issues when running as Windows service.
This script runs before ComfyUI's logger is initialized, allowing us to
set UTF-8 encoding for stdout/stderr to handle Unicode characters (emojis, etc.)
"""

import sys
import os

def fix_unicode_encoding():
    """
    Fix Unicode encoding issues when running as Windows service.
    Windows services default to cp1252 encoding which can't handle Unicode characters.
    """
    if sys.platform != 'win32':
        return
    
    try:
        # Set UTF-8 encoding for stdout/stderr before logger is initialized
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            except (AttributeError, ValueError):
                pass
        
        if hasattr(sys.stderr, 'reconfigure'):
            try:
                sys.stderr.reconfigure(encoding='utf-8', errors='replace')
            except (AttributeError, ValueError):
                pass
        
        # Set environment variable for subprocesses and Python's default encoding
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        
        # Also patch the default encoding if possible
        if hasattr(sys, 'setdefaultencoding'):
            sys.setdefaultencoding('utf-8')
    except Exception:
        # Silently fail if we can't set encoding - better than crashing
        pass

# Execute the fix immediately when this module is imported
fix_unicode_encoding()

