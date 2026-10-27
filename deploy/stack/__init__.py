"""The stack's components as separate processes (DEMO ONLY).

config     configuration every process reads (secrets from a file)
providers  mock provider wire APIs + harness-only admin API
worker     Celery app module for ``celery -A deploy.stack.worker worker``
loops      relay / reconciler / advancer
processes  ProcessStack: spawn and stop them all
"""
