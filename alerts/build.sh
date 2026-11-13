#!/bin/sh
set -eu
cd "$(dirname "$0")"
{
  printf 'apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n  name: rpc-xray\n  labels:\n    app.kubernetes.io/name: rpc-xray\nspec:\n'
  sed 's/^/  /' rpc-xray.rules.yml
} > rpc-xray.prometheusrule.yaml
