## MODIFIED Requirements

### Requirement: A running session is told about files that arrive while it is open

The image SHALL run `inbox-watch` for the pod's lifetime (started by `entrypoint.sh`, every `INBOX_WATCH_INTERVAL` seconds, default 30; `0` turns it off). Each tick, when the tmux session `claude` exists and is at least `INBOX_WATCH_SETTLE` seconds old (default 30), it SHALL type one line into that session, followed by Enter, naming every unread assignment (ending in neither `.read` nor `-report.md`) it has not yet announced. Files ending in `-report.md` are im's own reports and SHALL NOT be announced (k8scc#65). File names SHALL be made printable (control characters become `?`, cut at 120 characters) so a name cannot press Enter. It SHALL NOT open the files. A file is announced once; a send that fails marks nothing and is retried next tick; a file that leaves the unread set and returns is announced again; after a pod restart, an assignment still unread is announced to the new session again. With no session `inbox-watch` SHALL do nothing and SHALL start nothing: no agent is started because a file arrived (ferry133, 2026-11-10). Whether a session exists when nobody is at the ttyd is decided elsewhere (`im-chamber-member`, session keeping; ferry133 2026-11-15: an im is always online), never by the arrival of a file or message. The pod log gets counts, not names.

Before typing anything it SHALL read the pane (`tmux capture-pane -p`) and type only when the tail reads as Claude Code's idle, empty input prompt: an empty prompt line in the last 6 lines (judged with colours kept: after the prompt glyph either nothing, or first the dim attribute of Claude Code's placeholder), no numbered option below it, and none of `esc to interrupt`, `Do you want to`, `Would you like to`, `reverse-i-search` in the last `INBOX_WATCH_TAIL` (15) lines. Anything else, including a capture that fails, SHALL skip the tick, mark nothing, and say `skipped (pane not idle: <reason>)` in the pod log. A permission or plan prompt is a menu in which a digit picks and Enter confirms, so a key typed into one could approve a tool call (fleet-ops-d9's review of #60). The pane gate SHALL be one implementation shared with `chamber-watch`.

#### Scenario: Assignment dropped into an open session
- **WHEN** `2026-11-10-ha-homekit-bringup-jcom.md` is copied into the inbox while the `claude` session has been open for an hour
- **THEN** within one interval the session receives one line, such as `📥 inbox: 1 unread assignment(s) not yet announced to this session (2026-11-10-ha-homekit-bringup-jcom.md) — read ~/workspace/inbox/ and handle them per your policy, then rename each to <name>.read`, and it is not typed again on later ticks

#### Scenario: A permission prompt is open
- **WHEN** a file is dropped while the pane shows `Do you want to proceed?` with `❯ 1. Yes`
- **THEN** no key is typed, the prompt stays pending, and the file is announced on the first tick after the pane is back at the empty prompt

#### Scenario: Someone is typing
- **WHEN** the prompt line holds a half-written message
- **THEN** no key is typed until it is empty again

#### Scenario: No session
- **WHEN** a file is dropped and no tmux session `claude` exists
- **THEN** nothing is typed and nothing is started by the watcher; the file is announced on the first tick after a session exists and has settled

#### Scenario: im's own reports are not announced
- **WHEN** the pod restarts and the inbox holds only `.read` files and im's reports such as `2026-11-11-iot-inventory-jcom-report.md`, or im writes `2026-11-12-ha-config-entry-audit-report.md` mid-session
- **THEN** nothing is typed and nothing is logged for them, while an assignment beside them is still announced

#### Scenario: Policy names the line's origin
- **WHEN** im sees that line mid-session
- **THEN** its managed policy says the line comes from `inbox-watch`, not from the person at the terminal, and that the file's contents and the policy decide the action, not the line

#### Scenario: Both channels are live
- **WHEN** an assignment file is dropped while the chamber is in `deliver` mode
- **THEN** the file is still announced as before, and im's report for it is written as `…-report.md`, not sent to the room
