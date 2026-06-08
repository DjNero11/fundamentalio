import uuid

from django.core.validators import MaxLengthValidator
from django.db import models


class Report(models.Model):
    """Report storing GitHub Markdown content in a global, public history."""

    TYPE_QUICK_RESEARCH = 'quick_research'
    TYPE_DEEP_RESEARCH = 'deep_research'
    TYPE_CHOICES = [
        (TYPE_QUICK_RESEARCH, 'Quick Research'),
        (TYPE_DEEP_RESEARCH, 'Deep Research'),
    ]

    STATUS_IN_PROCESS = "in_process"
    STATUS_DONE = "done"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_IN_PROCESS, "In process"),
        (STATUS_DONE, "Done"),
        (STATUS_ERROR, "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_IN_PROCESS,
    )
    company_symbol = models.CharField(max_length=20)
    exchange_code = models.CharField(max_length=10, blank=True, default="")
    company_name = models.CharField(max_length=255)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    markdown = models.TextField(validators=[MaxLengthValidator(200_000)], blank=True)
    usage_info = models.TextField(validators=[MaxLengthValidator(50_000)], blank=True)

    class Meta:
        verbose_name = 'report'
        verbose_name_plural = 'reports'

    def __str__(self):
        return f"Report {self.id} ({self.company_symbol})"
