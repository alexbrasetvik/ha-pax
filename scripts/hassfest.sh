#!/usr/bin/env bash
# Run hassfest locally, the way home-assistant/actions/hassfest does in CI, without Docker:
# a shallow checkout of Home Assistant core at the version the dev environment uses, and
# a separate venv with core's requirements (hassfest imports many core components).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cache="$root/.cache"
version="$(cd "$root" && uv run --quiet python -c 'from homeassistant.const import __version__; print(__version__)')"
core="$cache/ha-core-$version"
venv="$cache/hassfest-venv-$version"

if [[ ! -d "$core" ]]; then
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$version" \
    https://github.com/home-assistant/core.git "$core"
fi
if [[ ! -x "$venv/bin/python" ]]; then
  uv venv --quiet --python 3.14 "$venv"
  VIRTUAL_ENV="$venv" uv pip install --quiet \
    -c "$core/homeassistant/package_constraints.txt" \
    -r "$core/requirements.txt" \
    "$(grep -E '^pipdeptree==' "$core/requirements_test.txt")" \
    "$(grep -E '^tqdm==' "$core/requirements_test.txt")" \
    "$(grep -E '^ruff==' "$core/requirements_test_pre_commit.txt")"
fi

cd "$core"
export PATH="$venv/bin:$PATH"
exec "$venv/bin/python" -m script.hassfest --integration-path "$root/custom_components/pax" "$@"
