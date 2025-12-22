#!/usr/bin/env bash
set -euo pipefail

REPO="${1:?Usage: $0 <owner/repo>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/.workflow-run-deleter.env"
TOKEN="$DELETER_PAT"

echo "Repo:  $REPO"

deleted=0

while true; do
    echo "Fetching next batch..."
    response=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO/actions/runs?per_page=100")

    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)

    if [ "$http_code" != "200" ]; then
        echo "Error response (HTTP $http_code): $body"
        exit 1
    fi

    ids=$(echo "$body" | jq -r '.workflow_runs[] | select(.status != "in_progress" and .status != "queued" and .status != "waiting") | .id')

    if [ -z "$ids" ]; then
        break
    fi

    while IFS= read -r id; do
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
            -H "Authorization: Bearer $TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$REPO/actions/runs/$id")
        echo "Deleted run $id - HTTP $http_code"
        deleted=$((deleted + 1))
    done <<< "$ids"
done

echo "Done - $deleted run(s) deleted."
