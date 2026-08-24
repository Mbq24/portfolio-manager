#!/usr/bin/env bash
# build_bundle.sh — Bundle the Forge modules portfolio-manager needs at
# runtime into vendor/forge/ so the Docker image is self-contained.
#
# The Forge repo lives on the dev Mac (iCloud); the container can't reach it,
# so we copy the needed modules at build time. vendor/ is gitignored — no
# Forge code is committed into the portfolio-manager repo (keeps ownership
# clean: this is a runtime dependency, not a fork).
set -euo pipefail

FORGE="${FORGE_ROOT:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/VisualStudioProjects/Forge}"
VENDOR="$(cd "$(dirname "$0")" && pwd)/vendor/forge"

mkdir -p "$VENDOR/dsl" "$VENDOR/generators" "$VENDOR/tradingview" "$VENDOR/examples"

# DSL package (schema / indicators / conditions)
cp "$FORGE/dsl/__init__.py" "$VENDOR/dsl/" 2>/dev/null || true
cp "$FORGE/dsl/schema.py" "$FORGE/dsl/indicators.py" "$FORGE/dsl/conditions.py" "$VENDOR/dsl/"

# Local compute engine (the exact code the harness validates with)
cp "$FORGE/generators/local.py" "$VENDOR/generators/"

# Price fetcher (yfinance) — synthetic fallback DISABLED for trading callers
cp "$FORGE/tradingview/data_fetcher.py" "$VENDOR/tradingview/"

# Strategy YAMLs — the DSL definitions that config.json references
cp "$FORGE"/examples/*.yaml "$VENDOR/examples/"

echo "Bundled Forge modules into $VENDOR:"
find "$VENDOR" -name "*.py" -o -name "*.yaml" | sort
