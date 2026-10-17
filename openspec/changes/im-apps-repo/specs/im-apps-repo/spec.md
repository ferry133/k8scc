## ADDED Requirements

### Requirement: The base image carries sops, pinned once, and no private key tooling

The `base` image SHALL install `sops` with `SOPS_VERSION` declared once before the first `FROM`, downloaded per architecture and verified against a sha256 pinned in the Dockerfile (the `omnictl` pattern), and SHALL assert its version at build time; the `factory` stage SHALL inherit it rather than install it again. The base image SHALL NOT install `age`, `gh`, `flux` or `kustomize` for this purpose: encryption to a tenant public key needs only `sops`, reconciliation is triggered with `kubectl annotate`, and the agent never holds a private age key.

#### Scenario: sops present, pinned, verified
- **WHEN** the base image is built
- **THEN** `sops --version` matches `SOPS_VERSION`, the download was checked against the pinned sha256 for the build architecture, and `factory` carries no second `sops` install step

#### Scenario: No age private key tooling
- **WHEN** the base image is inspected
- **THEN** `age` and `age-keygen` are absent

### Requirement: GitHub's SSH host keys are pinned in the image

The image SHALL carry a `known_hosts` file for `github.com` whose keys are taken from GitHub's published `ssh_keys` (the `/meta` API) and committed to this repository, and the build SHALL assert each key's fingerprint against values pinned in the Dockerfile. The apps-repo ssh configuration SHALL point at that file with `StrictHostKeyChecking yes`.

#### Scenario: Fingerprints asserted at build
- **WHEN** the image is built
- **THEN** `ssh-keygen -lf` over the committed file yields exactly the pinned fingerprints, and a build with one key altered fails

#### Scenario: Unknown host key is refused, not accepted
- **WHEN** a push is attempted and the server's key is not in the pinned file
- **THEN** ssh refuses the connection; nothing is added to any known_hosts

### Requirement: The session configures the apps repo only when both inputs exist, and git is told which ssh config to use

`claude-session` SHALL, when `IM_APPS_REPO` (an `owner/repo`) is set and non-empty (an empty value is the same as unset) **and** the write key file exists at `IM_APPS_KEY` (default `~/.ssh/im-apps.key`; empty means the default) with permissions ssh itself would accept (for a file owned by the calling user: no group or other bits, so `0600` or stricter; a file owned by another user is not checked for those bits), write `/home/claude/.ssh/config` with a dedicated host alias for github.com using that key (`IdentitiesOnly yes`, the pinned known_hosts, `StrictHostKeyChecking yes`) and `~/.gitconfig` with `user.name im`, `user.email im@<repo>` (the repository part of `IM_APPS_REPO`) and `core.sshCommand = ssh -F /home/claude/.ssh/config`, idempotently. The explicit `-F` is required because OpenSSH resolves its user configuration from the account's passwd home directory, not from `$HOME`, and the pod runs as uid 0 with `HOME` pinned to `/home/claude`; without it `~/.ssh/config` is never read while `~/.gitconfig` is. When either input is absent, or the key's mode would be rejected by ssh, it SHALL write neither file and print one line naming the failed precondition.

#### Scenario: Both inputs present
- **WHEN** `claude-session` runs with `IM_APPS_REPO=ferry133/jg-x-apps` and a `0600` key file present
- **THEN** `/home/claude/.ssh/config` contains the alias and `IdentityFile`, `~/.gitconfig` contains `user.name im`, `user.email im@jg-x-apps` and `core.sshCommand` pointing at that config, and a second run changes neither file

#### Scenario: Key absent, nothing written
- **WHEN** `IM_APPS_REPO` is set but the key file is missing
- **THEN** neither file is created and a one-line notice names the key path

#### Scenario: Key mode too open
- **WHEN** the key file is present, owned by the calling user, with mode `0644`
- **THEN** neither file is created and the notice names the mode

#### Scenario: Config is reached through -F, not through HOME
- **WHEN** `ssh -F /home/claude/.ssh/config -G <alias>` and `ssh -F /home/claude/.ssh/config -G github.com` are evaluated
- **THEN** only the alias resolves `identityfile` to the mounted key; and when the process's passwd home differs from `$HOME`, a plain `ssh -G <alias>` does **not** resolve it — which is why `core.sshCommand` carries `-F`

#### Scenario: Only the alias has a key
- **WHEN** the agent pushes to a remote that does not use the alias
- **THEN** no key is offered and authentication fails

### Requirement: The policy says what belongs in the apps repo

`im-policy.md` SHALL carry a section for the apps repo stating: only manifests, kustomizations and a README go in; every `kind: Secret` is encrypted with the tenant public key before commit; commits carry no logs, IP addresses, or environment notes beyond hostnames in manifests; reconciliation is requested with `kubectl annotate` on the tenant Kustomization; anything meant to persist goes through the repo rather than `kubectl apply`; and that the section does not apply when no apps repo is configured. The policy controls test SHALL check the section's presence and the absence of wording that permits plaintext secrets or logs.

#### Scenario: Section present
- **WHEN** `test/im-policy-controls.sh` runs on the branch
- **THEN** its checks for the apps-repo section pass

#### Scenario: Old policy fails the control
- **WHEN** the same test runs against the previous `im-policy.md`
- **THEN** the apps-repo checks fail
