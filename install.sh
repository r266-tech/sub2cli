#!/bin/sh
# Fail-closed tombstone for the retired sub2cli installer.

printf '%s\n' \
  'sub2cli is retired (EOL 2026-07-22); this installer is disabled and made no changes.' \
  >&2
exit 64
