"""
Sandbox module for task isolation.

Implements rules:
1. During task 1 work, files can be written outside the task folder (e.g., task_003)
2. Task files (e.g., task_003.json) must be in the task folder (e.g., task_003)
3. README must also be in the task folder
4. All writes outside the task folder should be interrupted and not executed
5. All files modified from the project folder should have the same relative path but in the task folder
6. When reading files and folders, files and folders from other tasks should be ignored and not displayed
"""
from __future__ import annotations
import json
import os
import re
from typing import Optional


class Sandbox:
    """
    Sandbox manager for task isolation.
    
    Maps project paths to task-specific paths and enforces write restrictions.
    """
    
    def __init__(self, task_dir: str, project_path: str):
        """
        Initialize sandbox.
        
        Args:
            task_dir: Absolute path to the task directory (e.g., .tasks/task_001)
            project_path: Absolute path to the project root
        """
        self.task_dir = os.path.abspath(task_dir)
        self.project_path = os.path.abspath(project_path)
        self._task_number = self._extract_task_number(task_dir)
        self._enabled = True
    
    def _extract_task_number(self, task_dir: str) -> int:
        """Extract task number from task directory path."""
        basename = os.path.basename(task_dir)
        match = re.match(r'task_(\d+)', basename)
        if match:
            return int(match.group(1))
        return 0
    
    def is_enabled(self) -> bool:
        """Check if sandbox is enabled."""
        return self._enabled
    
    def enable(self):
        """Enable sandbox."""
        self._enabled = True
    
    def disable(self):
        """Disable sandbox."""
        self._enabled = False
    
    def is_enabled_for_task(self, task_dir: str) -> bool:
        """
        Check if sandbox is enabled for a specific task.
        
        Args:
            task_dir: Absolute path to the task directory
            
        Returns:
            True if sandbox is enabled for this task
        """
        if not self._enabled:
            return False
        
        task_basename = os.path.basename(task_dir)
        match = re.match(r'task_(\d+)', task_basename)
        if match:
            return True
        return False
    
    def map_project_path(self, project_path: str) -> str:
        """
        Map a project path to the task-specific path.
        
        Args:
            project_path: Path relative to project root (e.g., src/main.py)
            
        Returns:
            Task-specific path (e.g., task_001/src/main.py)
        """
        if not self._enabled:
            return project_path
        
        # Remove project prefix and add task prefix
        if project_path.startswith(self.project_path):
            relative = project_path[len(self.project_path):].lstrip('/')
            return os.path.join(self.task_dir, relative)
        
        return project_path
    
    def map_task_path(self, task_path: str) -> str:
        """
        Map a task-specific path back to project path.
        
        Args:
            task_path: Path relative to task directory (e.g., src/main.py)
            
        Returns:
            Project path (e.g., src/main.py)
        """
        if not self._enabled:
            return task_path
        
        # Remove task prefix and add project prefix
        if task_path.startswith(self.task_dir):
            relative = task_path[len(self.task_dir):].lstrip('/')
            return os.path.join(self.project_path, relative)
        
        return task_path
    
    def should_allow_write(self, target_path: str) -> tuple[bool, str]:
        """
        Check if a write operation should be allowed.
        
        Args:
            target_path: Absolute path where file will be written
            
        Returns:
            Tuple of (allowed, reason)
        """
        if not self._enabled:
            return True, "Sandbox disabled"
        
        # Normalize paths
        target_abs = os.path.abspath(target_path)
        task_abs = os.path.abspath(self.task_dir)
        project_abs = os.path.abspath(self.project_path)
        
        # Rule 2: Task files must be in task folder
        if target_path.endswith('.json') and 'task_' not in target_path:
            return False, f"Task file must be in task folder: {target_path}"
        
        # Rule 3: README must be in task folder
        if target_path.endswith('README.md'):
            if not target_path.startswith(task_abs):
                return False, f"README must be in task folder: {target_path}"
        
        # Rule 4: Writes outside task folder should be interrupted
        if not target_path.startswith(task_abs):
            return False, f"Write outside task folder not allowed: {target_path}"
        
        # Rule 5: Files modified from project folder should have same relative path
        if target_path.startswith(project_abs):
            relative = target_path[len(project_abs):].lstrip('/')
            expected = os.path.join(task_abs, relative)
            if target_path != expected:
                return False, f"Path mismatch: expected {expected}, got {target_path}"
        
        return True, "OK"
    
    def should_allow_read(self, target_path: str) -> tuple[bool, str]:
        """
        Check if a read operation should be allowed.
        
        Args:
            target_path: Absolute path of file to read
            
        Returns:
            Tuple of (allowed, reason)
        """
        if not self._enabled:
            return True, "Sandbox disabled"
        
        # Normalize paths
        target_abs = os.path.abspath(target_path)
        task_abs = os.path.abspath(self.task_dir)
        project_abs = os.path.abspath(self.project_path)
        
        # Rule 6: Ignore files from other tasks
        if target_path.startswith(project_abs):
            # Check if file is in a different task folder
            task_match = re.search(r'task_(\d+)', target_path)
            if task_match:
                other_task_num = int(task_match.group(1))
                if other_task_num != self._task_number:
                    return False, f"File from other task ignored: {target_path}"
        
        return True, "OK"
    
    def get_task_path(self, project_path: str) -> str:
        """
        Get the task-specific path for a project path.
        
        Args:
            project_path: Path relative to project root
            
        Returns:
            Task-specific path
        """
        return self.map_project_path(project_path)
    
    def get_project_path(self, task_path: str) -> str:
        """
        Get the project path for a task-specific path.
        
        Args:
            task_path: Path relative to task directory
            
        Returns:
            Project path
        """
        return self.map_task_path(task_path)
    
    def validate_path(self, path: str, operation: str) -> tuple[bool, str]:
        """
        Validate a path for a given operation.
        
        Args:
            path: Absolute path to validate
            operation: 'read' or 'write'
            
        Returns:
            Tuple of (valid, reason)
        """
        if operation == 'write':
            return self.should_allow_write(path)
        elif operation == 'read':
            return self.should_allow_read(path)
        else:
            return True, "Unknown operation"


def create_sandbox(task_dir: str, project_path: str) -> Sandbox:
    """
    Create a sandbox instance for a task.
    
    Args:
        task_dir: Absolute path to task directory
        project_path: Absolute path to project root
        
    Returns:
        Sandbox instance
    """
    return Sandbox(task_dir, project_path)
