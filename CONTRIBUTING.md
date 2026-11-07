# Contributing

Issues and pull requests are welcome: bug reports, new relay or load-generator
adapters, cloud targets, and measurement-method fixes.

## Before you start

- Open an issue first for anything larger than a fix, so the approach can be
  agreed before you build it.
- Keep each pull request to one purpose, against `main`.

## Setup

You need bash, Python 3.11 or later, and, for cloud testbeds, Terraform and the
cloud's CLI (`aws` or `oci`). Copy the relevant `*.local.tfvars.example` to its
`*.local.tfvars` name and fill in your own account details; those files are
ignored by git.

## Checks

Run these before opening a pull request:

```bash
python3 -m pytest -q tests          # includes the repository hygiene checks
bash -n <each changed shell script>
```

## Rules for tracked content

- Never commit Terraform state, `*.local.tfvars`, `relays.local.sh`, `.testbed/`,
  `results/`, credentials, account or tenancy identifiers, or host addresses.
- A relay that is not public belongs in your ignored `relays.local.sh`
  (see `relays.local.sh.example`); do not name it in tracked files.
- Results stay out of the repo: no measured figures in files, commits or PR
  text. A number reported elsewhere states its testbed and the measured path
  ceiling (see the README's one rule and `PARITY.md`).
- Testbeds bill by the hour: destroy what you create with
  `./bin/testbed.sh <deployment> destroy`.

## Commits

Write a short imperative subject line, and explain why in the body when it is
not obvious from the change.

## License

By contributing you agree that your contribution is licensed under the
project's MIT license. Files in `patches/` modify third-party projects and stay
under those projects' licenses.
