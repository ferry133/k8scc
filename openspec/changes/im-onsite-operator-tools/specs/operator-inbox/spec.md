## ADDED Requirements

### Requirement: Inbox directory is announced at session start

`claude-session` SHALL check `~/workspace/inbox/` on every ttyd session start and, when it contains at least one file not ending in `.read`, print one line to the terminal naming the count and the path before the agent starts. It SHALL NOT parse, print or act on the files' contents.

#### Scenario: Unread file present
- **WHEN** `~/workspace/inbox/` contains `2026-10-11-break-glass.md`
- **THEN** the terminal shows one line such as `📥 inbox: 1 file(s) — read ~/workspace/inbox/ before anything else` before the agent prompt

#### Scenario: Only read files present
- **WHEN** every file in the directory ends in `.read`, or the directory is empty or absent
- **THEN** nothing is printed

### Requirement: Inbox convention is documented for both sides

The image's `CLAUDE.md` SHALL document that company colleagues who are not at the ttyd drop files for im into `~/workspace/inbox/` via `kubectl exec`/`cp` (the same directory carries im's own reports back), that the agent renames a file to `<name>.read` once acted on, and that the directory lives on the workspace PVC so it survives pod restarts.

#### Scenario: Agent marks a file read
- **WHEN** the agent has read and acted on an inbox file
- **THEN** the file is renamed with a `.read` suffix and is no longer counted at the next session start
