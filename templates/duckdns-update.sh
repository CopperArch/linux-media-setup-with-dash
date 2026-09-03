#!/bin/bash
source "$HOME/.config/duckdns/duckdns.env"
curl -s "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAINS}&token=${DUCKDNS_TOKEN}&ip=" \
  >> "$HOME/.config/duckdns/duckdns.log" 2>&1
echo " $(date -Iseconds)" >> "$HOME/.config/duckdns/duckdns.log"
