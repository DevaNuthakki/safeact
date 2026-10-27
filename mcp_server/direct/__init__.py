"""Direct provider tools: the unguarded baseline arm (mock providers only).

Never deployed and never pointed at a real provider: the server is built
around in-process mock provider objects handed to it by the evaluation
harness, and has no URL, configuration, database or network client
(a Phase A test forbids those imports).
"""
