"""B3 local human/password authentication; hash-only, expiring bearer sessions."""

import re
import secrets
from datetime import timedelta
from hashlib import sha256

import sqlalchemy as sa


class Unauthenticated(ValueError):
    pass


class Forbidden(ValueError):
    pass


class Missing(ValueError):
    pass


TOKEN = re.compile(r"sb3s_[A-Za-z0-9_-]{43}\Z")
NO_SECRET_LOGGING = dict(log_statement="none",log_duration="off",log_min_duration_statement="-1",
    log_min_duration_sample="-1",log_transaction_sample_rate="0",log_parameter_max_length="0",
    log_parameter_max_length_on_error="0",log_min_error_statement="panic")


class Identity:
    def __init__(self,engine,tables):
        self.engine,self.t = engine,tables

    def login(self,username,password):
        if type(username) is not str or type(password) is not str or not 1<=len(password.encode())<=72:
            raise Unauthenticated("Invalid login")
        with self.engine.begin() as c:
            human = c.execute(sa.select(self.t.humans).where(self.t.humans.c.username==username)
                ).mappings().one_or_none()
            # pgcrypto is already a pinned database dependency. Unknown users
            # also do one bcrypt-12 operation, using a fresh salt with no hash
            # that could match. Application role logging suppresses all secrets.
            salt = human["password_hash"] if human else c.scalar(sa.select(sa.func.public.gen_salt("bf",12)))
            valid = c.scalar(sa.select(sa.func.public.crypt(password,salt)))==salt
            if not valid or not human or not human["enabled"]:
                raise Unauthenticated("Invalid login")
            token = "sb3s_"+secrets.token_urlsafe(32)
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            expires = now+timedelta(hours=8)
            c.execute(self.t.sessions.insert().values(token_sha256=sha256(token.encode()).hexdigest(),
                actor_id=human["actor_id"],expires_at=expires))
            return dict(token=token,expires_at=expires)

    def authenticate(self,c,token,*,roles=()):
        if type(token) is not str or not TOKEN.fullmatch(token):
            raise Unauthenticated("A live human session is required")
        now = c.scalar(sa.select(sa.func.clock_timestamp()))
        human = c.execute(sa.select(self.t.humans).join(self.t.sessions).where(
            self.t.sessions.c.token_sha256==sha256(token.encode()).hexdigest(),self.t.sessions.c.expires_at>now,
            self.t.sessions.c.revoked.is_(False),self.t.humans.c.enabled.is_(True))
            .with_for_update(read=True,of=self.t.sessions)).mappings().one_or_none()
        if human is None:
            raise Unauthenticated("A live human session is required")
        if roles and not set(roles).intersection(human["roles"]):
            raise Forbidden("Human role required")
        return dict(actor_id=human["actor_id"],tenant_id=human["tenant_id"],roles=tuple(human["roles"]))

    def logout(self,token):
        if type(token) is not str or not TOKEN.fullmatch(token):
            raise Unauthenticated("A live human session is required")
        with self.engine.begin() as c:
            changed = c.execute(self.t.sessions.update().where(self.t.sessions.c.revoked.is_(False),
                self.t.sessions.c.token_sha256==sha256(token.encode()).hexdigest()).values(revoked=True))
            if changed.rowcount!=1:raise Unauthenticated("A live human session is required")
