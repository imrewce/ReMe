"""Collect external observations into workspace resource packages."""

from .base_connector import BaseConnectorStep
from .finance_news import FinanceNewsStep
from .imap_mail import MailAuthStep, MailFetchStep
from .screen_capture import ScreenCaptureStep
from .source_cursor import SourceCursorStep

__all__ = [
    "BaseConnectorStep",
    "FinanceNewsStep",
    "MailAuthStep",
    "MailFetchStep",
    "ScreenCaptureStep",
    "SourceCursorStep",
]
