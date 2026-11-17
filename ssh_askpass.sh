#!/bin/sh
# OpenSSH reads the dialog's answer directly from this helper's stdout.
exec /usr/bin/osascript - "$1" <<'APPLESCRIPT'
on run argv
    activate
    set sshPrompt to item 1 of argv
    set answerDialog to display dialog sshPrompt with title "Sentinal Eye SSH" default answer "" with hidden answer buttons {"Cancel", "Connect"} default button "Connect" cancel button "Cancel" giving up after 60
    if gave up of answerDialog then error number -128
    return text returned of answerDialog
end run
APPLESCRIPT
