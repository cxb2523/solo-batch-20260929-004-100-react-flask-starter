"""Pytest bootstrap: make the ``src`` package importable when running
``pytest`` from the backend directory (works with both pytest 5.x in the
Docker image and modern pytest on the host).
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
