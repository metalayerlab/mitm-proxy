#!/usr/bin/env bash
# Validate the deployment manifests: what CI's `manifests` job runs.
# Template-owned. A missing tool is skipped locally, but is an error in CI.
# Quiet on a pass: one summary line; a failing tool's output in full (design record 0007).
set -euo pipefail
cd "$(dirname "$0")/.."

status=0
checked=""
skipped=""
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

need() {
    command -v "$1" > /dev/null 2>&1 && return 0
    if [ "${CI:-}" = "true" ]; then
        echo "error: $1 is not installed"
        status=1
    else
        skipped="$skipped $1"
    fi
    return 1
}

run() {
    local out
    if out="$("$@" 2>&1)"; then
        checked="$checked $1"
    else
        printf 'error: %s\n%s\n' "$*" "$out"
        status=1
    fi
}

if need hadolint; then
    run hadolint Dockerfile
fi

if need docker; then
    run docker compose -f contrib/docker/compose.yml config --quiet
fi

if need kubectl; then
    run kubectl kustomize contrib/k8s -o "$tmp/k8s.yaml"
    if need kubeconform && [ -s "$tmp/k8s.yaml" ]; then
        run kubeconform -strict -summary "$tmp/k8s.yaml"
    fi
fi

if need systemd-analyze; then
    # verify wants ExecStart's binary to exist: check copies that point at /bin/true.
    mkdir -p "$tmp/systemd"
    for unit in contrib/systemd/*.service; do
        sed 's|^ExecStart=[^ ]*|ExecStart=/bin/true|' "$unit" > "$tmp/systemd/$(basename "$unit")"
    done
    run systemd-analyze verify --man=no "$tmp"/systemd/*.service
fi

if [ "$status" -eq 0 ]; then
    echo "manifests OK (checked:${checked:- nothing}${skipped:+; not installed, skipped:$skipped})"
fi
exit "$status"
