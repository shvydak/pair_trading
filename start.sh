#!/bin/bash
cd backend
pip install -r requirements.txt -q
uvicorn main:app --reload --port 8000
