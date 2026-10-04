# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub instead: on the repository's **Security** tab, choose **Report a vulnerability**. You'll get a reply as soon as practical, and a fix and advisory will follow for confirmed issues.

Useful things to include: the affected component (agent, status page, Helm chart or Terraform), the version, and steps to reproduce.

## Supported versions

Only the latest release gets security fixes.

## Security notes for operators

- **The agent needs no Splunk token.** It sends to your own Splunk OTel Collector. Its RBAC is read-only cluster-wide: `get` on Deployments, StatefulSets and DaemonSets, `list` on pods and nodes, and `/readyz`. It also has `get`/`create`/`update` on Leases in its own namespace, for leader election.
- **The status page has no authentication.** Only expose it behind your own SSO or auth, or keep it internal. It needs only a **read** Splunk API token; don't give it an admin token.
- **Both containers** run as a non-root user with a read-only root filesystem, with all Linux capabilities dropped.
