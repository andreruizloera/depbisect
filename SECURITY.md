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
versions involved. `--no-index --find-links` restricts installs to
distributions you already have locally, which is the most conservative
mode.

Manifest and lockfile parsing uses only the standard library
(`tomllib`, `json`) and never evaluates file content as code.

## Reporting

Open a GitHub issue for anything that does not require confidentiality.
For sensitive reports, email andre.x.ruizloera@gmail.com.
