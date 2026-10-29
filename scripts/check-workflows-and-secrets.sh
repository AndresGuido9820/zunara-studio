#!/usr/bin/env bash
# Linux x86_64 only; CI downloads versioned tools and verifies reviewed hashes.
set -euo pipefail
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'This helper requires Linux x86_64; run actionlint and gitleaks manually elsewhere.' >&2
  exit 2
fi
zunara_tools=$(mktemp -d)
trap 'rm -rf "$zunara_tools"' EXIT
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  --output "$zunara_tools/gitleaks.tar.gz"
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz \
  --output "$zunara_tools/actionlint.tar.gz"
(
  cd "$zunara_tools"
  echo '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  gitleaks.tar.gz' | sha256sum --check
  echo '8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8  actionlint.tar.gz' | sha256sum --check
  tar --extract --gzip --file gitleaks.tar.gz gitleaks
  tar --extract --gzip --file actionlint.tar.gz actionlint
)
"$zunara_tools/actionlint" -color
"$zunara_tools/gitleaks" dir . --redact --no-banner
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  "$zunara_tools/gitleaks" git . --redact --no-banner
fi
