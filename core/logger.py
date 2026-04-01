"""
Global logging system for AutoCooker3.
Logs all events to a single autocooker.log file for easy debugging.
"""

import os
import json
from datetime import datetime
from typing import Optional


class GlobalLogger:
    """
    Centralized logger that writes all events to autocooker.log.
    Format: JSON lines for easy parsing/grepping.
    """
    
    def __init__(self, log_file: str = "autocooker.log"):
        self.log_file = log_file
        self.session_start = datetime.now().isoformat()
        
        # Create log file if doesn't exist
        if not os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write("")  # Create empty file
    
    def log(
        self,
        phase: str,
        level: str,
        message: str,
        task_id: Optional[str] = None,
        log_type: str = "info"
    ):
        """
        Write a log entry to the global log file.
        
        Args:
            phase: Phase name (planning, coding, qa, system, etc.)
            level: Log level (info, warn, error, ok, etc.)
            message: Log message
            task_id: Optional task ID
            log_type: Type of log entry (info, tool_call, tool_result, etc.)
        """
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "session": self.session_start,
                "phase": phase,
                "level": level,
                "type": log_type,
                "message": message,
                "task_id": task_id or ""
            }
            
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            # Don't let logging errors crash the program
            print(f"[GlobalLogger ERROR] Failed to write log: {e}", flush=True)
    
    def log_phase_start(self, phase: str, task_id: str):
        """Log the start of a phase."""
        self.log(phase, "info", f"═══ {phase.upper()} PHASE START ═══", task_id, "phase_header")
    
    def log_phase_end(self, phase: str, task_id: str, success: bool):
        """Log the end of a phase."""
        status = "COMPLETE" if success else "FAILED"
        level = "ok" if success else "error"
        self.log(phase, level, f"═══ {phase.upper()} PHASE {status} ═══", task_id, "phase_header")
    
    def log_step(self, phase: str, step_name: str, task_id: str):
        """Log the start of a step."""
        self.log(phase, "info", f"─── Step {step_name} ───", task_id, "step_header")
    
    def log_tool_call(self, phase: str, tool_name: str, task_id: str, args: str = ""):
        """Log a tool call."""
        msg = f"[Tool ►] {tool_name}({args[:100]}...)" if len(args) > 100 else f"[Tool ►] {tool_name}({args})"
        self.log(phase, "info", msg, task_id, "tool_call")
    
    def log_tool_result(self, phase: str, result: str, task_id: str):
        """Log a tool result."""
        preview = result[:200] + "..." if len(result) > 200 else result
        self.log(phase, "info", f"[Tool ◄] {preview}", task_id, "tool_result")
    
    def rotate_log(self, max_size_mb: int = 10):
        """
        Rotate log file if it exceeds max_size_mb.
        Renames current log to autocooker.log.old
        """
        try:
            if not os.path.exists(self.log_file):
                return
            
            size_mb = os.path.getsize(self.log_file) / (1024 * 1024)
            if size_mb > max_size_mb:
                old_log = f"{self.log_file}.old"
                if os.path.exists(old_log):
                    os.remove(old_log)
                os.rename(self.log_file, old_log)
                print(f"[GlobalLogger] Log rotated: {self.log_file} → {old_log}", flush=True)
        except Exception as e:
            print(f"[GlobalLogger] Failed to rotate log: {e}", flush=True)


# Global singleton instance
GLOBAL_LOG = GlobalLogger()
