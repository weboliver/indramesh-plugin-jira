# Contributing to indramesh-plugin-jira

Thanks for taking the time to contribute. This document covers how to report a problem, how to
propose a change, and the terms a contribution is accepted under.

## How this repository is maintained

This repository is a **one-way mirror**. Development happens in the project's own development
tree and each release is published here from it: changes flow in one direction, outward from that
tree to this repository.

- A pull request opened here is reviewed and answered here. If it is accepted, the change is
  applied in the development tree and appears in this repository with the next published release.
- Nothing is merged back into the development tree *from* this repository, so it is the review
  that lands your change, not a merge here.

That is why a pull request can be accepted while this repository still shows the previous state
until the next release — and why a larger change is best described in an issue first.

## Reporting a problem

- **Security vulnerabilities** must never be reported in a public issue. See
  [SECURITY.md](SECURITY.md) for the private reporting paths.
- **Everything else** — open an issue. Say what you expected, what happened instead, and the
  smallest reproduction you can manage; include the release you run and how it is deployed
  (Docker Compose or a single-node Kubernetes cluster). The issue forms in this repository ask
  for exactly that.

## Proposing a change

- Fork the repository and open a pull request from a topic branch.
- Keep a pull request focused on one change, and describe in it what changes and why. Link the
  issue it answers, if there is one.
- Say how you checked the change: the command(s) you ran and what you observed. If the change
  touches the documented API, say so explicitly, because that surface is versioned.
- Please do not put credentials, tokens or private data from a running instance into a report, an
  issue or a commit.

## License of contributions

Contributions are accepted under the license this repository is published under:
**Apache-2.0**. Inbound equals outbound — a contribution is licensed to the project under the
same terms as the project's own code, and no separate contributor agreement or copyright
assignment is required.

## Sign-off (optional)

You may certify that you have the right to submit your contribution with a `Signed-off-by:`
trailer — the form the Developer Certificate of Origin uses:

```text
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds one per commit. The trailer is welcome but **optional**: a contribution
without it is not refused.