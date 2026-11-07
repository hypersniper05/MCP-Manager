#!/bin/sh
# MCP-Manager installer for Linux and macOS (not tested on a Mac yet). On Windows, run install.bat.
#
#   ./install.sh                 asks who can reach it and whether to use HTTPS, then starts it
#   ./install.sh --yes           no questions: this computer only, plain HTTP
#   ./install.sh --network       other devices on your network can reach it too
#   ./install.sh --https         HTTPS, with a certificate made for this computer
#   ./install.sh --http          plain HTTP (to go back from --https)
#   ./install.sh --name mcp.lan  a host name the certificate must cover (repeat for more)
#   ./install.sh --port 9000     another port (default 8899)
#   ./install.sh --no-trust      leave this computer's trust store alone
#   ./install.sh --trust FILE    only trust a certificate: for another device that connects
#
# Safe to run again: it keeps your admin token, your data and a certificate that still fits.
set -eu
cd "$(dirname "$0")"

# Git Bash would rewrite /data/tls as a Windows path; a no-op everywhere else
export MSYS_NO_PATHCONV=1
ROOT_PEM=certs/mcp-manager-root.pem
network='' https='' trust=yes yes='' port='' names='' trust_only=''

say() { printf '%s\n' "$*"; }
die() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) yes=1 ;;
    --network) network=yes ;;
    --https) https=yes ;;
    --http) https=no ;;
    --no-trust) trust=no ;;
    --name) [ $# -ge 2 ] || die "--name needs a host name"; names="$names $2"; shift ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; port=$2; shift ;;
    --trust) [ $# -ge 2 ] || die "--trust needs a file"; trust_only=$2; shift ;;
    -h|--help) sed -n '2,14p' "$0" | cut -c3-; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

# ask "question" y|n: the answer, or the default when there is nobody to ask
ask() {
  if [ -n "$yes" ] || [ ! -t 0 ]; then [ "$2" = y ]; return; fi
  if [ "$2" = y ]; then choices='[Y/n]'; else choices='[y/N]'; fi
  printf '%s %s ' "$1" "$choices"
  read -r reply || reply=''
  case "$reply" in
    [Yy]*) return 0 ;;
    [Nn]*) return 1 ;;
    *) [ "$2" = y ] ;;
  esac
}

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

# set_env KEY VALUE: replaces the line, or the commented-out example of it, or adds one
set_env() {
  awk -v k="$1" -v v="$2" '
    $0 ~ "^#? *" k "=" { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env > .env.new
  mv .env.new .env
  chmod 600 .env
}

drop_env() {
  awk -v k="$1" '$0 ~ "^" k "=" { print "# " $0; next } { print }' .env > .env.new
  mv .env.new .env
  chmod 600 .env
}

# the names and addresses other devices will use; localhost is always covered
cert_names() {
  for n in $names; do say "$n"; done
  [ "$network" = yes ] || return 0
  host=$(hostname 2>/dev/null || true)
  if [ -n "$host" ]; then
    say "$host"
    say "${host%%.*}.local"
  fi
  case "$(uname -s)" in
    Darwin) ifconfig 2>/dev/null | awk '/inet /{print $2}' ;;
    *) hostname -I 2>/dev/null | tr ' ' '\n' ;;
  esac | grep -E '^[0-9.]+$' | grep -v -E '^(127\.|169\.254\.)' || true
}

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

trust_store() {
  case "$(uname -s)" in
    Darwin)
      security add-trusted-cert -r trustRoot -p ssl -k "$HOME/Library/Keychains/login.keychain-db" "$1" ;;
    Linux)
      if command -v update-ca-certificates >/dev/null 2>&1; then
        as_root cp "$1" /usr/local/share/ca-certificates/mcp-manager.crt
        as_root update-ca-certificates
      elif command -v update-ca-trust >/dev/null 2>&1; then
        as_root cp "$1" /etc/pki/ca-trust/source/anchors/mcp-manager.pem
        as_root update-ca-trust
      elif command -v trust >/dev/null 2>&1; then
        as_root trust anchor --store "$1"
      else
        return 1
      fi ;;
    *) return 1 ;;
  esac
}

# Node apps, Claude Code among them, read their own list of roots, not the system's
trust_node() {
  path="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
  case "${SHELL:-}" in
    *zsh) rc="$HOME/.zshrc" ;;
    *bash) rc="$HOME/.bashrc" ;;
    *) rc="$HOME/.profile" ;;
  esac
  line="export NODE_EXTRA_CA_CERTS=\"$path\""
  if grep -qs 'NODE_EXTRA_CA_CERTS=' "$rc"; then
    grep -qsF "$line" "$rc" || say "NODE_EXTRA_CA_CERTS is already set in $rc, so it stays. Node apps need it to name $path."
    return 0
  fi
  printf '\n# MCP-Manager: Node apps read this instead of the system store\n%s\n' "$line" >> "$rc"
  say "Node apps will trust it too, in new terminals ($rc)."
}

trust() {
  if trust_store "$1"; then
    say "This computer now trusts $1."
    trust_node "$1"
  else
    say "Could not add it to this computer's trust store. Apps will refuse the connection until you"
    say "trust $1 by hand: see \"HTTPS on your own network\" in the README."
  fi
}

if [ -n "$trust_only" ]; then
  [ -f "$trust_only" ] || die "no such file: $trust_only"
  trust "$trust_only"
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "Docker is not installed. Get it from https://docs.docker.com/get-docker/, or see \"Without Docker\" in the README."
docker compose version >/dev/null 2>&1 || die "Docker Compose is missing: install the compose plugin."
docker info >/dev/null 2>&1 || die "Docker is not running, or this user may not use it. Start it, or add yourself to the docker group, and run this again."

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi
token=$(env_value MCPM_ADMIN_TOKEN)
new_token=''
if [ -z "$token" ]; then
  token="mcpm_admin_$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
  set_env MCPM_ADMIN_TOKEN "$token"
  new_token=1
fi

if [ -z "$network" ]; then
  if ask "Should other devices on your network reach it? Only this computer is the safest." n; then
    network=yes
  else
    network=no
  fi
fi
if [ -z "$https" ]; then
  if [ "$network" = yes ]; then
    if ask "Use HTTPS? Recommended: app keys then cross your network encrypted." y; then https=yes; else https=no; fi
  else
    if ask "Use HTTPS? Not needed on this computer alone, and some apps refuse a private certificate." n; then https=yes; else https=no; fi
  fi
fi

if [ "$network" = yes ]; then set_env MCPM_PUBLISH 0.0.0.0; else set_env MCPM_PUBLISH 127.0.0.1; fi
[ -z "$port" ] || set_env MCPM_PORT "$port"
port=$(env_value MCPM_PORT)
port=${port:-8899}

docker compose build

scheme=http
if [ "$https" = yes ]; then
  scheme=https
  mkdir -p certs
  # shellcheck disable=SC2046  # one word per name is the point
  docker compose run --rm -T mcp-manager python -m mcp_manager.tls --dir /data/tls $(cert_names) \
    | sed -n '/BEGIN CERTIFICATE/,/END CERTIFICATE/p' > "$ROOT_PEM"
  [ -s "$ROOT_PEM" ] || die "the certificate was not made: see the lines above."
  set_env MCPM_TLS_CERT /data/tls/tls-cert.pem
  set_env MCPM_TLS_KEY /data/tls/tls-key.pem
else
  # a certificate of your own, set by hand, is yours to remove
  case "$(env_value MCPM_TLS_CERT)" in
    /data/tls/*) drop_env MCPM_TLS_CERT; drop_env MCPM_TLS_KEY ;;
  esac
fi

# Recreated even if no setting changed: the running manager holds the old certificate in memory.
# Three tries, because Docker can take a moment to let go of a port it was just publishing.
started=''
for attempt in 1 2 3; do
  if docker compose up -d --force-recreate; then started=1; break; fi
  [ "$attempt" = 3 ] || sleep 3
done
[ -n "$started" ] || die "it did not start: see the lines above."

tries=0
until docker compose exec -T mcp-manager python -c "import socket; socket.create_connection(('127.0.0.1', 8899), 2)" >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || die "it did not start within a minute: docker compose logs mcp-manager"
  sleep 1
done

if [ "$https" = yes ] && [ "$trust" = yes ]; then
  say ""
  say "To connect without warnings, this computer has to trust $ROOT_PEM."
  say "Its private key is gone, so it can vouch for this manager and nothing else."
  if ask "Trust it now? You may be asked for your password." y; then
    trust "$ROOT_PEM"
  fi
fi

say ""
say "MCP-Manager is running."
say "  Admin panel:   $scheme://localhost:$port/"
say "  MCP endpoint:  $scheme://localhost:$port/mcp"
if [ -n "$new_token" ]; then
  say "  Admin token:   $token"
  say "                 (kept in .env; this is the only time it is shown)"
else
  say "  Admin token:   the one in .env"
fi
if [ "$network" = yes ]; then
  say "  Reachable from your network too."
else
  say "  Reachable from this computer only. To change that: ./install.sh --network"
fi
[ "$https" != yes ] || say "  Other devices need the certificate: see \"HTTPS on your own network\" in the README."
say ""
say "Next: open the admin panel, add your MCP servers, and create a key for each app."
