# Security Policy

This is the security policy for **indramesh-plugin-jira**. It states which versions receive security
fixes, how to report a vulnerability privately, and what the project assumes about the
environment an instance runs in.

## Supported versions

Security fixes are issued for the **latest 1.x release only**.

| Version | Supported |
| ------- | --------- |
| The latest `1.x` release | Yes |
| Earlier releases, and any development snapshot | No |

Earlier releases are not patched retroactively. If you are on an older release, the fix is to
move to the current one.

## Reporting a vulnerability

Report suspected vulnerabilities **privately**, never in a public issue, a pull request or a
discussion thread — and please do not disclose the report publicly before a fix has been
released.

Two private paths reach the maintainer:

- **GitHub private vulnerability reporting** — open the *Security* tab of this repository and
  choose *Report a vulnerability*: https://github.com/weboliver/indramesh-plugin-jira/security/advisories/new
- **Email** — `security@indramesh.com`

A useful report contains the version or commit you tested, the smallest reproduction you can
manage, what you believe the impact is, and any suggestion you have for a fix. Please do not
include credentials or private data from a live instance; if a secret is part of the report,
say so and the maintainer will propose a channel for it.

## What to expect

This project is maintained by one person, so reports are handled on a **best effort** basis.
There is deliberately **no response deadline and no service-level agreement**: a turnaround
time that could not be kept would be worth less than saying so plainly.

What a report can expect:

- an acknowledgement that it arrived,
- an assessment of whether it is in scope and how it is planned,
- a fix and a release note once a fix is ready, and
- credit in the release note, if you want it.

Please allow a reasonable window for a fix before publishing anything about the report.

## Threat model

This section describes the trust boundaries the product is built around and the assumptions it
makes about its environment. It is a summary of the intended security posture, not an audit of
every component.

### Trust boundaries and assumptions

- **The administrator is trusted.** Whoever installs and operates the platform has full access
  to it and to the data in it. The product is not a sandbox between an administrator and that
  data, and a hostile administrator is not a threat this project defends against.
- **Plugins run with the core's full trust.** A plugin is application code loaded into the
  backend, not an isolated process: installing one is equivalent to running its author's code
  with the permissions of the platform itself. Only plugins you trust should be installed, and
  the platform does not attempt to contain a malicious one.
- **The data services are expected to bind to the internal network.** The database, the queue
  and the search service are assumed to be reachable only from the host or the cluster the
  instance runs in. Exposing them to an untrusted network is an operator error, not a supported
  configuration.
- **TLS is the operator's responsibility.** The shipped configuration is built to be reached
  over your own certificates or a terminating proxy. Serving an instance to anyone beyond your
  own machine on the shipped defaults is not covered.
- **Dependencies are pinned.** Images and packages are pinned to explicit versions so a
  published tree does not silently change under a redeploy.

### In scope

- Authentication, authorization and session handling in the API and the user interface.
- Injection of any kind through the documented API or the user interface.
- Remote code execution, privilege escalation or cross-tenant data exposure reachable through
  the shipped product as configured by default.
- Disclosure of secrets the product itself stores or manages.
- Server-side request forgery, unsafe deserialization and template injection through request
  data.

### Out of scope

- **Self-inflicted denial of service** — resource exhaustion caused by an authenticated
  administrator's own requests or configuration is an operational matter, not a vulnerability.
- **Social engineering**, including reports that require an administrator to be persuaded to run
  something harmful.
- **Third-party plugins.** A flaw in a plugin that does not ship with this repository belongs to
  that plugin's repository, not to this one.
- Findings that require an already compromised host, a hostile administrator, or direct
  filesystem access to the data services.
- Hardening measures that are already documented operator decisions (for example exposing a
  development port to an untrusted network).

## License

This policy is part of the project and is published under **Apache-2.0**.