#!/usr/bin/env bash
# Start throwaway PostgreSQL and Redis inside a sandbox so an agent can run a
# repository's database tests. Both listen on localhost only and live and die
# with the sandbox. Safe to run more than once.
#
#   sandbox-services start   # start both; prints connection URLs
#   sandbox-services status
#   sandbox-services stop
set -euo pipefail

PG_VERSION="$(ls /usr/lib/postgresql | sort -V | tail -n 1)"
PG_CLUSTER=main

pg_running() { pg_lsclusters --no-header | awk -v v="$PG_VERSION" '$1 == v && $2 == "main" {print $4}' | grep -qx online; }
redis_running() { redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; }

start() {
    pg_running || pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" start
    redis_running || redis-server --bind 127.0.0.1 --port 6379 --daemonize yes --save "" --appendonly no >/dev/null
    for _ in $(seq 1 30); do
        if pg_isready -q -h 127.0.0.1 -p 5432 && redis_running; then
            echo "PostgreSQL ${PG_VERSION}: postgresql://postgres@127.0.0.1:5432/postgres (no password)"
            echo "Redis: redis://127.0.0.1:6379/0"
            return 0
        fi
        sleep 1
    done
    echo "services did not become ready" >&2
    return 1
}

status() {
    if pg_running; then echo "PostgreSQL ${PG_VERSION}: running"; else echo "PostgreSQL ${PG_VERSION}: stopped"; fi
    if redis_running; then echo "Redis: running"; else echo "Redis: stopped"; fi
}

stop() {
    pg_running && pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" stop
    redis_running && redis-cli -h 127.0.0.1 shutdown nosave >/dev/null 2>&1 || true
}

case "${1:-start}" in
    start) start ;;
    status) status ;;
    stop) stop ;;
    *) echo "usage: sandbox-services {start|status|stop}" >&2; exit 2 ;;
esac
