#!/bin/bash
source venv/bin/activate
PYTHONPATH=. pytest -v tests/
