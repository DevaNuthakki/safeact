"""Connector adapters (connector logic) and in-process mock providers.

Adapters (``payments``, ...) classify provider responses and normalize
lookups under a connector profile. Mock providers (``*_mock``) are test
doubles that record every call and effect in an ``EffectsLedger`` the
evaluation harness owns. Adapters, workers and reconcilers never read
the ledger: it is not a lookup endpoint.
"""
