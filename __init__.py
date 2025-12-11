"""
ComfyUI-Sentinel Custom Node
Includes Unicode encoding fix for Windows service compatibility
"""

import sys
import io

# Patch LogInterceptor to handle Unicode encoding errors gracefully
# This is needed when running as Windows service where console encoding is cp1252
def patch_logger_encoding():
    """Patch ComfyUI's LogInterceptor to handle Unicode encoding errors."""
    try:
        # Import app.logger after ComfyUI has initialized it
        import app.logger
        
        # Check if LogInterceptor exists and hasn't been patched yet
        if hasattr(app.logger, 'LogInterceptor') and not hasattr(app.logger.LogInterceptor, '_unicode_patched'):
            original_write = app.logger.LogInterceptor.write
            
            def safe_write(self, data):
                """Write method that handles Unicode encoding errors."""
                try:
                    return original_write(self, data)
                except UnicodeEncodeError:
                    # Handle Unicode encoding errors gracefully
                    try:
                        # Try to encode with errors='replace' to replace problematic characters
                        if isinstance(data, str):
                            safe_data = data.encode(self.encoding, errors='replace').decode(self.encoding)
                            return original_write(self, safe_data)
                    except (UnicodeEncodeError, UnicodeDecodeError, AttributeError):
                        # If that fails, try ASCII fallback
                        try:
                            if isinstance(data, str):
                                safe_data = data.encode('ascii', errors='replace').decode('ascii')
                                return original_write(self, safe_data)
                        except Exception:
                            # Last resort: write a placeholder message
                            try:
                                return original_write(self, "[Unicode encoding error: unable to display message]\n")
                            except Exception:
                                # If even that fails, silently ignore
                                pass
            
            # Apply the patch
            app.logger.LogInterceptor.write = safe_write
            app.logger.LogInterceptor._unicode_patched = True
            
            # Also patch existing instances if they exist
            if hasattr(app.logger, 'stdout_interceptor') and app.logger.stdout_interceptor:
                app.logger.stdout_interceptor.write = lambda data: safe_write(app.logger.stdout_interceptor, data)
            if hasattr(app.logger, 'stderr_interceptor') and app.logger.stderr_interceptor:
                app.logger.stderr_interceptor.write = lambda data: safe_write(app.logger.stderr_interceptor, data)
    except Exception:
        # Silently fail if patching doesn't work - better than crashing
        pass

# Try to patch the logger (may fail if logger not initialized yet, which is OK)
try:
    patch_logger_encoding()
except Exception:
    pass

from .nodes import *
from .sentinel import *

# Try patching again after imports (logger should be initialized by now)
try:
    patch_logger_encoding()
except Exception:
    pass

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]
