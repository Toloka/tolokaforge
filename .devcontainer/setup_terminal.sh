#!/bin/bash

set -xeuo pipefail

if [[ "$USER" == "root" ]] ; then
    export HOME=/root
else
    export HOME="/home/${USER}"
fi

# fix bat: https://github.com/sharkdp/bat?tab=readme-ov-file#on-ubuntu-using-apt
mkdir -p "$HOME/.local/bin"
ln -s /usr/bin/batcat "$HOME/.local/bin/bat"

# install oh-my-zsh
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
cp /devcontainer_bootstrap/.zshrc "$HOME/"

# install fzf
git clone --depth 1 https://github.com/junegunn/fzf.git "$HOME/.fzf"
"$HOME/.fzf/install"
