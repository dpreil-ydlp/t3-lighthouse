#!/usr/bin/env bash
# Tests for bin/lighthouse: uninstall safety, collector ownership, Compose project isolation, and
# tokens kept off the curl command line. docker, launchctl, curl, uname, and python are stubs, and
# every run uses a throwaway HOME, so nothing here touches Docker, launchd, or your own files.
#
#   bash tests/installer/run.sh             # tests this checkout
#   LIGHTHOUSE_SRC=/other/checkout bash tests/installer/run.sh
set -euo pipefail

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SRC=${LIGHTHOUSE_SRC:-$(CDPATH='' cd -- "$HERE/../.." && pwd)}
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/lighthouse-test.XXXXXX")
SCRATCH=$(CDPATH='' cd -- "$SCRATCH" && pwd -P)
trap 'rm -rf "$SCRATCH"' EXIT
STUBS=$SCRATCH/stubs
PASSED=0
FAILED=0

mkdir -p "$STUBS"

# docker: logs every call. STUB_DOCKER_UP=0 makes `docker info` fail. STUB_OTHER_PROJECT and
# STUB_OTHER_CONFIG make that project look like it was started from STUB_OTHER_CONFIG.
# Volumes live in the file STUB_VOLUMES, one "project name created-at" per line: `compose up`
# creates the project's database volume, `compose down --volumes` deletes it.
cat > "$STUBS/docker" <<'STUB'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "$STUB_LOG"
case "$1" in
  info) [ "${STUB_DOCKER_UP:-1}" = 1 ] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }; exit 0 ;;
  ps)
    for arg in "$@"; do
      [ "$arg" = "label=com.docker.compose.project=${STUB_OTHER_PROJECT:-}" ] && [ -n "${STUB_OTHER_PROJECT:-}" ] && echo "$STUB_OTHER_CONFIG"
    done
    exit 0 ;;
  images) exit 0 ;;
  volume)
    case "$2" in
      ls) for arg in "$@"; do
            case "$arg" in label=com.docker.compose.project=*) awk -v p="${arg#*=*=}" '$1 == p { print $2 }' "$STUB_VOLUMES" ;; esac
          done ;;
      inspect) shift 2
        while [ "$#" -gt 0 ]; do
          case "$1" in --format) shift ;; *) awk -v n="$1" '$2 == n { print $2, $3 }' "$STUB_VOLUMES" ;; esac
          shift
        done ;;
    esac
    exit 0 ;;
  compose) ;;
  *) exit 0 ;;
esac
shift
dir= project=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) project=$2; shift ;;
    --env-file|-f) shift ;;
    --project-directory) dir=$2; shift ;;
    version) exit 0 ;;
    config) echo "ghcr.io/example/t3-lighthouse-api:latest"; echo "postgres:17.11-alpine"; exit 0 ;;
    ps) echo healthy; exit 0 ;;
    up)
      grep -q "^$project " "$STUB_VOLUMES" || echo "$project ${project}_lighthouse-db $(date -u +%Y-%m-%dT%H:%M:%SZ)-$RANDOM" >> "$STUB_VOLUMES"
      exit 0 ;;
    down)
      case " $* " in *" --volumes "*) awk -v p="$project" '$1 != p' "$STUB_VOLUMES" > "$STUB_VOLUMES.new"; mv "$STUB_VOLUMES.new" "$STUB_VOLUMES" ;; esac
      exit 0 ;;
    exec)
      # admin-entrypoint ... --token-file /host-secrets/NAME writes a token into the secrets folder.
      while [ "$#" -gt 0 ]; do
        if [ "$1" = --token-file ]; then
          name=$(basename "$2")
          printf 'tok-%s-%s' "${name%-token}" "$RANDOM$RANDOM" > "$dir/secrets/$name"
        fi
        shift
      done
      exit 0 ;;
    *) exit 0 ;;
  esac
  shift
done
STUB

# curl: logs its arguments and answers /healthz and /api/v1/me. A token that starts with
# tok-agent belongs to an agent, any other to the owner. Headers may come from stdin (-H @-).
cat > "$STUBS/curl" <<'STUB'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$STUB_LOG"
url= header= out=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -H) if [ "$2" = @- ]; then header=$(cat); else header=$2; fi; shift ;;
    -o) out=$2; shift ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
case "$url" in
  */healthz) echo ok ;;
  */api/v1/me)
    case "$header" in
      *"Bearer tok-agent"*) echo '{"kind":"agent"}' ;;
      *"Bearer tok-"*) echo '{"kind":"user"}' ;;
      *) exit 22 ;;
    esac ;;
  *) [ -z "$out" ] || : > "$out"; exit 22 ;;
esac
STUB

cat > "$STUBS/launchctl" <<'STUB'
#!/usr/bin/env bash
printf 'launchctl %s\n' "$*" >> "$STUB_LOG"
STUB

cat > "$STUBS/uname" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = -s ]; then echo "${STUB_UNAME:-Linux}"; else exec /usr/bin/uname "$@"; fi
STUB

# Python 3.11+ for the collector checks; the tests never run the collector itself.
cat > "$STUBS/python3.12" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

# install-mac-collector.sh uses BSD `stat -f %Lp`; translate it on Linux.
if [ "$(/usr/bin/uname -s)" != Darwin ]; then
  cat > "$STUBS/stat" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = -f ] && [ "$2" = %Lp ]; then exec /usr/bin/stat -c %a "$3"; fi
exec /usr/bin/stat "$@"
STUB
fi
chmod +x "$STUBS"/*

# A fresh throwaway HOME for each test.
new_home() {
  T_HOME=$SCRATCH/home-$1
  mkdir -p "$T_HOME"
  T_LOG=$SCRATCH/log-$1
  : > "$T_LOG"
  T_VOLUMES=$SCRATCH/volumes-$1
  : > "$T_VOLUMES"
  ln -sf "$T_LOG.out" "$SCRATCH/out-$CURRENT"
}

# copy_install DIR: a copy of the scripts as an install folder, with no config yet.
copy_install() {
  local dir=$1
  mkdir -p "$dir/collector"
  cp -R "$SRC/bin" "$SRC/scripts" "$dir/"
  cp "$SRC/docker-compose.yml" "$SRC/docker-compose.build.yml" "$dir/"
  : > "$dir/collector/collector.py"
}

# fake_server_install DIR PROJECT: an install folder as the installer leaves it.
fake_server_install() {
  local dir=$1
  copy_install "$dir"
  mkdir -m 700 "$dir/secrets"
  printf 'COMPOSE_PROJECT_NAME=%s\nLIGHTHOUSE_API_PORT=51821\nLIGHTHOUSE_DASHBOARD_PORT=51823\n' "$2" > "$dir/.env"
  printf 'tok-owner-%s' "$RANDOM" > "$dir/secrets/owner-token"
  printf 'tok-agent-%s' "$RANDOM" > "$dir/secrets/agent-token"
}

# lh DIR ARGS...: run DIR/bin/lighthouse with the stubs, the throwaway HOME, and no terminal.
lh() {
  local dir=$1; shift
  case "$T_HOME" in "$SCRATCH"/*) ;; *) echo "refusing to run outside the scratch HOME" >&2; exit 1 ;; esac
  env -i HOME="$T_HOME" PATH="$STUBS:/usr/bin:/bin:/usr/sbin:/sbin" TMPDIR="$SCRATCH" \
    STUB_LOG="$T_LOG" STUB_VOLUMES="$T_VOLUMES" STUB_UNAME="${STUB_UNAME:-Linux}" STUB_DOCKER_UP="${STUB_DOCKER_UP:-1}" \
    STUB_OTHER_PROJECT="${STUB_OTHER_PROJECT:-}" STUB_OTHER_CONFIG="${STUB_OTHER_CONFIG:-}" \
    ${EXTRA_ENV:+"$EXTRA_ENV"} LIGHTHOUSE_PYTHON=python3.12 \
    bash "$dir/bin/lighthouse" "$@" < /dev/null > "$T_LOG.out" 2>&1
}

# lh_script DIR ARGS...: run DIR/scripts/install-mac-collector.sh the same way.
lh_script() {
  local dir=$1; shift
  case "$T_HOME" in "$SCRATCH"/*) ;; *) echo "refusing to run outside the scratch HOME" >&2; exit 1 ;; esac
  env -i HOME="$T_HOME" PATH="$STUBS:/usr/bin:/bin:/usr/sbin:/sbin" STUB_LOG="$T_LOG" LIGHTHOUSE_PYTHON=python3.12 \
    sh "$dir/scripts/install-mac-collector.sh" "$@" < /dev/null >> "$T_LOG.out" 2>&1
}

CURRENT=
run_test() {
  CURRENT=$1
  if ( "$1" ) && [ ! -f "$SCRATCH/failed-$1" ]; then
    echo "ok - $1"
    PASSED=$((PASSED + 1))
  else
    echo "not ok - $1"
    sed 's/^/      | /' "$SCRATCH/out-$1" 2>/dev/null || true
    FAILED=$((FAILED + 1))
  fi
}

# Each test runs in a subshell; a failed check marks the test and the test keeps going.
expect() { # expect "description" command...
  if ! "${@:2}"; then
    echo "    FAILED: $1"
    : > "$SCRATCH/failed-$CURRENT"
  fi
}
not() { ! "$@"; }
has() { grep -qF -- "$2" "$1"; }

# With Docker stopped, uninstall must keep every file and exit non-zero.
test_uninstall_keeps_everything_when_docker_is_down() {
  new_home a1-down
  local dir=$T_HOME/t3-lighthouse status=0
  fake_server_install "$dir" t3-lighthouse
  STUB_DOCKER_UP=0 lh "$dir" uninstall --yes || status=$?
  expect "exit status is non-zero" [ "$status" != 0 ]
  expect "the install folder is kept" [ -f "$dir/.env" ]
  expect "the tokens are kept" [ -s "$dir/secrets/owner-token" ]
  expect "nothing ran compose down" not has "$T_LOG" "down"
  expect "the message says nothing was deleted" has "$T_LOG.out" "Nothing was deleted"
}

# With Docker running, uninstall removes the stack, then the folder.
test_uninstall_removes_stack_then_folder_when_docker_is_up() {
  new_home a1-up
  local dir=$T_HOME/t3-lighthouse status=0
  fake_server_install "$dir" t3-lighthouse
  lh "$dir" uninstall --yes || status=$?
  expect "exit status is zero" [ "$status" = 0 ]
  expect "compose down ran with --volumes" has "$T_LOG" "down --volumes --remove-orphans"
  expect "the install folder is gone" [ ! -e "$dir" ]
}

# A collector, briefs job, and settings that were on this Mac before a --no-collector install.
pre_existing_collector() {
  local agents=$T_HOME/Library/LaunchAgents conf=$T_HOME/.config/lighthouse
  mkdir -p "$agents" "$conf/secrets"
  printf '<plist><dict><key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>/Users/someone/lighthouse/collector/collector.py</string></array></dict></plist>\n' > "$agents/ai.lighthouse.collector.plist"
  printf '<plist><dict><key>Label</key><string>ai.lighthouse.briefs</string></dict></plist>\n' > "$agents/ai.lighthouse.briefs.plist"
  printf 'tok-agent-earlier' > "$conf/secrets/agent-token"
  printf 't3-token' > "$conf/secrets/t3-mcp-token"
  printf '{}' > "$conf/collector-state.json"
}

# Uninstalling an install made with --no-collector keeps a collector it did not create.
test_uninstall_keeps_a_collector_it_did_not_install() {
  new_home a2-keep
  local dir=$T_HOME/t3-lighthouse status=0 conf=$T_HOME/.config/lighthouse
  pre_existing_collector
  fake_server_install "$dir" t3-lighthouse
  STUB_UNAME=Darwin lh "$dir" uninstall --yes || status=$?
  expect "exit status is zero" [ "$status" = 0 ]
  expect "the collector LaunchAgent is kept" [ -f "$T_HOME/Library/LaunchAgents/ai.lighthouse.collector.plist" ]
  expect "the briefs LaunchAgent is kept" [ -f "$T_HOME/Library/LaunchAgents/ai.lighthouse.briefs.plist" ]
  expect "launchctl was not asked to stop anything" not has "$T_LOG" "launchctl bootout"
  expect "the collector's agent token is kept" [ -s "$conf/secrets/agent-token" ]
  expect "the T3 token is kept" [ -s "$conf/secrets/t3-mcp-token" ]
  expect "the collector state is kept" [ -f "$conf/collector-state.json" ]
  expect "the output says the collector was kept" has "$T_LOG.out" "Kept LaunchAgent ai.lighthouse.collector"
  expect "the install folder is gone" [ ! -e "$dir" ]
}

# A collector this install set up is removed; files the user made are kept.
# The tokens never appear on a curl command line.
test_uninstall_removes_only_the_collector_it_installed() {
  new_home a2-own
  local dir=$T_HOME/t3-lighthouse status=0 conf=$T_HOME/.config/lighthouse agents=$T_HOME/Library/LaunchAgents
  mkdir -p "$agents" "$conf/secrets"
  printf '<plist><dict><key>Label</key><string>ai.lighthouse.briefs</string></dict></plist>\n' > "$agents/ai.lighthouse.briefs.plist"
  printf 't3-token' > "$conf/secrets/t3-mcp-token"
  chmod 700 "$conf/secrets"
  fake_server_install "$dir" t3-lighthouse
  STUB_UNAME=Darwin lh "$dir" collector || status=$?
  expect "the collector installs" [ "$status" = 0 ]
  expect "the collector LaunchAgent runs this install's collector" has "$agents/ai.lighthouse.collector.plist" "$dir/collector/collector.py"
  expect "the agent token was not on a curl command line" not has "$T_LOG" "$(cat "$dir/secrets/agent-token")"
  printf '{}' > "$conf/collector-state.json"
  printf 'log\n' > "$conf/collector.log"
  status=0
  STUB_UNAME=Darwin lh "$dir" uninstall --yes || status=$?
  expect "exit status is zero" [ "$status" = 0 ]
  expect "the collector LaunchAgent is removed" [ ! -e "$agents/ai.lighthouse.collector.plist" ]
  expect "launchctl stopped the collector" has "$T_LOG" "launchctl bootout gui/$(id -u)/ai.lighthouse.collector"
  expect "the collector's agent token is removed" [ ! -e "$conf/secrets/agent-token" ]
  expect "the collector state is removed" [ ! -e "$conf/collector-state.json" ]
  expect "the briefs LaunchAgent is kept" [ -f "$agents/ai.lighthouse.briefs.plist" ]
  expect "the T3 token is kept" [ -s "$conf/secrets/t3-mcp-token" ]
}

# Another folder's collector, installed with its own script after this install's, owns the shared
# token, state, and log: uninstalling this install keeps them.
test_uninstall_keeps_the_files_of_a_collector_that_replaced_its_own() {
  new_home a2-replaced
  local first=$T_HOME/t3-lighthouse second=$T_HOME/other status=0 conf=$T_HOME/.config/lighthouse
  local plist=$T_HOME/Library/LaunchAgents/ai.lighthouse.collector.plist
  fake_server_install "$first" t3-lighthouse
  fake_server_install "$second" other
  STUB_UNAME=Darwin lh "$first" collector || status=$?
  expect "the first collector installs" [ "$status" = 0 ]
  status=0
  lh_script "$second" --host 127.0.0.1 --port 51821 --replace-collector || status=$?
  expect "the second folder's script replaces it" [ "$status" = 0 ]
  expect "the LaunchAgent runs the second folder's collector" has "$plist" "<string>$second/collector/collector.py</string>"
  expect "the marker names the second folder" [ "$(cat "$conf/collector-installed-by" 2>/dev/null)" = "$second" ]
  printf '{}' > "$conf/collector-state.json"
  printf 'log\n' > "$conf/collector.log"
  status=0
  STUB_UNAME=Darwin lh "$first" uninstall --yes || status=$?
  expect "uninstalling the first exits zero" [ "$status" = 0 ]
  expect "the second folder's LaunchAgent is kept" has "$plist" "$second/collector/collector.py"
  expect "the agent token is kept" [ -s "$conf/secrets/agent-token" ]
  expect "the collector state is kept" [ -f "$conf/collector-state.json" ]
  expect "the collector log is kept" [ -f "$conf/collector.log" ]
  expect "the marker still names the second folder" [ "$(cat "$conf/collector-installed-by" 2>/dev/null)" = "$second" ]
  status=0
  STUB_UNAME=Darwin lh "$second" uninstall --yes || status=$?
  expect "uninstalling the second exits zero" [ "$status" = 0 ]
  expect "the second uninstall removes the LaunchAgent" [ ! -e "$plist" ]
  expect "the second uninstall removes the agent token" [ ! -e "$conf/secrets/agent-token" ]
  expect "the second uninstall removes the collector state" [ ! -e "$conf/collector-state.json" ]
}

# A marker naming this folder is not enough: when the LaunchAgent runs another folder's collector,
# the token, state, and log are that collector's and are kept.
test_uninstall_keeps_collector_files_when_the_launchagent_runs_another_folder() {
  new_home a2-stale
  local dir=$T_HOME/t3-lighthouse status=0 conf=$T_HOME/.config/lighthouse
  pre_existing_collector
  fake_server_install "$dir" t3-lighthouse
  printf '%s\n' "$dir" > "$conf/collector-installed-by"
  printf 'log\n' > "$conf/collector.log"
  STUB_UNAME=Darwin lh "$dir" uninstall --yes || status=$?
  expect "exit status is zero" [ "$status" = 0 ]
  expect "the collector LaunchAgent is kept" [ -f "$T_HOME/Library/LaunchAgents/ai.lighthouse.collector.plist" ]
  expect "the agent token is kept" [ "$(cat "$conf/secrets/agent-token" 2>/dev/null)" = tok-agent-earlier ]
  expect "the collector state is kept" [ -f "$conf/collector-state.json" ]
  expect "the collector log is kept" [ -f "$conf/collector.log" ]
  expect "the output says the files were kept" has "$T_LOG.out" "Kept the collector token, state, and log"
}

# Installing a collector over another folder's needs a yes or --replace-collector, on every path.
test_collector_install_does_not_replace_another_folders_collector_silently() {
  new_home a2-ask
  local dir=$T_HOME/t3-lighthouse status=0 conf=$T_HOME/.config/lighthouse
  local plist=$T_HOME/Library/LaunchAgents/ai.lighthouse.collector.plist
  pre_existing_collector
  fake_server_install "$dir" t3-lighthouse
  STUB_UNAME=Darwin lh "$dir" collector || status=$?
  expect "lighthouse collector exits non-zero" [ "$status" != 0 ]
  expect "it names the flag" has "$T_LOG.out" "--replace-collector"
  status=0
  STUB_UNAME=Darwin lh "$dir" install --with-collector || status=$?
  expect "install --with-collector exits non-zero" [ "$status" != 0 ]
  status=0
  lh_script "$dir" --host 127.0.0.1 --port 51821 --token-file "$dir/secrets/agent-token" || status=$?
  expect "the script exits non-zero" [ "$status" != 0 ]
  expect "the other LaunchAgent is unchanged" has "$plist" "/Users/someone/lighthouse/collector/collector.py"
  expect "the other agent token is unchanged" [ "$(cat "$conf/secrets/agent-token")" = tok-agent-earlier ]
  expect "no marker was written" [ ! -e "$conf/collector-installed-by" ]
  expect "launchctl was not called" not has "$T_LOG" "launchctl"
  status=0
  STUB_UNAME=Darwin lh "$dir" collector --replace-collector || status=$?
  expect "with --replace-collector it installs" [ "$status" = 0 ]
  expect "the LaunchAgent runs this install's collector" has "$plist" "<string>$dir/collector/collector.py</string>"
  expect "the marker names this install" [ "$(cat "$conf/collector-installed-by" 2>/dev/null)" = "$dir" ]
  expect "the agent token is this install's" cmp -s "$conf/secrets/agent-token" "$dir/secrets/agent-token"
}

# The owner token is not on a curl command line during install either.
# An install outside the default folder gets a Compose project name of its own.
test_install_in_another_folder_gets_its_own_project() {
  new_home f1-name
  local first=$T_HOME/t3-lighthouse second=$T_HOME/Lighthouse\ Two status=0 name
  copy_install "$first"
  copy_install "$second"
  lh "$first" install --no-collector || status=$?
  expect "the first install succeeds" [ "$status" = 0 ]
  expect "the default folder uses the project t3-lighthouse" has "$first/.env" "COMPOSE_PROJECT_NAME=t3-lighthouse"
  expect "the owner token was not on a curl command line" not has "$T_LOG" "$(cat "$first/secrets/owner-token" 2>/dev/null || echo missing)"
  status=0
  STUB_OTHER_PROJECT=t3-lighthouse STUB_OTHER_CONFIG="$first/docker-compose.yml" lh "$second" install --no-collector || status=$?
  expect "the second install succeeds" [ "$status" = 0 ]
  name=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$second/.env" 2>/dev/null)
  expect "the second install has another project name (got '$name')" [ "${name:-t3-lighthouse}" != t3-lighthouse ]
  expect "the second install's compose calls use that name" has "$T_LOG" "compose -p $name --project-directory $second"
  expect "the second install's name is a valid Compose name" not grep -q '[^a-z0-9_-]' <<<"$name"
}

# A project name already used by another folder is refused, by install and by uninstall.
test_install_and_uninstall_refuse_another_installs_project() {
  new_home f1-refuse
  local first=$T_HOME/t3-lighthouse second=$T_HOME/second status=0
  copy_install "$second"
  STUB_OTHER_PROJECT=t3-lighthouse STUB_OTHER_CONFIG="$first/docker-compose.yml" \
    EXTRA_ENV=COMPOSE_PROJECT_NAME=t3-lighthouse lh "$second" install --no-collector || status=$?
  expect "install exits non-zero" [ "$status" != 0 ]
  expect "install started no containers" not has "$T_LOG" " up "
  expect "install explains the clash" has "$T_LOG.out" "belongs to another install"
  rm -rf "$second"
  : > "$T_LOG"
  fake_server_install "$second" t3-lighthouse
  status=0
  STUB_OTHER_PROJECT=t3-lighthouse STUB_OTHER_CONFIG="$first/docker-compose.yml" lh "$second" uninstall --yes || status=$?
  expect "uninstall exits non-zero" [ "$status" != 0 ]
  expect "uninstall did not run compose down" not has "$T_LOG" "down"
  expect "uninstall kept the folder" [ -f "$second/.env" ]
}

# Another install's database volume, with that install's containers removed by `lighthouse down`,
# is refused by install, up, and uninstall, and is never deleted.
test_install_and_uninstall_refuse_another_installs_volume_without_containers() {
  new_home f1-volume
  local second=$T_HOME/second status=0 foreign="t3-lighthouse t3-lighthouse_lighthouse-db 2026-01-01T00:00:00Z"
  echo "$foreign" > "$T_VOLUMES"
  copy_install "$second"
  EXTRA_ENV=COMPOSE_PROJECT_NAME=t3-lighthouse lh "$second" install --no-collector || status=$?
  expect "install exits non-zero" [ "$status" != 0 ]
  expect "install started no containers" not has "$T_LOG" " up "
  expect "install explains the clash" has "$T_LOG.out" "has no containers, and this install has no record of creating it"
  rm -rf "$second"
  fake_server_install "$second" t3-lighthouse
  status=0
  lh "$second" up || status=$?
  expect "up exits non-zero" [ "$status" != 0 ]
  expect "up started no containers" not has "$T_LOG" " up "
  status=0
  lh "$second" uninstall --yes || status=$?
  expect "uninstall exits non-zero" [ "$status" != 0 ]
  expect "uninstall did not run compose down" not has "$T_LOG" "down"
  expect "uninstall kept the folder" [ -f "$second/.env" ]
  expect "the other install's volume is still there" has "$T_VOLUMES" "$foreign"
}

# An install's own volume stays usable after `lighthouse down`, and uninstall deletes it.
# An install with no record of its volume needs LIGHTHOUSE_VOLUMES_ARE_MINE=1.
test_own_volume_is_recognised_after_down() {
  new_home f1-own
  local dir=$T_HOME/t3-lighthouse status=0
  copy_install "$dir"
  lh "$dir" install --no-collector || status=$?
  expect "install succeeds" [ "$status" = 0 ]
  expect "install recorded its volume" has "$dir/.volumes" "t3-lighthouse_lighthouse-db"
  status=0
  lh "$dir" down || status=$?
  expect "down succeeds" [ "$status" = 0 ]
  status=0
  lh "$dir" up || status=$?
  expect "up after down succeeds" [ "$status" = 0 ]
  rm -f "$dir/.volumes"
  status=0
  lh "$dir" up || status=$?
  expect "up without the record exits non-zero" [ "$status" != 0 ]
  status=0
  EXTRA_ENV=LIGHTHOUSE_VOLUMES_ARE_MINE=1 lh "$dir" up || status=$?
  expect "up with LIGHTHOUSE_VOLUMES_ARE_MINE=1 succeeds" [ "$status" = 0 ]
  expect "up recorded the volume again" has "$dir/.volumes" "t3-lighthouse_lighthouse-db"
  status=0
  lh "$dir" uninstall --yes || status=$?
  expect "uninstall succeeds" [ "$status" = 0 ]
  expect "uninstall ran compose down --volumes" has "$T_LOG" "down --volumes --remove-orphans"
  expect "the volume is gone" not has "$T_VOLUMES" "t3-lighthouse_lighthouse-db"
}

# A postgres image that was on the machine before the install is kept, and the output says so.
test_uninstall_lists_a_postgres_image_it_kept() {
  new_home pg-kept
  local dir=$T_HOME/t3-lighthouse status=0
  fake_server_install "$dir" t3-lighthouse
  lh "$dir" uninstall --yes || status=$?
  expect "exit status is zero" [ "$status" = 0 ]
  expect "postgres was not removed" not has "$T_LOG" "docker image rm postgres"
  expect "the output lists the kept postgres image" has "$T_LOG.out" "kept postgres:17.11-alpine"
}

# Update downloads from LIGHTHOUSE_REPO when the install is not a git checkout.
test_update_downloads_from_the_configured_repo() {
  new_home f5-repo
  local dir=$T_HOME/t3-lighthouse status=0
  fake_server_install "$dir" t3-lighthouse
  printf 'LIGHTHOUSE_REF=main\nLIGHTHOUSE_REPO=someone/fork\n' >> "$dir/.env"
  lh "$dir" update || status=$?
  expect "update asked for the fork's tarball" has "$T_LOG" "https://codeload.github.com/someone/fork/tar.gz/main"
}

for t in \
  test_uninstall_keeps_everything_when_docker_is_down \
  test_uninstall_removes_stack_then_folder_when_docker_is_up \
  test_uninstall_keeps_a_collector_it_did_not_install \
  test_uninstall_removes_only_the_collector_it_installed \
  test_uninstall_keeps_the_files_of_a_collector_that_replaced_its_own \
  test_uninstall_keeps_collector_files_when_the_launchagent_runs_another_folder \
  test_collector_install_does_not_replace_another_folders_collector_silently \
  test_install_in_another_folder_gets_its_own_project \
  test_install_and_uninstall_refuse_another_installs_project \
  test_install_and_uninstall_refuse_another_installs_volume_without_containers \
  test_own_volume_is_recognised_after_down \
  test_uninstall_lists_a_postgres_image_it_kept \
  test_update_downloads_from_the_configured_repo
do
  run_test "$t"
done

echo "$PASSED passed, $FAILED failed"
[ "$FAILED" = 0 ]
