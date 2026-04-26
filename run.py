#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import os
import re
import uvicorn


class SensitiveDataFilter(logging.Filter):
    """Filter to redact sensitive data from log messages."""
    
    # Patterns to redact (cookie values, tokens, etc.)
    SENSITIVE_PATTERNS = [
        # Cookie values - redact the value part
        (re.compile(r'(auth_token|tubeyou_csrf|tubeyou_session|session|token)=([^;\s]+)', re.IGNORECASE), r'\1=***REDACTED***'),
        # Authorization headers
        (re.compile(r'(authorization:\s*)(bearer\s+)?([^\s]+)', re.IGNORECASE), r'\1\2***REDACTED***'),
        # API keys in headers
        (re.compile(r'(x-api-key:\s*)([^\s]+)', re.IGNORECASE), r'\1***REDACTED***'),
        # sec-websocket-key (not sensitive but noisy)
        (re.compile(r'(sec-websocket-key:\s*)([^\s]+)', re.IGNORECASE), r'\1***'),
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            # Convert args to list for modification
            args = list(record.args) if isinstance(record.args, tuple) else [record.args]
            modified = False
            
            for i, arg in enumerate(args):
                if isinstance(arg, str):
                    new_arg = arg
                    for pattern, replacement in self.SENSITIVE_PATTERNS:
                        new_arg = pattern.sub(replacement, new_arg)
                    if new_arg != arg:
                        args[i] = new_arg
                        modified = True
            
            if modified:
                record.args = tuple(args)
        
        # Also check the message itself
        if isinstance(record.msg, str):
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        
        return True


if __name__ == "__main__":
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    dev_mode = log_level == "debug"

    # Add sensitive data filter to uvicorn loggers
    sensitive_filter = SensitiveDataFilter()
    for logger_name in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
        logging.getLogger(logger_name).addFilter(sensitive_filter)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=dev_mode,
        log_level=log_level,
    )
