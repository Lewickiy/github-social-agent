"""Dashboard API for the GitHub Social Agent.

FastAPI app that reads the same SQLite database the bot writes to and
exposes stats, user listings, profiles, actions and job control over HTTP.

Run:
    uvicorn api.app:app --host 0.0.0.0 --port 8000
"""
