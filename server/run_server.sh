#!/bin/bash

cd "$(dirname "$0")/.."
source .venv/bin/activate
cd server
python server.py

