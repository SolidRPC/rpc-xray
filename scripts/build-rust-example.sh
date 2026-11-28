#!/usr/bin/env bash
set -euo pipefail

manifest="$1/Cargo.toml"
sdk="$(cd "$(dirname "$0")/../rust" && pwd -P)"
config="patch.crates-io.solidrpc-rpc-xray.path='$sdk'"

cargo metadata --locked --manifest-path "$manifest" --format-version 1 > /dev/null
saved_lock=$(mktemp)
cp "$1/Cargo.lock" "$saved_lock"
trap 'cp "$saved_lock" "$1/Cargo.lock" && rm "$saved_lock"' EXIT
sdk_version=$(cargo metadata --no-deps --format-version 1 --manifest-path "$sdk/Cargo.toml" | jq -er '.packages[] | select(.name == "solidrpc-rpc-xray") | .version')
cargo update --config "$config" --manifest-path "$manifest" -p solidrpc-rpc-xray --precise "$sdk_version"
cargo metadata --config "$config" --manifest-path "$manifest" --format-version 1 |
  jq -e --arg sdk "$sdk/Cargo.toml" '
    [.packages[] | select(.name == "solidrpc-rpc-xray") | .manifest_path] == [$sdk]
  ' > /dev/null || {
    echo "Expected $manifest to resolve solidrpc-rpc-xray from $sdk" >&2
    exit 1
  }
cargo build --locked --config "$config" --manifest-path "$manifest"
