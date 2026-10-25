"""Vercel serverless entrypoint: the whole FastAPI app runs as one Python function."""
from app.main import app  # noqa: F401
