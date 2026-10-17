## ADDED Requirements

### Requirement: The base image carries sops and no private key tooling

The `base` image SHALL install a pinned `sops` and assert its version at build time. It SHALL NOT install `age`, `gh`, `flux` or `kustomize` for this purpose: encryption to a tenant public key needs only `sops`, reconciliation is triggered with `kubectl annotate`, and the agent never holds a private age key.

#### Scenario: sops present and pinned
- **WHEN** the base image is built
- **THEN** `sops --version` succeeds and matches the pinned version asserted in the Dockerfile

#### Scenario: No age private key tooling
- **WHEN** the base image is inspected
- **THEN** `age` and `age-keygen` are absent

### Requirement: The session configures the apps repo only when both inputs exist

`claude-session` SHALL, when `IM_APPS_REPO` (an `owner/repo`) is set **and** the write key file is mounted at its documented path, write `~/.ssh/config` with a dedicated host alias for github.com using that key (`IdentitiesOnly yes`, pinned known_hosts) and `~/.gitconfig` with the agent's identity, idempotently. When either input is absent it SHALL write neither file.

#### Scenario: Both inputs present
- **WHEN** `claude-session` runs with `IM_APPS_REPO` set and the key file present
- **THEN** `~/.ssh/config` contains the alias and `IdentityFile`, `~/.gitconfig` contains `user.name` and `user.email`, and a second run changes neither file

#### Scenario: Key absent, nothing written
- **WHEN** `IM_APPS_REPO` is set but the key file is missing
- **THEN** neither file is created and a one-line notice is printed

#### Scenario: Only the alias has a key
- **WHEN** the agent pushes to a remote that is not the configured alias
- **THEN** no key is offered and authentication fails

### Requirement: The policy says what belongs in the apps repo

`im-policy.md` SHALL carry a section for the apps repo stating: only manifests, kustomizations and a README go in; every `kind: Secret` is encrypted with the tenant public key before commit; commits carry no logs, IP addresses, or environment notes beyond hostnames in manifests; reconciliation is requested with `kubectl annotate` on the tenant Kustomization; anything meant to persist goes through the repo rather than `kubectl apply`; and that the section does not apply when no apps repo is configured. The policy controls test SHALL check the section's presence and the absence of wording that permits plaintext secrets or logs.

#### Scenario: Section present
- **WHEN** `test/im-policy-controls.sh` runs on the branch
- **THEN** its checks for the apps-repo section pass

#### Scenario: Old policy fails the control
- **WHEN** the same test runs against the previous `im-policy.md`
- **THEN** the apps-repo checks fail
