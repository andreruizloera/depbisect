# Security

## Threat model

depbisect executes two categories of untrusted things on your behalf:

1. The test command you pass with `--test` runs through the shell with
   your environment. It is your command; depbisect adds nothing to it
   beyond a `VIRTUAL_ENV`/`PATH` pointing at the trial virtualenv.
2. Installing a package version executes that package's build hooks
   and, when your tests import it, its code. Bisecting means
   installing versions you have not personally vetted, including
   intermediate releases you never ran before. If a compromised
   release exists between your good and bad versions, depbisect will
   install and execute it inside the trial environment, which is a
   normal virtualenv, not a security sandbox.

The temporary-workspace design protects your project files from
modification; it does not contain malicious code. Treat a bisect run
with the same caution as running `pip install` or `npm install` on the
versions involved. For a Python project, `--no-index --find-links`
restricts installs to distributions you already have locally, which is
the most conservative mode. There is no equivalent for a Node project:
its trials always run `npm install` against a registry, npm's configured
one or the one `--index-url` names.

Manifest and lockfile parsing uses only the standard library
(`tomllib`, `json`) and never evaluates file content as code.

## Network access

depbisect issues no network request of its own unless you pass
`--online`. With it, the only requests made are GET requests to the
simple repository API for the packages being bisected, at
`https://pypi.org/simple/` or whatever `--index-url` names. The URL
scheme is checked and must be http or https, so an index URL cannot be
used to read a local file. Responses are parsed as text with `json`
and `html.parser`; no part of a response is executed, and nothing is
written to disk.

`--online` does widen the exposure of point 2 above, because it finds
intermediate releases to install that you would not otherwise have
tested. An index that has been tampered with can offer versions that
do not exist upstream, and depbisect will install them in the trial
environment. If that matters for your threat model, `--find-links`
with a directory you control is the alternative, and it is still the
default.

## Reporting

Open a GitHub issue for anything that does not require confidentiality.
For sensitive reports, email andre.x.ruizloera@gmail.com.
