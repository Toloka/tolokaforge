#!/bin/sh
# Install one coding-harness CLI into a terminal-bench task image.
#
#   install-harness.sh <install_method> <install_source> <version>
#
# Runs inside the harness image layer the adapter synthesises on top of a
# task's base image; the three arguments are the `HarnessSpec` fields of the
# same name, resolved from the adapter's harness registry. A missing argument,
# an unknown method, or a version that cannot be recorded aborts the build
# rather than producing an image whose missing or drifting CLI would surface
# as a trial-time failure.
#
# Method contracts:
#
#   npm        <source> is a global npm package. Node 20 LTS is installed
#              first when the base image lacks a modern enough one.
#   pip        <source> is a PyPI distribution, installed with `pip install
#              --no-cache-dir`. python3 + pip are installed when absent.
#   curl-bash  <source> is an installer script URL. It is downloaded, then run
#              as `sh <script> --version <version>` — an installer wired to
#              this method must accept that flag, since the version is what
#              the benchmark record names.
#   binary     <source> is a URL to either a `.tar.gz` / `.tgz` whose members
#              are executables at the archive root, or a bare executable. Both
#              land in $TOLOKAFORGE_HARNESS_BIN_DIR, the latter named after the
#              URL's last path segment.
#
# `latest` is accepted by npm and pip, which can be asked afterwards what they
# resolved; curl-bash and binary refuse it, because nothing here can report
# what such an installer chose and an unrecorded agent version is not a
# benchmark result. The resolved version is written to
# $TOLOKAFORGE_HARNESS_STATE_DIR/installed-version.txt for the container to
# carry.
#
# POSIX sh on purpose: the base image is the task's, and a task is free to
# ship one without bash.
set -eu

METHOD="${1:-}"
SOURCE="${2:-}"
VERSION="${3:-}"

STATE_DIR="${TOLOKAFORGE_HARNESS_STATE_DIR:-/opt/tolokaforge}"
BIN_DIR="${TOLOKAFORGE_HARNESS_BIN_DIR:-/usr/local/bin}"
NODE_MAJOR=20

fail() {
    echo "install-harness.sh: $1" >&2
    exit 1
}

[ -n "$METHOD" ] || fail "no install method given (argument 1)"
[ -n "$SOURCE" ] || fail "no install source given for method '${METHOD}' (argument 2)"
[ -n "$VERSION" ] || fail "no version given for '${SOURCE}' (argument 3); the agent version is part of a benchmark result and must be pinned"

record_version() {
    [ -n "$1" ] || fail "could not determine the version of '${SOURCE}' that '${METHOD}' installed"
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$1" > "$STATE_DIR/installed-version.txt"
}

install_os_packages() {
    # install_os_packages <apt package list> <apk package list>
    # Unquoted on purpose: each argument is a space-separated package list.
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        apt-get update
        apt-get install -y --no-install-recommends $1
        rm -rf /var/lib/apt/lists/*
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache $2
    else
        fail "install method '${METHOD}' needs '$1' and neither apt-get nor apk is available to install it"
    fi
}

ensure_command() {
    # ensure_command <command> <apt package list> <apk package list>
    if command -v "$1" >/dev/null 2>&1; then
        return 0
    fi
    install_os_packages "$2" "$3"
    command -v "$1" >/dev/null 2>&1 || fail "'$1' is still missing after installing '$2'"
}

ensure_node() {
    # Modern coding-harness CLIs (claude-code, codex, gemini-cli) use optional
    # chaining and other ES2020+ syntax in their install scripts. Ubuntu
    # 22.04's apt `nodejs` package ships Node 12, which parses those as
    # SyntaxError. Pull Node 20 LTS from NodeSource so the build succeeds
    # regardless of what the task's base image ships.
    if command -v node >/dev/null 2>&1 &&
        node -e "process.exit(parseInt(process.versions.node.split('.')[0]) >= 18 ? 0 : 1)" 2>/dev/null; then
        return 0
    fi
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        apt-get update
        apt-get install -y --no-install-recommends ca-certificates curl gnupg
        mkdir -p /etc/apt/keyrings
        curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
            | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
        echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
            > /etc/apt/sources.list.d/nodesource.list
        apt-get update
        apt-get install -y --no-install-recommends nodejs
        rm -rf /var/lib/apt/lists/*
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "nodejs~${NODE_MAJOR}" npm
    else
        fail "Node ${NODE_MAJOR}+ is required and neither apt-get nor apk is available to install it"
    fi
}

resolve_pip() {
    for candidate in pip pip3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PIP="$candidate"
            return 0
        fi
    done
    return 1
}

download() {
    # download <url> <destination>
    ensure_command curl "ca-certificates curl" "ca-certificates curl"
    # Written to a file rather than piped onward: POSIX sh has no `pipefail`,
    # so a failed download feeding a pipeline would leave the build green with
    # nothing installed.
    curl -fsSL "$1" -o "$2"
}

install_npm() {
    ensure_node
    npm install -g "${SOURCE}@${VERSION}"
    if [ "$VERSION" = "latest" ]; then
        record_version "$(node -p "require('$(npm root -g)/${SOURCE}/package.json').version")"
    else
        record_version "$VERSION"
    fi
}

install_pip() {
    resolve_pip || {
        install_os_packages "python3 python3-pip" "python3 py3-pip"
        resolve_pip || fail "pip is still missing after installing python3-pip"
    }
    if [ "$VERSION" = "latest" ]; then
        "$PIP" install --no-cache-dir "$SOURCE"
        record_version "$("$PIP" show "$SOURCE" | sed -n 's/^Version: //p')"
    else
        "$PIP" install --no-cache-dir "${SOURCE}==${VERSION}"
        record_version "$VERSION"
    fi
}

install_curl_bash() {
    # Installer scripts published under ``curl … | bash`` conventions are
    # bash, not POSIX sh — they routinely use ``[[ … ]]``, arrays, and
    # ``process substitution``. Running with ``/bin/sh`` produces
    # ``Syntax error: "(" unexpected`` on installers like x.ai's.
    ensure_command bash bash bash
    installer=/tmp/harness-installer.sh
    download "$SOURCE" "$installer"
    # Positional version arg (``bash installer.sh <version>``) matches the
    # ``curl … | bash -s -- <version>`` shape most installers document. If
    # a specific installer needs ``--version`` instead, its harness entry
    # should ship a ``pre_exec_shell`` that rewrites the invocation — the
    # generic branch stays predictable.
    bash "$installer" "$VERSION"
    rm -f "$installer"
    record_version "$VERSION"
}

install_binary() {
    archive=/tmp/harness-download
    download "$SOURCE" "$archive"
    mkdir -p "$BIN_DIR"
    case "$SOURCE" in
        *.tar.gz | *.tgz)
            ensure_command tar tar tar
            tar -xzf "$archive" -C "$BIN_DIR"
            rm -f "$archive"
            ;;
        *)
            name="${SOURCE%%\?*}"
            name="${name##*/}"
            [ -n "$name" ] || fail "install_source '${SOURCE}' has no filename to install as"
            mv "$archive" "${BIN_DIR}/${name}"
            chmod +x "${BIN_DIR}/${name}"
            ;;
    esac
    record_version "$VERSION"
}

reject_floating_version() {
    [ "$VERSION" != "latest" ] ||
        fail "install method '${METHOD}' cannot report what 'latest' resolved to; pin a version"
}

case "$METHOD" in
    npm) install_npm ;;
    pip) install_pip ;;
    curl-bash)
        reject_floating_version
        install_curl_bash
        ;;
    binary)
        reject_floating_version
        install_binary
        ;;
    *) fail "unknown install method '${METHOD}'; expected one of: npm, pip, curl-bash, binary" ;;
esac
