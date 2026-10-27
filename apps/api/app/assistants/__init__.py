"""Identity-bound assistants embedded in the connected insurance portal.

The business layer (`app.business`) stays independent of AI providers; this package
reads it through `InsuranceBusinessService` and never mutates a run.
"""
