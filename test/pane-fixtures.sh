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

# MEASURED: a session whose login has expired, after one chamber announcement
# was typed into it (jcom, production, Claude Code 2.1.261, 2026-11-25,
# `capture-pane -p -e`, fleet-ops-02 on k8scc#70 issuecomment-7166698330). The
# colour codes and the order of the lines are the capture's; the rules are
# shortened, the room name is changed, and the ten blank lines between the
# answer and the status line are kept, so that the "Login expired" line is
# outside the last 15 lines, as it was there.
LOGIN_RULE="${ESC}[38;5;244m────────────────────────────────────────"
LOGIN_FOOT="${ESC}[39m  ${ESC}[38;5;220m⏵⏵ auto mode on${ESC}[38;5;246m (shift+tab to cycle) · ← for agents"
LOGIN_HEAD="${ESC}[38;5;239m${ESC}[48;5;237m❯ ${ESC}[38;5;231m💬 chamber: 1 message(s) for you in cust-a — run ${ESC}[38;5;153mchamber read cust-a${ESC}[38;5;231m and handle them per your policy${ESC}[39m"
LOGIN_ANSWER="${ESC}[38;5;220m${ESC}[49m●${ESC}[39m ${ESC}[38;5;220mLogin expired · Please run /login
${ESC}[38;5;246m✻${ESC}[39m ${ESC}[38;5;246mCrunched for 0s · done 3:12 PM"
LOGIN_STATUS="${ESC}[39m                                        ${ESC}[38;5;211mNot logged in · Run /login"
LOGIN_GAP='









'
NOT_LOGGED_IN_REAL="${LOGIN_HEAD}
${LOGIN_ANSWER}
${LOGIN_GAP}${LOGIN_STATUS}
${LOGIN_RULE}
${ESC}[39m❯${NB}
${LOGIN_RULE}
${LOGIN_FOOT}
"
# Derived from the capture, not captured: the same pane without the status
# line and with the answer near the prompt, so only "Login expired" is in the
# tail; and the same pane with neither, which is an ordinary idle prompt (the
# positive control for both).
LOGIN_EXPIRED_NEAR="${LOGIN_HEAD}
${LOGIN_ANSWER}

${LOGIN_RULE}
${ESC}[39m❯${NB}
${LOGIN_RULE}
${LOGIN_FOOT}
"
LOGGED_IN_SAME_PANE="${LOGIN_HEAD}
${ESC}[38;5;246m✻${ESC}[39m ${ESC}[38;5;246mCrunched for 0s · done 3:12 PM
${LOGIN_GAP}
${LOGIN_RULE}
${ESC}[39m❯${NB}
${LOGIN_RULE}
${LOGIN_FOOT}
"
# Hand-written: a session that only talks about logging in. Not the status line.
TALKS_ABOUT_LOGIN="● If the terminal says you are not logged in, run /login and paste the code.
${LOGIN_RULE}
${ESC}[39m❯${NB}
${LOGIN_RULE}
${LOGIN_FOOT}
"
