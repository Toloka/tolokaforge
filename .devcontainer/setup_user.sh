#!/bin/bash

set -xeuo pipefail

if [[ "$USER" == "root" ]] ; then
    echo "Current user is root. Do nothing"
    exit 0
fi

groupadd --gid $USER_GID "$USER"
useradd --uid $USER_UID --gid $USER_GID -m "$USER"

# Add sudo support.
echo "$USER" ALL=\(root\) NOPASSWD:ALL > "/etc/sudoers.d/$USER"
chmod 0440 "/etc/sudoers.d/$USER"

# terminal
usermod --shell /bin/zsh "$USER"