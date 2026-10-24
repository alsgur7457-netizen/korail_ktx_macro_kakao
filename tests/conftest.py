"""Pytest configuration and shared fixtures."""
import os
import sys
from pathlib import Path

# Keep the developer's real .env out of the tests.
os.environ.setdefault("ENV_FILE", os.devnull)

# Add src/ to Python path so tests can import project modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
