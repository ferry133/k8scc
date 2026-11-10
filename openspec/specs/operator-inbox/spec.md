# operator-inbox Specification

## Purpose
The `~/workspace/inbox/` hand-over channel between company colleagues not at the ttyd and im: a one-line unread count on ttyd session start, and a one-line announcement typed into the running session when a file arrives while it is open (k8scc#59). Neither ever opens the files; the agent acts on them under its policy. Root: jg-base `im-onsite-operator-tools`.
## Requirements
### Requirement: Inbox directory is announced at session start

`claude-session` SHALL check `~/workspace/inbox/` on every ttyd session start and, when it contains at least one file not ending in `.read`, print one line to the terminal naming the count and the path before the agent starts. The script SHALL NOT parse or print the files' contents; acting on them is the agent's, under its policy.

#### Scenario: Unread file present
- **WHEN** `~/workspace/inbox/` contains `2026-10-11-break-glass.md`
- **THEN** the terminal shows one line such as `📥 inbox: 1 file(s) — read ~/workspace/inbox/ before anything else` before the agent prompt

#### Scenario: Only read files present
- **WHEN** every file in the directory ends in `.read`, or the directory is empty or absent
- **THEN** nothing is printed

### Requirement: A running session is told about files that arrive while it is open

The image SHALL run `inbox-watch` for the pod's lifetime (started by `entrypoint.sh`, every `INBOX_WATCH_INTERVAL` seconds, default 30; `0` turns it off). Each tick, when the tmux session `claude` exists and is at least `INBOX_WATCH_SETTLE` seconds old (default 30), it SHALL type one line into that session, followed by Enter, naming every unread file (not ending in `.read`) it has not yet announced. File names SHALL be made printable (control characters become `?`, cut at 120 characters) so a name cannot press Enter. It SHALL NOT open the files. A file is announced once; a send that fails marks nothing and is retried next tick; a file that leaves the unread set and returns is announced again. With no session it SHALL do nothing: no headless agent is started (ferry133, 2026-11-10). The pod log gets counts, not names.

Before typing anything it SHALL read the pane (`tmux capture-pane -p`) and type only when the tail reads as Claude Code's idle, empty input prompt: an empty prompt line in the last 6 lines (judged with colours kept: after the prompt glyph either nothing, or first the dim attribute of Claude Code's placeholder), no numbered option below it, and none of `esc to interrupt`, `Do you want to`, `Would you like to`, `reverse-i-search` in the last `INBOX_WATCH_TAIL` (15) lines. Anything else, including a capture that fails, SHALL skip the tick, mark nothing, and say `skipped (pane not idle: <reason>)` in the pod log. A permission or plan prompt is a menu in which a digit picks and Enter confirms, so a key typed into one could approve a tool call (fleet-ops-d9's review of #60).

#### Scenario: Assignment dropped into an open session
- **WHEN** `2026-11-10-ha-homekit-bringup-jcom.md` is copied into the inbox while the `claude` session has been open for an hour
- **THEN** within one interval the session receives one line, such as `📥 inbox: 1 new file(s) since this session started (2026-11-10-ha-homekit-bringup-jcom.md) — read ~/workspace/inbox/ and handle them per your policy, then rename each to <name>.read`, and it is not typed again on later ticks

#### Scenario: A permission prompt is open
- **WHEN** a file is dropped while the pane shows `Do you want to proceed?` with `❯ 1. Yes`
- **THEN** no key is typed, the prompt stays pending, and the file is announced on the first tick after the pane is back at the empty prompt

#### Scenario: Someone is typing
- **WHEN** the prompt line holds a half-written message
- **THEN** no key is typed until it is empty again

#### Scenario: No session
- **WHEN** a file is dropped and no tmux session `claude` exists
- **THEN** nothing is typed and nothing is started; the file is announced on the first tick after a session exists and has settled

#### Scenario: Policy names the line's origin
- **WHEN** im sees that line mid-session
- **THEN** its managed policy says the line comes from `inbox-watch`, not from the person at the terminal, and that the file's contents and the policy decide the action, not the line

### Requirement: Inbox convention is documented for both sides

The image's `CLAUDE.md` SHALL document that company colleagues who are not at the ttyd drop files for im into `~/workspace/inbox/` via `kubectl exec`/`cp` (the same directory carries im's own reports back), that the agent renames a file to `<name>.read` once acted on, and that the directory lives on the workspace PVC so it survives pod restarts.

#### Scenario: Agent marks a file read
- **WHEN** the agent has read and acted on an inbox file
- **THEN** the file is renamed with a `.read` suffix and is no longer counted at the next session start

