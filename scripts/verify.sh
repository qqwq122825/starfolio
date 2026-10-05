#!/usr/bin/env bash
# Local tests/build only. Does not deploy or contact a broker.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/python"
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check static/app.js
node tests/static_ui_check.cjs
cd "$root/web"
npm test
npm run build
npm run typecheck
npm run lint
