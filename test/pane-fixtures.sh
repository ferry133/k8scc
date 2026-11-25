# Pane fixtures for the pane gate's controls (sourced, not run). They were in
# test/inbox-watch-controls.sh until inbox-watch was retired (k8scc#103); the
# text below is unchanged. The *_REAL ones are captures from the image's
# Claude Code (jg-jcc2, 2026-11-10, k8scc#60); the others are hand-written.
# Used by test/chamber-watch-controls.sh.
IDLE_BOX='  ⎿  Done. Next steps:
     1. rotate the key
     2. tell the company

╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
  ? for shortcuts



'
IDLE_RULE='● Restart applied.
────────────────────────────────────────────
❯ 
────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
'
PERMISSION='● Bash(talosctl -n 10.9.9.23 reboot)
╭──────────────────────────────────────────╮
│ Bash command                             │
│                                          │
│   talosctl -n 10.9.9.23 reboot           │
│                                          │
│ Do you want to proceed?                  │
│ ❯ 1. Yes                                 │
│   2. No, and tell Claude what to do      │
╰──────────────────────────────────────────╯
'
MENU_NOPHRASE='╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
 ❯ 1. Yes
   2. No
'
HALF_TYPED='╭──────────────────────────────────────────╮
│ > please check the hom                   │
╰──────────────────────────────────────────╯
  ? for shortcuts
'
SLASH='╭──────────────────────────────────────────╮
│ > /                                      │
╰──────────────────────────────────────────╯
  /help   Show help
'
BUSY='✻ Pondering… (12s · ↑ 1.2k tokens · esc to interrupt)
╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
'
PROMPT_HIGH='╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
line
line
line
line
line
line
'

# MEASURED, not hand-written: the prompt line of Claude Code 2.1.261 in the
# image (jg-jcc2, 2026-11-10, `capture-pane -p -e`), idle with its dim
# placeholder, and with one character typed. NBSP after the prompt glyph.
ESC=$'\e'; NB=$'\xc2\xa0'
REAL_RULE="${ESC}[2m────────────────────────────────────────${ESC}[0m"
IDLE_REAL="${REAL_RULE}
${ESC}[39m❯${NB}${ESC}[2mTry \"fix lint errors\"${ESC}[0m
${REAL_RULE}
  ${ESC}[2m⏸ manual mode on · ? for shortcuts · ← for agents${ESC}[0m
"
TYPED_REAL="${REAL_RULE}
${ESC}[39m❯${NB}x
${REAL_RULE}
  ${ESC}[2m⏸ manual mode on · ? for shortcuts · ← for agents${ESC}[0m
"
TRUST_REAL=" Quick safety check: Is this a project you created or one you trust?
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"

