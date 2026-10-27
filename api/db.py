"""One database transaction per request, as the API login role.

The transaction commits when the block completes, including when a
product function returned a refusal *result* (a failed sign-in is a
result, not an exception, so its attempt row is kept). Any exception
rolls back.
"""

from contextlib import contextmanager


@contextmanager
def transaction(request):
    with request.app.state.connection_scope() as connection:
        with connection.transaction():
            yield connection
