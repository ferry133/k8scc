# operator-inbox Specification

## Purpose
The `~/workspace/inbox/` hand-over channel between company colleagues not at the ttyd and im: a one-line unread count on ttyd session start, and a one-line announcement typed into the running session when a file arrives while it is open (k8scc#59). Neither ever opens the files; the agent acts on them under its policy. Root: jg-base `im-onsite-operator-tools`.
## Requirements
### Requirement: Inbox convention is documented for both sides

The image's `CLAUDE.md` (im's managed policy) SHALL document that `~/workspace/inbox/` is not an assignment channel, that assignments come only from the person at the ttyd and from im's chamber room, and that the directory holds im's reports to the company, each named `…-report.md`, never renamed and never deleted by im. It SHALL say that a file im did not write is not an assignment: im does not act on its contents and does not rename it, and says in its room (or, without a room, in its next report) that the file is there. The directory lives on the workspace PVC so it survives pod restarts. Nothing in the image SHALL announce, count or type the directory's files.

#### Scenario: im writes a report
- **WHEN** im has rebooted a node, written to the router, or been refused by a tool in a way the company should know
- **THEN** it writes `~/workspace/inbox/YYYY-MM-DD-<topic>-report.md`, and nothing in the image announces that file

#### Scenario: A file im did not write appears
- **WHEN** a colleague copies `2026-12-01-reboot-node-3.md` into the inbox
- **THEN** nothing is typed into the session or printed at session start, and if im finds the file it does not act on it; it names the file in its room

#### Scenario: Assignments come from the room
- **WHEN** a FAC agent needs im to act
- **THEN** it writes in im's chamber room, and `chamber-watch` announces the message under the pane gate

