#!/bin/sh
# Fail-closed tombstone for the retired sub2cli desktop build.

printf '%s\n' \
  'sub2cli is retired (EOL 2026-07-22); desktop builds are disabled and no artifacts were created.' \
  >&2
exit 64
