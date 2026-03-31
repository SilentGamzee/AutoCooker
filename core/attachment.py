"""
Attachment module containing Attachment dataclass and validation functions.
"""

import base64
from dataclasses import dataclass, field
from typing import Optional


# Allowed content types for images and files
ALLOWED_IMAGE_CONTENT_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/svg+xml",
]

ALLOWED_FILE_CONTENT_TYPES = [
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
    "application/json",
]

ALLOWED_CONTENT_TYPES = ALLOWED_IMAGE_CONTENT_TYPES + ALLOWED_FILE_CONTENT_TYPES

MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10MB


@dataclass
class Attachment:
    """
    Represents an attachment with metadata and content.
    
    Attributes:
        type: The type of attachment (e.g., 'image', 'file')
        name: The name of the attachment file
        size_bytes: The size of the attachment in bytes
        content_type: The MIME type of the attachment
        data: The base64-encoded content of the attachment
    """
    type: str
    name: str
    size_bytes: int
    content_type: str
    data: str


def validate_attachment(attachment: Attachment) -> Optional[str]:
    """
    Validates an Attachment object.
    
    Validates:
    - Required fields are present and non-empty
    - Attachment type is 'image' or 'file'
    - content_type is in the allowed lists
    - size_bytes is <= 10MB
    - data is valid base64
    
    Args:
        attachment: The Attachment object to validate
        
    Returns:
        None if valid, or an error message string if validation fails
    """
    # Check required fields are present
    if not hasattr(attachment, 'type'):
        return "Missing required field: type"
    if not hasattr(attachment, 'name'):
        return "Missing required field: name"
    if not hasattr(attachment, 'size_bytes'):
        return "Missing required field: size_bytes"
    if not hasattr(attachment, 'content_type'):
        return "Missing required field: content_type"
    if not hasattr(attachment, 'data'):
        return "Missing required field: data"
    
    # Validate type field
    if not attachment.type:
        return "Attachment type cannot be empty"
    if attachment.type not in ['image', 'file']:
        return f"Invalid attachment type: '{attachment.type}'. Must be 'image' or 'file'"
    
    # Validate name field
    if not attachment.name:
        return "Attachment name cannot be empty"
    
    # Validate size_bytes
    if not isinstance(attachment.size_bytes, int):
        return "size_bytes must be an integer"
    if attachment.size_bytes < 0:
        return "size_bytes cannot be negative"
    if attachment.size_bytes > MAX_SIZE_BYTES:
        return f"Attachment size exceeds maximum limit of {MAX_SIZE_BYTES} bytes (10MB)"
    
    # Validate content_type
    if not attachment.content_type:
        return "Content type cannot be empty"
    if attachment.content_type not in ALLOWED_CONTENT_TYPES:
        return f"Invalid content_type: '{attachment.content_type}'. Must be one of the allowed content types"
    
    # Validate data field
    if not attachment.data:
        return "Attachment data cannot be empty"
    
    # Validate base64 data
    try:
        base64.b64decode(attachment.data)
    except Exception as e:
        return f"Invalid base64 data: {str(e)}"
    
    return None
