"""
Shared pytest fixtures and path setup.
"""
import sys
import os

# Make backend importable without installing as a package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


import pytest
import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """
    Redirect db module to a fresh in-file temp database.
    Each test gets an isolated, empty DB.
    """
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_file)
    db_module.init_db()
    return db_module
