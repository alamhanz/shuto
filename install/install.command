#!/bin/sh
# Double-click in Finder to install shuto from this folder (macOS).
cd "$(dirname "$0")" || exit 1
sh ./install.sh "$@"
status=$?
echo
if [ "$status" -ne 0 ]; then
    echo "The install did not finish. Read the message above."
fi
printf 'Press Return to close this window. '
read -r _
exit "$status"
