import sys

# PyInstaller compatibility: must run before importing other modules
if getattr(sys, 'frozen', False):
    # Running in a PyInstaller-packaged environment
    import inspect
    
    _original_getsource = inspect.getsource
    _original_getsourcelines = inspect.getsourcelines
    _original_findsource = inspect.findsource
    
    def _getsource_fallback(obj):
        try:
            return _original_getsource(obj)
        except (OSError, TypeError):
            return f"# Source not available\n"
    
    def _getsourcelines_fallback(obj):
        try:
            return _original_getsourcelines(obj)
        except (OSError, TypeError):
            return (["# Source not available\n"], 0)
    
    def _findsource_fallback(obj):
        try:
            return _original_findsource(obj)
        except (OSError, TypeError):
            return (["# Source not available\n"], 0)
    
    inspect.getsource = _getsource_fallback
    inspect.getsourcelines = _getsourcelines_fallback
    inspect.findsource = _findsource_fallback
    

from uvicorn import run
from main import app
from app.core import settings
 
if __name__ == "__main__":
	# Local single-user application: loopback by default (override with HOST / PORT, unsupported beyond this machine).
	run(app, host=settings.app.host, port=settings.app.port, log_level="info") 