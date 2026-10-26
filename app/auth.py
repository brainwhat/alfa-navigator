"""Demo role tokens. Strictly a demo: static per-role tokens, see docs/limitations.md."""
import secrets

from fastapi import Header, HTTPException

from app import settings

def same(given: str, expected: str) -> bool:
    # compare_digest takes ASCII-only str, so compare UTF-8 bytes: a Cyrillic password is just wrong, not an error
    return secrets.compare_digest(given.encode(), expected.encode())

def token_for(role: str) -> str:
    return settings.DEMO_CLIENT_KEY if role == 'client' else settings.DEMO_BANK_KEY

def require(role: str):
    def dependency(authorization: str | None = Header(None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Требуется вход')
        if not same(authorization[7:], token_for(role)):
            raise HTTPException(403, 'Недостаточно прав')
        return role
    return dependency

def validate_any(authorization: str | None):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Требуется вход')
    candidate = authorization[7:]
    if not (same(candidate, settings.DEMO_CLIENT_KEY) or same(candidate, settings.DEMO_BANK_KEY)):
        raise HTTPException(403, 'Недостаточно прав')
